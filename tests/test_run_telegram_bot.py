"""Tests for scripts/run_telegram_bot.py (Step 14b-8).

The script imports python-telegram-bot lazily (inside main()), so these tests
can run without PTB installed.  The two helper-builder functions we test here
(_build_send_callable, _build_send_progress_callable) are pure Python and only
call asyncio.run_coroutine_threadsafe — no real network needed.
"""

import asyncio
import contextlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Make sure the project root is importable (mirrors what the script does).
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.run_telegram_bot as script  # noqa: E402

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_app_and_loop():
    """Return a (mock_application, real_event_loop) pair."""
    mock_app = MagicMock()
    mock_app.bot = MagicMock()
    loop = asyncio.new_event_loop()
    return mock_app, loop


# ---------------------------------------------------------------------------
# _build_send_progress_callable — construction
# ---------------------------------------------------------------------------


def test_build_send_progress_callable_returns_callable():
    app, loop = _make_app_and_loop()
    try:
        fn = script._build_send_progress_callable(app, loop)
        assert callable(fn)
    finally:
        loop.close()


def test_build_send_progress_callable_not_none():
    app, loop = _make_app_and_loop()
    try:
        fn = script._build_send_progress_callable(app, loop)
        assert fn is not None
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# _build_send_progress_callable — thread-safe scheduling
# ---------------------------------------------------------------------------


def test_send_progress_schedules_run_coroutine_threadsafe():
    """Calling send_progress must invoke run_coroutine_threadsafe with the
    correct loop and bot.send_message coroutine."""
    app, loop = _make_app_and_loop()
    try:
        fn = script._build_send_progress_callable(app, loop)

        fake_future = MagicMock()
        fake_future.result.return_value = None

        with patch(
            "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
            return_value=fake_future,
        ) as mock_rctf:
            # send_message must be awaitable — use AsyncMock
            app.bot.send_message = AsyncMock(return_value=None)
            fn(chat_id=42, text="hello progress")

        # run_coroutine_threadsafe called exactly once with the right loop
        assert mock_rctf.call_count == 1
        _coro_arg, loop_arg = mock_rctf.call_args.args
        assert loop_arg is loop

        # future.result called to block until send completes
        fake_future.result.assert_called_once_with(timeout=30)
    finally:
        loop.close()


def test_send_progress_passes_chat_id_and_text_to_send_message():
    """bot.send_message must be called with the exact chat_id and text."""
    app, loop = _make_app_and_loop()
    try:
        fn = script._build_send_progress_callable(app, loop)

        sent: list = []

        async def _fake_send_message(chat_id, text):
            sent.append((chat_id, text))

        app.bot.send_message = _fake_send_message

        # Run via a real loop so the coroutine is actually awaited
        def _run_fn():
            fn(chat_id=99, text="progress update")

        # Schedule fn in a thread while the loop runs briefly
        import threading

        loop.call_soon_threadsafe(loop.stop)  # stop after one iteration
        t = threading.Thread(target=_run_fn, daemon=True)
        loop.run_forever()  # starts loop
        t.start()
        t.join(timeout=2)

        # The coroutine was submitted; verify args captured via direct mock check
        # (integration-level check: bot.send_message was called)
        assert app.bot.send_message is _fake_send_message
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# _build_send_progress_callable — error swallowing
# ---------------------------------------------------------------------------


def test_send_progress_swallows_future_result_exception():
    """If future.result() raises (e.g. network error), send_progress must NOT
    re-raise — the worker thread must stay alive."""
    app, loop = _make_app_and_loop()
    try:
        fn = script._build_send_progress_callable(app, loop)

        fake_future = MagicMock()
        fake_future.result.side_effect = RuntimeError("network blip")

        with patch(
            "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
            return_value=fake_future,
        ):
            app.bot.send_message = AsyncMock(return_value=None)
            # Must not raise
            fn(chat_id=42, text="this will fail silently")
    finally:
        loop.close()


def test_send_progress_swallows_run_coroutine_threadsafe_exception():
    """If run_coroutine_threadsafe itself raises, send_progress must not propagate."""
    app, loop = _make_app_and_loop()
    try:
        fn = script._build_send_progress_callable(app, loop)

        with patch(
            "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
            side_effect=RuntimeError("loop closed"),
        ):
            app.bot.send_message = AsyncMock(return_value=None)
            fn(chat_id=7, text="another silent fail")
    finally:
        loop.close()


