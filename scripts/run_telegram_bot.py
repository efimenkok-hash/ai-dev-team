#!/usr/bin/env python3
"""
scripts/run_telegram_bot.py

Step 14a Module 7 + 14b-8: entry point for the AI Dev Team Telegram bot.
Wires components from core/* into a running long-poll loop.

Usage:
    python scripts/run_telegram_bot.py
    python scripts/run_telegram_bot.py --log-level=DEBUG

Required env (loaded from .env via python-dotenv):
    TELEGRAM_OWNER_CHAT_ID      — your numeric chat id (whitelist)
    TELEGRAM_BOT_TOKEN          — coordinator / single-bot compatibility token

Optional env:
    STATE_DB_PATH               — canonical SQLite state path
    TELEGRAM_AGENT_TOKENS       — canonical multi-bot role-to-env-key mapping
    OPENROUTER_API_KEY          — canonical LLM + vision key
    OPENAI_API_KEY              — Whisper voice transcription only
    BOT_COST_THRESHOLD_USD      — confirmation gate cost threshold (default 1.0)
    OBS_LOG_PATH                — observability JSONL sink
    LOG_LEVEL                   — logging level override

Legacy compatibility / bootstrap env:
    BOT_STATE_DIR               — legacy fallback directory for state.db
    REPO_PATH                   — legacy single-project runtime seed only
    WORKTREE_ROOT               — optional legacy worktree-root override

This script keeps PTB-specific code at the boundary; all logic lives in
core.bot_runner and core.telegram_bridge, both fully unit-tested without
network or PTB dependencies.

Threading contract:
    send_callable        — called from executor threads via bridge.handle()
    send_progress_callable — called from BackgroundTaskRunner worker thread
    send_progress_envelope_callable — same worker-thread path, but preserves
    sender_role until transport selection
    Both use asyncio.run_coroutine_threadsafe to safely schedule PTB coroutines
    onto the main event loop. Errors in send_progress are logged and swallowed
    so the worker thread stays alive.
"""

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

# Ensure project root is on sys.path when running as a script
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from core.bot_identity_lifecycle import (  # noqa: E402
    BotIdentityLifecycleService,
    MultiBotLifecycleReport,
)
from core.bot_runner import (  # noqa: E402
    build_bridge_from_env,
    build_multi_bot_bridge_from_env,
    build_multi_bot_runtime_spec_from_env,
    get_required_env,
)
from core.coordinator_role import COORDINATOR_ROLE  # noqa: E402
from core.env_layout import BotRuntimeEnvConfig  # noqa: E402
from core.multi_bot_bridge import MultiBotBridge  # noqa: E402
from core.multi_bot_runtime import BotIdentity  # noqa: E402
from core.multi_bot_sender import (  # noqa: E402
    MultiBotOutboundSender,
    PerRoleOutboundSender,
    RoleBoundSender,
)
from core.startup_config_validation import (  # noqa: E402
    format_startup_validation_report,
    validate_bot_startup_config,
)
from core.telegram_bridge import (  # noqa: E402
    IncomingMessage,
    OutgoingEnvelope,
    OutgoingMessage,
)

logger = logging.getLogger("ai_dev_team.bot")
_LIFECYCLE_SERVICE = BotIdentityLifecycleService()
_CONFIG_FAILURE_EXIT_CODE = 2


class _StartupLifecycleError(RuntimeError):
    def __init__(self, reason: str, report: MultiBotLifecycleReport) -> None:
        super().__init__(reason)
        self.report = report


def _log_lifecycle_report(
    report: MultiBotLifecycleReport,
    *,
    level: str = "info",
    prefix: str = "Lifecycle summary",
) -> None:
    log_fn = getattr(logger, level)
    log_fn("%s\n%s", prefix, _LIFECYCLE_SERVICE.format_report(report))


