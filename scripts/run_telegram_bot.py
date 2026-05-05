#!/usr/bin/env python3
"""
scripts/run_telegram_bot.py

Step 14a Module 7 + 14b-8: entry point for the AI Dev Team Telegram bot.
Wires components from core/* into a running long-poll loop.

Usage:
    python scripts/run_telegram_bot.py
    python scripts/run_telegram_bot.py --log-level=DEBUG

Required env (loaded from .env via python-dotenv):
    TELEGRAM_BOT_TOKEN          — token from @BotFather
    TELEGRAM_OWNER_CHAT_ID      — your numeric chat id (whitelist)

Optional env:
    OPENAI_API_KEY              — enables voice (Whisper)
    OPENROUTER_API_KEY          — enables vision (image description)
    REPO_PATH                   — git repo path; enables real LLM pipeline
                                  (requires OPENROUTER_API_KEY)
    WORKTREE_ROOT               — optional custom worktree directory
    BOT_COST_THRESHOLD_USD      — confirmation gate cost threshold (default 1.0)

This script keeps PTB-specific code at the boundary; all logic lives in
core.bot_runner and core.telegram_bridge, both fully unit-tested without
network or PTB dependencies.

Threading contract:
    send_callable        — called from executor threads via bridge.handle()
    send_progress_callable — called from BackgroundTaskRunner worker thread
    Both use asyncio.run_coroutine_threadsafe to safely schedule PTB coroutines
    onto the main event loop. Errors in send_progress are logged and swallowed
    so the worker thread stays alive.
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Ensure project root is on sys.path when running as a script
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from core.bot_runner import build_bridge_from_env, get_required_env  # noqa: E402
from core.telegram_bridge import IncomingMessage, OutgoingMessage  # noqa: E402

logger = logging.getLogger("ai_dev_team.bot")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Quiet down PTB's verbose internal loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.INFO)


def _build_send_callable(application, loop):
    """Create a sync `send(OutgoingMessage)` callable that schedules an
    async PTB send on the main event loop and waits for completion.

    Bridge runs handlers in an executor (background threads), so this is
    the safe way to bridge sync->async->Telegram.
    """

    def _send(out: OutgoingMessage) -> None:
        future = asyncio.run_coroutine_threadsafe(
            application.bot.send_message(
                chat_id=out.chat_id,
                text=out.text,
                reply_to_message_id=out.reply_to_message_id,
            ),
            loop,
        )
        # Block until the async send completes; surface errors to the bridge.
        future.result(timeout=30)

    return _send


def _build_send_progress_callable(application, loop):
    """Create a sync `send_progress(chat_id, text)` callable for streaming
    progress events from the BackgroundTaskRunner worker thread to the user.

    Called from a non-async worker thread — uses run_coroutine_threadsafe to
    safely schedule the PTB coroutine on the main event loop.

    Errors are logged and swallowed: the worker must not crash because a
    single Telegram send failed (network blip, rate limit, etc.).
    """

    def _send_progress(chat_id: int, text: str) -> None:
        try:
            future = asyncio.run_coroutine_threadsafe(
                application.bot.send_message(chat_id=chat_id, text=text),
                loop,
            )
            future.result(timeout=30)
        except Exception:
            logger.exception(
                "send_progress failed for chat_id=%s; worker continues", chat_id
            )

    return _send_progress


def _ptb_update_to_incoming(update) -> IncomingMessage | None:
    """Convert a PTB Update into our abstract IncomingMessage.

    Returns None if the update doesn't carry a message we should handle
    (e.g. edits, channel posts, callbacks).
    """
    msg = getattr(update, "message", None)
    if msg is None:
        return None
    chat = msg.chat
    user = msg.from_user
    if chat is None or user is None:
        return None

    # Telegram message has at most one of: text, voice, photo (for our purposes)
    text = msg.text or msg.caption  # caption when photo+text
    voice_bytes = None
    voice_mime = "audio/ogg"
    photo_bytes = None
    photo_mime = "image/jpeg"

    return IncomingMessage(
        chat_id=int(chat.id),
        user_id=int(user.id),
        message_id=int(msg.message_id),
        text=text,
        voice_bytes=voice_bytes,
        voice_mime=voice_mime,
        photo_bytes=photo_bytes,
        photo_mime=photo_mime,
        timestamp=float(msg.date.timestamp()) if msg.date else 0.0,
    )


async def _download_voice(message) -> tuple[bytes, str]:
    """Download voice payload from PTB message. Returns (bytes, mime_type)."""
    voice = message.voice
    file = await voice.get_file()
    payload = await file.download_as_bytearray()
    return bytes(payload), voice.mime_type or "audio/ogg"


async def _download_photo(message) -> tuple[bytes, str]:
    """Download the largest photo size from PTB message."""
    photo = message.photo[-1]  # largest variant
    file = await photo.get_file()
    payload = await file.download_as_bytearray()
    return bytes(payload), "image/jpeg"


def _make_handlers(bridge, loop):
    """Build PTB callbacks that delegate to bridge.handle(...)."""
    async def on_message(update, context):
        message = getattr(update, "message", None)
        if message is None:
            return

        # Resolve attachments asynchronously before handing off to bridge
        text = message.text or message.caption
        voice_bytes = None
        voice_mime = "audio/ogg"
        photo_bytes = None
        photo_mime = "image/jpeg"

        try:
            if message.voice is not None and not voice_bytes:
                voice_bytes, voice_mime = await _download_voice(message)
            if message.photo and not photo_bytes:
                photo_bytes, photo_mime = await _download_photo(message)
        except Exception as exc:
            logger.exception("Failed to download attachment: %s", exc)
            await context.bot.send_message(
                chat_id=message.chat.id,
                text=(
                    "Менеджер: Не удалось скачать вложение. "
                    "Попробуйте отправить ещё раз или текстом."
                ),
            )
            return

        # If nothing to handle, skip
        if not text and not voice_bytes and not photo_bytes:
            return

        try:
            incoming = IncomingMessage(
                chat_id=int(message.chat.id),
                user_id=int(message.from_user.id),
                message_id=int(message.message_id),
                text=text,
                voice_bytes=voice_bytes,
                voice_mime=voice_mime,
                photo_bytes=photo_bytes,
                photo_mime=photo_mime,
                timestamp=float(message.date.timestamp()) if message.date else 0.0,
            )
        except ValueError as exc:
            logger.warning("Invalid incoming message: %s", exc)
            return

        # Bridge.handle is sync — run in default executor so PTB loop stays free
        await loop.run_in_executor(None, bridge.handle, incoming)

    return on_message


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run AI Dev Team Telegram bot (long-polling).",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        help="Logging level: DEBUG / INFO / WARNING / ERROR",
    )
    args = parser.parse_args(argv)

    _setup_logging(args.log_level)

    load_dotenv(dotenv_path=ROOT / ".env")
    env = dict(os.environ)

    try:
        token = get_required_env(env, "TELEGRAM_BOT_TOKEN")
    except ValueError as exc:
        logger.error("Configuration error: %s", exc)
        return 2

    # Lazy PTB import — keep the rest of the script importable without
    # python-telegram-bot installed (useful for unit tests).
    try:
        from telegram.ext import (
            ApplicationBuilder,
            MessageHandler,
            filters,
        )
    except ImportError:
        logger.error(
            "python-telegram-bot not installed. "
            "Run: pip install -r requirements.txt"
        )
        return 3

    application = ApplicationBuilder().token(token).build()
    loop = asyncio.get_running_loop()

    send_callable = _build_send_callable(application, loop)
    send_progress_callable = _build_send_progress_callable(application, loop)
    try:
        bridge = build_bridge_from_env(
            env,
            send_callable=send_callable,
            send_progress_callable=send_progress_callable,
        )
    except ValueError as exc:
        logger.error("Bridge construction failed: %s", exc)
        return 4

    on_message = _make_handlers(bridge, loop)

    # One handler covers text, voice, photo, captioned photos, and slash
    # commands (we let parse_command in the bridge decide).
    application.add_handler(
        MessageHandler(
            filters.TEXT | filters.VOICE | filters.PHOTO | filters.CAPTION,
            on_message,
        )
    )

    logger.info("Bot starting. Owner whitelist: %s", env.get("TELEGRAM_OWNER_CHAT_ID"))
    logger.info("Whisper enabled: %s", bool(env.get("OPENAI_API_KEY")))
    logger.info("Vision enabled: %s", bool(env.get("OPENROUTER_API_KEY")))
    logger.info(
        "Real LLM pipeline: %s",
        bool(env.get("OPENROUTER_API_KEY") and env.get("REPO_PATH")),
    )

    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    # Keep alive until user interrupts (Ctrl+C)
    try:
        stop_event = asyncio.Event()
        await stop_event.wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        rc = 0
    sys.exit(rc)