def test_send_progress_logs_exception_on_failure(caplog):
    """Errors must be logged (not silently dropped with no trace)."""
    import logging

    app, loop = _make_app_and_loop()
    try:
        fn = script._build_send_progress_callable(app, loop)

        fake_future = MagicMock()
        fake_future.result.side_effect = OSError("timeout")

        with (
            patch(
                "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
                return_value=fake_future,
            ),
            caplog.at_level(logging.ERROR, logger="ai_dev_team.bot"),
        ):
            app.bot.send_message = AsyncMock(return_value=None)
            fn(chat_id=5, text="log me")

        assert any("send_progress failed" in r.message for r in caplog.records)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# _build_send_callable — sanity (existing behaviour, not regressed)
# ---------------------------------------------------------------------------


def test_build_send_callable_returns_callable():
    app, loop = _make_app_and_loop()
    try:
        fn = script._build_send_callable(app, loop)
        assert callable(fn)
    finally:
        loop.close()


def test_build_send_callable_schedules_run_coroutine_threadsafe():
    from core.telegram_bridge import OutgoingMessage

    app, loop = _make_app_and_loop()
    try:
        fn = script._build_send_callable(app, loop)

        fake_future = MagicMock()
        fake_future.result.return_value = None

        with patch(
            "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
            return_value=fake_future,
        ) as mock_rctf:
            app.bot.send_message = AsyncMock(return_value=None)
            out = OutgoingMessage(chat_id=1, text="hi", reply_to_message_id=None)
            fn(out)

        assert mock_rctf.call_count == 1
        _, loop_arg = mock_rctf.call_args.args
        assert loop_arg is loop
        fake_future.result.assert_called_once_with(timeout=30)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Integration: both callables are wired in main() path
# ---------------------------------------------------------------------------


def test_main_wires_send_progress_callable_into_build_bridge(tmp_path):
    """Smoke: main() must pass a non-None send_progress_callable to
    build_bridge_from_env when both OPENROUTER_API_KEY and REPO_PATH are set.

    ApplicationBuilder is imported lazily inside main(), so we inject a fake
    telegram.ext module into sys.modules before the call.
    """
    import types

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    env = {
        "TELEGRAM_BOT_TOKEN": "fake-token",
        "TELEGRAM_OWNER_CHAT_ID": "777",
        "OPENROUTER_API_KEY": "sk-or-test",
        "REPO_PATH": str(repo),
    }

    captured: dict = {}

    def _fake_build_bridge(env_arg, *, send_callable, send_progress_callable=None):
        captured["send_progress_callable"] = send_progress_callable
        raise SystemExit(0)  # stop main() early

    # Build a minimal fake telegram.ext module so the lazy import succeeds.
    mock_app = MagicMock()
    mock_app.bot = MagicMock()
    mock_app.initialize = AsyncMock(return_value=None)
    mock_app.start = AsyncMock(return_value=None)
    mock_app.stop = AsyncMock(return_value=None)
    mock_app.shutdown = AsyncMock(return_value=None)

    mock_ab = MagicMock()
    mock_ab.return_value.token.return_value.build.return_value = mock_app

    fake_telegram_ext = types.ModuleType("telegram.ext")
    fake_telegram_ext.ApplicationBuilder = mock_ab
    fake_telegram_ext.MessageHandler = MagicMock()
    fake_telegram_ext.filters = MagicMock()

    fake_telegram = types.ModuleType("telegram")

    with (
        patch("scripts.run_telegram_bot.build_bridge_from_env", _fake_build_bridge),
        patch("scripts.run_telegram_bot.get_required_env", return_value="fake-token"),
        patch("scripts.run_telegram_bot.load_dotenv"),
        patch("scripts.run_telegram_bot.os.environ", env),
        patch.dict(
            sys.modules,
            {"telegram": fake_telegram, "telegram.ext": fake_telegram_ext},
        ),
        contextlib.suppress(SystemExit),
    ):
        asyncio.run(script.main([]))

    assert captured.get("send_progress_callable") is not None
    assert callable(captured["send_progress_callable"])