def _classify_reachability_failure(role: str, exc: Exception) -> str:
    exc_name = type(exc).__name__
    if exc_name in {"InvalidToken", "Unauthorized"}:
        return f"bot_token_invalid:{role}"
    return f"bot_reachable_check_failed:{role}"


@dataclass(frozen=True)
class RunningBotApplication:
    identity: BotIdentity
    application: object
    send_callable: object

    def __post_init__(self) -> None:
        if not isinstance(self.identity, BotIdentity):
            raise ValueError(
                "invalid_running_bot_identity_type:"
                f"{type(self.identity).__name__}"
            )
        if self.application is None:
            raise ValueError("running_bot_application_missing_application")
        if not callable(self.send_callable):
            raise ValueError("running_bot_send_callable_not_callable")


@dataclass(frozen=True)
class RunningMultiBotRuntime:
    bridge: MultiBotBridge
    applications_by_role: Mapping[str, RunningBotApplication]
    outbound_sender: MultiBotOutboundSender
    progress_sender: MultiBotOutboundSender
    lifecycle_report: MultiBotLifecycleReport
    primary_role: str

    def __post_init__(self) -> None:
        if not isinstance(self.bridge, MultiBotBridge):
            raise ValueError(
                "invalid_multi_bot_bridge_type:"
                f"{type(self.bridge).__name__}"
            )
        if not isinstance(self.outbound_sender, MultiBotOutboundSender):
            raise ValueError(
                "invalid_multi_bot_outbound_sender_type:"
                f"{type(self.outbound_sender).__name__}"
            )
        if not isinstance(self.progress_sender, MultiBotOutboundSender):
            raise ValueError(
                "invalid_multi_bot_progress_sender_type:"
                f"{type(self.progress_sender).__name__}"
            )
        if not isinstance(self.lifecycle_report, MultiBotLifecycleReport):
            raise ValueError(
                "invalid_multi_bot_lifecycle_report_type:"
                f"{type(self.lifecycle_report).__name__}"
            )
        if not isinstance(self.applications_by_role, Mapping):
            raise ValueError(
                "invalid_running_multi_bot_applications_type:"
                f"{type(self.applications_by_role).__name__}"
            )
        if self.primary_role != COORDINATOR_ROLE:
            raise ValueError(
                "running_multi_bot_primary_role_must_be_coordinator_agent:"
                f"{self.primary_role}"
            )

        expected_roles = self.bridge.enabled_roles()
        if self.outbound_sender.enabled_roles() != expected_roles:
            raise ValueError("running_multi_bot_outbound_sender_role_mismatch")
        if self.progress_sender.enabled_roles() != expected_roles:
            raise ValueError("running_multi_bot_progress_sender_role_mismatch")
        if tuple(self.lifecycle_report.states_by_role.keys()) != expected_roles:
            raise ValueError("running_multi_bot_lifecycle_report_role_mismatch")
        raw_roles = tuple(self.applications_by_role.keys())
        if not raw_roles:
            raise ValueError("empty_running_multi_bot_applications")
        unexpected_roles = sorted(set(raw_roles) - set(expected_roles))
        if unexpected_roles:
            raise ValueError(
                "unexpected_running_multi_bot_roles:"
                + ",".join(unexpected_roles)
            )

        normalized: dict[str, RunningBotApplication] = {}
        for role in expected_roles:
            if role not in self.applications_by_role:
                raise ValueError(f"missing_running_multi_bot_role:{role}")
            running = self.applications_by_role[role]
            if not isinstance(running, RunningBotApplication):
                raise ValueError(
                    "invalid_running_bot_application_type:"
                    f"{type(running).__name__}"
                )
            if running.identity.agent_role != role:
                raise ValueError(
                    "running_bot_role_identity_mismatch:"
                    f"{role}!={running.identity.agent_role}"
                )
            normalized[role] = running
        object.__setattr__(
            self,
            "applications_by_role",
            MappingProxyType(normalized),
        )


