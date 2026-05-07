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
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    mock_app = SimpleNamespace(bot=SimpleNamespace())
    loop = asyncio.new_event_loop()
    return mock_app, loop


def _close_coro_and_return(fake_future):
    """Mock side-effect for run_coroutine_threadsafe that avoids leaked coroutines."""

    def _submit(coro, _loop):
        coro.close()
        return fake_future

    return _submit


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
    correct loop and register a done-callback (fire-and-forget)."""
    app, loop = _make_app_and_loop()
    try:
        fn = script._build_send_progress_callable(app, loop)

        fake_future = MagicMock()

        with patch(
            "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
            side_effect=_close_coro_and_return(fake_future),
        ) as mock_rctf:
            app.bot.send_message = AsyncMock(return_value=None)
            fn(chat_id=42, text="hello progress")

        # run_coroutine_threadsafe called exactly once with the right loop
        assert mock_rctf.call_count == 1
        _coro_arg, loop_arg = mock_rctf.call_args.args
        assert loop_arg is loop

        # Fire-and-forget: callback registered, result() NEVER called
        fake_future.add_done_callback.assert_called_once()
        fake_future.result.assert_not_called()
    finally:
        loop.close()


def test_send_progress_passes_chat_id_and_text_to_send_message():
    """bot.send_message must be called with the exact chat_id and text."""
    app, loop = _make_app_and_loop()
    try:
        fn = script._build_send_progress_callable(app, loop)
        fake_future = MagicMock()
        app.bot.send_message = AsyncMock(return_value=None)

        with patch(
            "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
            side_effect=_close_coro_and_return(fake_future),
        ):
            fn(chat_id=99, text="progress update")

        app.bot.send_message.assert_called_once_with(
            chat_id=99,
            text="progress update",
        )
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# _build_send_progress_callable — error swallowing
# ---------------------------------------------------------------------------


def test_send_progress_swallows_run_coroutine_threadsafe_exception():
    """If run_coroutine_threadsafe itself raises, send_progress must not propagate."""
    app, loop = _make_app_and_loop()
    try:
        fn = script._build_send_progress_callable(app, loop)
        fake_coro = MagicMock()
        app.bot.send_message = MagicMock(return_value=fake_coro)

        with patch(
            "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
            side_effect=RuntimeError("loop closed"),
        ):
            fn(chat_id=7, text="another silent fail")
        fake_coro.close.assert_called_once_with()
    finally:
        loop.close()


def test_send_progress_done_callback_logs_send_message_error(caplog):
    """_on_send_done callback must log errors from the Telegram send coroutine."""
    import logging

    app, loop = _make_app_and_loop()
    try:
        # Grab the _on_send_done callback by capturing add_done_callback call
        fn = script._build_send_progress_callable(app, loop)

        fake_future = MagicMock()
        captured_callbacks: list = []
        fake_future.add_done_callback.side_effect = captured_callbacks.append

        with (
            patch(
                "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
                side_effect=_close_coro_and_return(fake_future),
            ),
            caplog.at_level(logging.ERROR, logger="ai_dev_team.bot"),
        ):
            app.bot.send_message = AsyncMock(return_value=None)
            fn(chat_id=5, text="log me")

        assert len(captured_callbacks) == 1
        on_done = captured_callbacks[0]

        # Simulate a failed future
        error_future = MagicMock()
        error_future.exception.return_value = OSError("Telegram 429")
        on_done(error_future)

        assert any("send_progress send_message failed" in r.message for r in caplog.records)
    finally:
        loop.close()


def test_send_progress_done_callback_silent_on_success(caplog):
    """_on_send_done must not log anything when the future succeeded."""
    import logging

    app, loop = _make_app_and_loop()
    try:
        fn = script._build_send_progress_callable(app, loop)

        fake_future = MagicMock()
        captured_callbacks: list = []
        fake_future.add_done_callback.side_effect = captured_callbacks.append

        with (
            patch(
                "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
                side_effect=_close_coro_and_return(fake_future),
            ),
            caplog.at_level(logging.ERROR, logger="ai_dev_team.bot"),
        ):
            app.bot.send_message = AsyncMock(return_value=None)
            fn(chat_id=5, text="ok send")

        on_done = captured_callbacks[0]
        ok_future = MagicMock()
        ok_future.exception.return_value = None
        on_done(ok_future)

        assert not caplog.records
    finally:
        loop.close()


def test_send_progress_does_not_block_worker_on_slow_telegram():
    """Fire-and-forget: _send_progress must return without waiting for the
    future to complete, even if result() would block/raise immediately."""
    import time

    app, loop = _make_app_and_loop()
    try:
        fn = script._build_send_progress_callable(app, loop)

        fake_future = MagicMock()
        # Simulate a future whose result() blocks forever (rate-limit scenario)
        fake_future.result.side_effect = RuntimeError("should never be called")

        with patch(
            "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
            side_effect=_close_coro_and_return(fake_future),
        ):
            app.bot.send_message = AsyncMock(return_value=None)
            start = time.monotonic()
            fn(chat_id=1, text="fire and forget")
            elapsed = time.monotonic() - start

        # Should return in well under 1 second (no blocking network call)
        assert elapsed < 1.0
        fake_future.result.assert_not_called()
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
            side_effect=_close_coro_and_return(fake_future),
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


def test_build_send_callable_closes_coro_when_submission_fails():
    from core.telegram_bridge import OutgoingMessage

    app, loop = _make_app_and_loop()
    try:
        fn = script._build_send_callable(app, loop)
        fake_coro = MagicMock()
        app.bot.send_message = MagicMock(return_value=fake_coro)

        with (
            patch(
                "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
                side_effect=RuntimeError("loop closed"),
            ),
            pytest.raises(RuntimeError, match="loop closed"),
        ):
            fn(OutgoingMessage(chat_id=1, text="hi", reply_to_message_id=None))

        fake_coro.close.assert_called_once_with()
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
    mock_app = SimpleNamespace(bot=SimpleNamespace())

    class _Builder:
        def token(self, _token):
            return self

        def build(self):
            return mock_app

    fake_telegram_ext = types.ModuleType("telegram.ext")
    fake_telegram_ext.ApplicationBuilder = _Builder
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