def _set_runtime_lifecycle_report(
    runtime: RunningMultiBotRuntime,
    report: MultiBotLifecycleReport,
) -> None:
    if not isinstance(runtime, RunningMultiBotRuntime):
        raise ValueError(
            "invalid_running_multi_bot_runtime_type:"
            f"{type(runtime).__name__}"
        )
    if not isinstance(report, MultiBotLifecycleReport):
        raise ValueError(
            "invalid_multi_bot_lifecycle_report_type:"
            f"{type(report).__name__}"
        )
    object.__setattr__(runtime, "lifecycle_report", report)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Quiet down PTB's verbose internal loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.INFO)


def _load_runtime_env() -> tuple[dict[str, str], BotRuntimeEnvConfig]:
    load_dotenv(dotenv_path=ROOT / ".env")
    env = dict(os.environ)
    return env, BotRuntimeEnvConfig.from_env(env)


def _build_send_callable(application, loop):
    """Create a sync `send(OutgoingMessage)` callable that schedules an
    async PTB send on the main event loop and waits for completion.

    Bridge runs handlers in an executor (background threads), so this is
    the safe way to bridge sync->async->Telegram.
    """

    def _send(out: OutgoingMessage) -> None:
        coro = application.bot.send_message(
            chat_id=out.chat_id,
            text=out.text,
            reply_to_message_id=out.reply_to_message_id,
        )
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            coro.close()
            raise
        # Block until the async send completes; surface errors to the bridge.
        future.result(timeout=30)

    return _send


def _build_send_envelope_callable(send_callable):
    if not callable(send_callable):
        raise ValueError("send_callable_not_callable")

    def _send_envelope(envelope: OutgoingEnvelope) -> None:
        if not isinstance(envelope, OutgoingEnvelope):
            raise ValueError(
                "invalid_outgoing_envelope_type:"
                f"{type(envelope).__name__}"
            )
        send_callable(envelope.message)

    return _send_envelope


def _load_ptb_runtime():
    try:
        from telegram.ext import (
            ApplicationBuilder,
            MessageHandler,
            filters,
        )
    except ImportError:
        return None
    return SimpleNamespace(
        ApplicationBuilder=ApplicationBuilder,
        MessageHandler=MessageHandler,
        filters=filters,
    )


def _build_send_progress_callable(application, loop):
    """Create a sync `send_progress(chat_id, text)` callable for streaming
    progress events from the BackgroundTaskRunner worker thread to the user.

    Called from a non-async worker thread — uses run_coroutine_threadsafe to
    safely schedule the PTB coroutine on the main event loop.

    Fire-and-forget: we do NOT call future.result() so the worker thread is
    never blocked waiting for Telegram's network round-trip.  A pipeline
    emits ~16 progress events; blocking on each would add up to 8 minutes of
    stall under a Telegram rate-limit (429).  Errors are surfaced via
    add_done_callback and logged without crashing the worker.
    """

    def _on_send_done(future) -> None:
        exc = future.exception()
        if exc is not None:
            logger.error("send_progress send_message failed: %s", exc)

    def _send_progress(chat_id: int, text: str) -> None:
        coro = application.bot.send_message(chat_id=chat_id, text=text)
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            future.add_done_callback(_on_send_done)
            # NB: не вызываем future.result() — это сериализовало бы worker
            # на сетевом I/O, превращая 16 событий × 30с в 8-минутный затык
            # под rate-limit'ом Telegram. Ошибки логируются в callback.
        except Exception:
            coro.close()
            logger.exception(
                "send_progress submission failed for chat_id=%s; worker continues",
                chat_id,
            )

    return _send_progress


def _build_send_progress_envelope_callable(application, loop):
    """Create a sync `send_progress_envelope(OutgoingEnvelope)` callable.

    This path is intentionally fire-and-forget: used for worker progress and
    project updates where blocking the worker thread on Telegram delivery would
    serialize the whole pipeline on network I/O.
    """

    def _on_send_done(future) -> None:
        exc = future.exception()
        if exc is not None:
            logger.error("send_progress_envelope send_message failed: %s", exc)

    def _send_progress_envelope(envelope: OutgoingEnvelope) -> None:
        if not isinstance(envelope, OutgoingEnvelope):
            raise ValueError(
                "invalid_outgoing_envelope_type:"
                f"{type(envelope).__name__}"
            )
        coro = application.bot.send_message(
            chat_id=envelope.message.chat_id,
            text=envelope.message.text,
            reply_to_message_id=envelope.message.reply_to_message_id,
        )
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            future.add_done_callback(_on_send_done)
        except Exception:
            coro.close()
            logger.exception(
                "send_progress_envelope submission failed for chat_id=%s; "
                "worker continues",
                envelope.message.chat_id,
            )

    return _send_progress_envelope


async def _probe_bot_identity(running_app: RunningBotApplication) -> tuple[int, str]:
    if not isinstance(running_app, RunningBotApplication):
        raise ValueError(
            "invalid_running_bot_application_type:"
            f"{type(running_app).__name__}"
        )
    me = await running_app.application.bot.get_me()
    return int(me.id), str(me.username)


async def _shutdown_single_application(
    application,
    *,
    initialized: bool,
    started: bool,
    polling_started: bool,
) -> None:
    if polling_started:
        updater = getattr(application, "updater", None)
        if updater is not None:
            with contextlib.suppress(Exception):
                await updater.stop()
    if started:
        with contextlib.suppress(Exception):
            await application.stop()
    if initialized:
        with contextlib.suppress(Exception):
            await application.shutdown()


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


def _make_incoming_handler(
    handle_incoming,
    loop,
    *,
    reply_on_attachment_error: bool = True,
    incoming_bot_role: str | None = None,
):
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
            if reply_on_attachment_error:
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
                incoming_bot_role=incoming_bot_role,
            )
        except ValueError as exc:
            logger.warning("Invalid incoming message: %s", exc)
            return

        await loop.run_in_executor(None, handle_incoming, incoming)

    return on_message


def _make_handlers(bridge, loop):
    """Build PTB callbacks that delegate to bridge.handle(...)."""
    return _make_incoming_handler(bridge.handle, loop)


def _make_multi_bot_handlers(runtime_bridge, agent_role, loop):
    def _handle_incoming(incoming: IncomingMessage):
        return runtime_bridge.handle_incoming(agent_role, incoming)

    return _make_incoming_handler(
        _handle_incoming,
        loop,
        reply_on_attachment_error=(agent_role == COORDINATOR_ROLE),
        incoming_bot_role=agent_role,
    )


def _build_running_multi_bot_runtime(
    env: Mapping[str, str],
    loop,
    *,
    ptb_runtime=None,
) -> RunningMultiBotRuntime | None:
    if not isinstance(env, Mapping):
        raise ValueError("env_must_be_mapping")
    runtime_spec = build_multi_bot_runtime_spec_from_env(env)
    if runtime_spec is None or runtime_spec.source != "telegram_agent_tokens":
        return None
    resolved_ptb_runtime = (
        _load_ptb_runtime() if ptb_runtime is None else ptb_runtime
    )
    if resolved_ptb_runtime is None:
        raise RuntimeError("ptb_runtime_unavailable")

    applications_by_role: dict[str, RunningBotApplication] = {}
    for role in runtime_spec.role_map.by_role:
        identity = runtime_spec.role_map.by_role[role]
        try:
            application = (
                resolved_ptb_runtime.ApplicationBuilder()
                .token(identity.token)
                .build()
            )
        except Exception as exc:
            raise RuntimeError(
                f"multi_bot_application_build_failed:{role}"
            ) from exc
        applications_by_role[role] = RunningBotApplication(
            identity=identity,
            application=application,
            send_callable=_build_send_callable(application, loop),
        )

    coordinator_app = applications_by_role[COORDINATOR_ROLE]
    progress_sender = MultiBotOutboundSender(
        PerRoleOutboundSender(
            primary_role=COORDINATOR_ROLE,
            senders_by_role={
                role: RoleBoundSender(
                    identity=running_app.identity,
                    send_envelope=_build_send_progress_envelope_callable(
                        running_app.application,
                        loop,
                    ),
                )
                for role, running_app in applications_by_role.items()
            },
        )
    )
    bridge = build_multi_bot_bridge_from_env(
        env,
        send_callable=coordinator_app.send_callable,
        send_progress_callable=_build_send_progress_callable(
            coordinator_app.application,
            loop,
        ),
        send_progress_envelope_callable=progress_sender.send,
    )
    if bridge is None:
        raise RuntimeError("multi_bot_bridge_build_failed")

    outbound_sender = MultiBotOutboundSender(
        PerRoleOutboundSender(
            primary_role=COORDINATOR_ROLE,
            senders_by_role={
                role: RoleBoundSender(
                    identity=running_app.identity,
                    send_envelope=_build_send_envelope_callable(
                        running_app.send_callable
                    ),
                )
                for role, running_app in applications_by_role.items()
            },
        )
    )
    bridge.primary_bridge.set_send_envelope(outbound_sender.send)

    message_filter = (
        resolved_ptb_runtime.filters.TEXT
        | resolved_ptb_runtime.filters.VOICE
        | resolved_ptb_runtime.filters.PHOTO
        | resolved_ptb_runtime.filters.CAPTION
    )
    for role, running_app in applications_by_role.items():
        running_app.application.add_handler(
            resolved_ptb_runtime.MessageHandler(
                message_filter,
                _make_multi_bot_handlers(bridge, role, loop),
            )
        )

    lifecycle_report = _LIFECYCLE_SERVICE.build_initial_report(runtime_spec)

    return RunningMultiBotRuntime(
        bridge=bridge,
        applications_by_role=applications_by_role,
        outbound_sender=outbound_sender,
        progress_sender=progress_sender,
        lifecycle_report=lifecycle_report,
        primary_role=COORDINATOR_ROLE,
    )


async def _start_running_multi_bot_runtime(
    runtime: RunningMultiBotRuntime,
) -> None:
    initialized_roles: list[str] = []
    started_roles: list[str] = []
    polling_roles: list[str] = []
    report = runtime.lifecycle_report
    try:
        logger.info(
            "Probing enabled bot identities: %s",
            ", ".join(runtime.applications_by_role.keys()),
        )
        for role, running_app in runtime.applications_by_role.items():
            try:
                bot_user_id, bot_username = await _probe_bot_identity(running_app)
                report = _LIFECYCLE_SERVICE.mark_reachable(
                    report,
                    role,
                    bot_user_id=bot_user_id,
                    bot_username=bot_username,
                )
                _set_runtime_lifecycle_report(runtime, report)
            except Exception as exc:
                reason = _classify_reachability_failure(role, exc)
                report = _LIFECYCLE_SERVICE.mark_failure(report, role, reason)
                _set_runtime_lifecycle_report(runtime, report)
                raise RuntimeError(reason) from exc
            logger.info(
                "Bot identity %s reachable as @%s (id=%s)",
                role,
                bot_username,
                bot_user_id,
            )

        for role, running_app in runtime.applications_by_role.items():
            try:
                await running_app.application.initialize()
                initialized_roles.append(role)
                report = _LIFECYCLE_SERVICE.mark_initialized(report, role)
                _set_runtime_lifecycle_report(runtime, report)
            except Exception as exc:
                reason = f"multi_bot_application_initialize_failed:{role}"
                report = _LIFECYCLE_SERVICE.mark_failure(report, role, reason)
                _set_runtime_lifecycle_report(runtime, report)
                raise RuntimeError(reason) from exc
            try:
                await running_app.application.start()
                started_roles.append(role)
            except Exception as exc:
                reason = f"multi_bot_application_start_failed:{role}"
                report = _LIFECYCLE_SERVICE.mark_failure(report, role, reason)
                _set_runtime_lifecycle_report(runtime, report)
                raise RuntimeError(reason) from exc
            report = _LIFECYCLE_SERVICE.mark_started(report, role)
            _set_runtime_lifecycle_report(runtime, report)

            updater = getattr(running_app.application, "updater", None)
            if updater is None:
                reason = f"multi_bot_application_polling_failed:{role}"
                report = _LIFECYCLE_SERVICE.mark_failure(report, role, reason)
                _set_runtime_lifecycle_report(runtime, report)
                raise RuntimeError(reason)
            polling_roles.append(role)
            try:
                await updater.start_polling()
            except Exception as exc:
                reason = f"multi_bot_application_polling_failed:{role}"
                report = _LIFECYCLE_SERVICE.mark_failure(report, role, reason)
                _set_runtime_lifecycle_report(runtime, report)
                raise RuntimeError(reason) from exc
            report = _LIFECYCLE_SERVICE.mark_polling_started(report, role)
            _set_runtime_lifecycle_report(runtime, report)
    except Exception:
        await _shutdown_running_multi_bot_runtime(
            runtime,
            initialized_roles=tuple(initialized_roles),
            started_roles=tuple(started_roles),
            polling_roles=tuple(polling_roles),
        )
        raise
    _log_lifecycle_report(
        runtime.lifecycle_report,
        prefix="Multi-bot lifecycle summary",
    )


async def _shutdown_running_multi_bot_runtime(
    runtime: RunningMultiBotRuntime,
    *,
    initialized_roles: tuple[str, ...] | None = None,
    started_roles: tuple[str, ...] | None = None,
    polling_roles: tuple[str, ...] | None = None,
) -> None:
    resolved_initialized = (
        initialized_roles
        if initialized_roles is not None
        else tuple(runtime.applications_by_role.keys())
    )
    resolved_started = (
        started_roles
        if started_roles is not None
        else tuple(runtime.applications_by_role.keys())
    )
    resolved_polling = (
        polling_roles
        if polling_roles is not None
        else tuple(runtime.applications_by_role.keys())
    )

    for role in reversed(resolved_polling):
        updater = getattr(
            runtime.applications_by_role[role].application,
            "updater",
            None,
        )
        if updater is None:
            continue
        with contextlib.suppress(Exception):
            await updater.stop()

    for role in reversed(resolved_started):
        with contextlib.suppress(Exception):
            await runtime.applications_by_role[role].application.stop()

    for role in reversed(resolved_initialized):
        with contextlib.suppress(Exception):
            await runtime.applications_by_role[role].application.shutdown()


async def _start_single_bot_application(
    application,
    *,
    identity: BotIdentity,
    report: MultiBotLifecycleReport,
) -> MultiBotLifecycleReport:
    initialized = False
    started = False
    polling_started = False
    updated_report = report
    role = identity.agent_role
    try:
        bot_user_id, bot_username = await _probe_bot_identity(
            RunningBotApplication(
                identity=identity,
                application=application,
                send_callable=lambda _out: None,
            )
        )
        updated_report = _LIFECYCLE_SERVICE.mark_reachable(
            updated_report,
            role,
            bot_user_id=bot_user_id,
            bot_username=bot_username,
        )
        logger.info(
            "Bot identity %s reachable as @%s (id=%s)",
            role,
            bot_username,
            bot_user_id,
        )
    except Exception as exc:
        reason = _classify_reachability_failure(role, exc)
        updated_report = _LIFECYCLE_SERVICE.mark_failure(
            updated_report,
            role,
            reason,
        )
        raise _StartupLifecycleError(reason, updated_report) from exc

    try:
        await application.initialize()
        initialized = True
        updated_report = _LIFECYCLE_SERVICE.mark_initialized(
            updated_report,
            role,
        )
    except Exception as exc:
        reason = f"multi_bot_application_initialize_failed:{role}"
        updated_report = _LIFECYCLE_SERVICE.mark_failure(
            updated_report,
            role,
            reason,
        )
        await _shutdown_single_application(
            application,
            initialized=initialized,
            started=started,
            polling_started=polling_started,
        )
        raise _StartupLifecycleError(reason, updated_report) from exc

    try:
        await application.start()
        started = True
        updated_report = _LIFECYCLE_SERVICE.mark_started(
            updated_report,
            role,
        )
    except Exception as exc:
        reason = f"multi_bot_application_start_failed:{role}"
        updated_report = _LIFECYCLE_SERVICE.mark_failure(
            updated_report,
            role,
            reason,
        )
        await _shutdown_single_application(
            application,
            initialized=initialized,
            started=started,
            polling_started=polling_started,
        )
        raise _StartupLifecycleError(reason, updated_report) from exc

    updater = getattr(application, "updater", None)
    if updater is None:
        reason = f"multi_bot_application_polling_failed:{role}"
        updated_report = _LIFECYCLE_SERVICE.mark_failure(
            updated_report,
            role,
            reason,
        )
        await _shutdown_single_application(
            application,
            initialized=initialized,
            started=started,
            polling_started=polling_started,
        )
        raise _StartupLifecycleError(reason, updated_report)
    try:
        await updater.start_polling()
        polling_started = True
        updated_report = _LIFECYCLE_SERVICE.mark_polling_started(
            updated_report,
            role,
        )
    except Exception as exc:
        reason = f"multi_bot_application_polling_failed:{role}"
        updated_report = _LIFECYCLE_SERVICE.mark_failure(
            updated_report,
            role,
            reason,
        )
        await _shutdown_single_application(
            application,
            initialized=initialized,
            started=started,
            polling_started=polling_started,
        )
        raise _StartupLifecycleError(reason, updated_report) from exc

    _log_lifecycle_report(
        updated_report,
        prefix="Single-bot lifecycle summary",
    )
    return updated_report


async def main(argv: list[str] | None = None) -> int:
    env, bot_env = _load_runtime_env()
    parser = argparse.ArgumentParser(
        description="Run AI Dev Team Telegram bot (long-polling).",
    )
    parser.add_argument(
        "--log-level",
        default=bot_env.shared.log_level or "INFO",
        help="Logging level: DEBUG / INFO / WARNING / ERROR",
    )
    args = parser.parse_args(argv)

    _setup_logging(args.log_level)

    validation_report = validate_bot_startup_config(env)
    if validation_report.has_errors:
        logger.error(
            "Startup config validation failed:\n%s",
            format_startup_validation_report(validation_report),
        )
        return _CONFIG_FAILURE_EXIT_CODE
    if validation_report.has_warnings:
        logger.warning(
            "Startup config warnings:\n%s",
            format_startup_validation_report(validation_report),
        )

    ptb_runtime = _load_ptb_runtime()
    if ptb_runtime is None:
        logger.error(
            "python-telegram-bot not installed. "
            "Run: pip install -r requirements.txt"
        )
        return 3

    # Sweep stale worktrees from previous sessions before accepting messages.
    # A previous bot crash or kill -9 can leave /tmp/aidt_worktrees/<task_id>
    # directories that git no longer tracks. Cleaning them on startup keeps
    # disk usage bounded and prevents stale state from interfering with new
    # acquire() calls that could trip the worktree_exists guard.
    from core.bot_runner import cleanup_orphan_worktrees_from_env

    orphans = cleanup_orphan_worktrees_from_env(env)
    if orphans > 0:
        logger.info("Cleaned %d orphan worktree(s) from previous sessions", orphans)

    loop = asyncio.get_running_loop()
    try:
        multi_runtime = _build_running_multi_bot_runtime(
            env,
            loop,
            ptb_runtime=ptb_runtime,
        )
    except Exception as exc:
        logger.error("Multi-bot runtime construction failed: %s", exc)
        return 5

    if multi_runtime is not None:
        logger.info(
            "Multi-bot runtime enabled for roles: %s",
            ", ".join(multi_runtime.applications_by_role.keys()),
        )
        logger.info(
            "Bot starting. Owner whitelist: %s",
            bot_env.telegram_owner_chat_id,
        )
        logger.info("Whisper enabled: %s", bool(bot_env.openai_api_key))
        logger.info("Vision enabled: %s", bool(bot_env.openrouter_api_key))
        logger.info(
            "Real LLM pipeline: %s",
            bool(
                bot_env.openrouter_api_key
                and bot_env.legacy.has_project_runtime_seed
            ),
        )
        try:
            await _start_running_multi_bot_runtime(multi_runtime)
        except Exception as exc:
            logger.error("Multi-bot startup failed: %s", exc)
            _log_lifecycle_report(
                multi_runtime.lifecycle_report,
                level="error",
                prefix="Multi-bot lifecycle failure report",
            )
            return 5
        try:
            stop_event = asyncio.Event()
            await stop_event.wait()
        finally:
            await _shutdown_running_multi_bot_runtime(multi_runtime)
        return 0

    try:
        token = get_required_env(env, "TELEGRAM_BOT_TOKEN")
    except ValueError as exc:
        logger.error("Configuration error: %s", exc)
        return _CONFIG_FAILURE_EXIT_CODE

    legacy_runtime_spec = build_multi_bot_runtime_spec_from_env(env)
    if legacy_runtime_spec is None:
        logger.error("Legacy runtime spec could not be built from TELEGRAM_BOT_TOKEN")
        return _CONFIG_FAILURE_EXIT_CODE

    application = ptb_runtime.ApplicationBuilder().token(token).build()
    legacy_report = _LIFECYCLE_SERVICE.build_initial_report(legacy_runtime_spec)

    send_callable = _build_send_callable(application, loop)
    send_progress_callable = _build_send_progress_callable(application, loop)
    send_progress_envelope_callable = _build_send_progress_envelope_callable(
        application,
        loop,
    )
    try:
        bridge = build_bridge_from_env(
            env,
            send_callable=send_callable,
            send_progress_callable=send_progress_callable,
            send_progress_envelope_callable=send_progress_envelope_callable,
        )
    except ValueError as exc:
        logger.error("Bridge construction failed: %s", exc)
        return 4

    on_message = _make_handlers(bridge, loop)

    application.add_handler(
        ptb_runtime.MessageHandler(
            ptb_runtime.filters.TEXT
            | ptb_runtime.filters.VOICE
            | ptb_runtime.filters.PHOTO
            | ptb_runtime.filters.CAPTION,
            on_message,
        )
    )

    logger.info("Bot starting. Owner whitelist: %s", bot_env.telegram_owner_chat_id)
    logger.info("Whisper enabled: %s", bool(bot_env.openai_api_key))
    logger.info("Vision enabled: %s", bool(bot_env.openrouter_api_key))
    logger.info(
        "Real LLM pipeline: %s",
        bool(
            bot_env.openrouter_api_key
            and bot_env.legacy.has_project_runtime_seed
        ),
    )
    try:
        legacy_report = await _start_single_bot_application(
            application,
            identity=legacy_runtime_spec.primary_bot,
            report=legacy_report,
        )
    except Exception as exc:
        if isinstance(exc, _StartupLifecycleError):
            legacy_report = exc.report
        logger.error("Single-bot startup failed: %s", exc)
        _log_lifecycle_report(
            legacy_report,
            level="error",
            prefix="Single-bot lifecycle failure report",
        )
        return 5

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
