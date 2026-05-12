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
from core.agent_personas import default_registry  # noqa: E402
from core.coordinator_role import COORDINATOR_ROLE  # noqa: E402
from core.multi_bot_bridge import MultiBotBridge  # noqa: E402
from core.multi_bot_runtime import (  # noqa: E402
    BotIdentity,
    MultiBotRuntimeSpec,
    PerRoleBotMap,
)
from core.multi_bot_sender import (  # noqa: E402
    MultiBotOutboundSender,
    PerRoleOutboundSender,
    RoleBoundSender,
)
from core.telegram_bridge import (  # noqa: E402
    BridgeReply,
    BridgeResult,
    IncomingMessage,
    OutgoingEnvelope,
    OutgoingMessage,
    TelegramBridge,
)

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


def _identity(
    role: str = COORDINATOR_ROLE,
    *,
    bot_id: str | None = None,
    token_env_key: str | None = None,
    token: str = "123:token",
) -> BotIdentity:
    resolved_bot_id = bot_id if bot_id is not None else role
    resolved_env_key = (
        token_env_key
        if token_env_key is not None
        else f"TELEGRAM_{role.upper()}_TOKEN"
    )
    return BotIdentity(
        bot_id=resolved_bot_id,
        agent_role=role,
        token_env_key=resolved_env_key,
        token=token,
    )


def _role_map(*identities: BotIdentity) -> PerRoleBotMap:
    return PerRoleBotMap(
        {identity.agent_role: identity for identity in identities}
    )


def _multi_bridge() -> MultiBotBridge:
    coordinator = _identity(
        token_env_key="TELEGRAM_BOT_TOKEN",
        token="123:coord",
    )
    writer = _identity(
        "writer_agent",
        token_env_key="TELEGRAM_WRITER_BOT_TOKEN",
        token="456:writer",
    )
    runtime_spec = MultiBotRuntimeSpec(
        primary_bot=coordinator,
        role_map=_role_map(coordinator, writer),
        source="telegram_agent_tokens",
    )

    def _send(_out) -> None:
        return None

    primary_bridge = TelegramBridge(
        owner_chat_ids=frozenset({777}),
        send=_send,
        personas=default_registry(),
        task_handler=lambda _text, _msg: None,
    )
    return MultiBotBridge(
        runtime_spec=runtime_spec,
        primary_bridge=primary_bridge,
    )


def _outbound_sender_for_bridge(bridge: MultiBotBridge) -> MultiBotOutboundSender:
    return MultiBotOutboundSender(
        PerRoleOutboundSender(
            primary_role=COORDINATOR_ROLE,
            senders_by_role={
                role: RoleBoundSender(
                    identity=bridge.resolve_identity(role),
                    send_envelope=lambda _envelope: None,
                )
                for role in bridge.enabled_roles()
            },
        )
    )


def _progress_sender_for_bridge(bridge: MultiBotBridge) -> MultiBotOutboundSender:
    return _outbound_sender_for_bridge(bridge)


def _multi_bridge_with_task_handler(task_handler) -> MultiBotBridge:
    coordinator = _identity(
        token_env_key="TELEGRAM_BOT_TOKEN",
        token="123:coord",
    )
    writer = _identity(
        "writer_agent",
        token_env_key="TELEGRAM_WRITER_BOT_TOKEN",
        token="456:writer",
    )
    runtime_spec = MultiBotRuntimeSpec(
        primary_bot=coordinator,
        role_map=_role_map(coordinator, writer),
        source="telegram_agent_tokens",
    )

    def _send(_out) -> None:
        return None

    primary_bridge = TelegramBridge(
        owner_chat_ids=frozenset({777}),
        send=_send,
        personas=default_registry(),
        task_handler=task_handler,
    )
    return MultiBotBridge(
        runtime_spec=runtime_spec,
        primary_bridge=primary_bridge,
    )


class _FakeFilter:
    def __or__(self, _other):
        return self


class _FakeMessageHandler:
    def __init__(self, filters, callback):
        self.filters = filters
        self.callback = callback


class _FakeUpdater:
    def __init__(self, *, fail_polling: bool = False):
        self.start_polling = AsyncMock(
            side_effect=(
                RuntimeError("polling failed")
                if fail_polling
                else None
            )
        )
        self.stop = AsyncMock(return_value=None)


class _FakeApplication:
    def __init__(
        self,
        token: str,
        *,
        fail_start: bool = False,
        fail_polling: bool = False,
    ):
        self.token = token
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=None))
        self.handlers: list[_FakeMessageHandler] = []
        self.initialize = AsyncMock(return_value=None)
        self.start = AsyncMock(
            side_effect=(
                RuntimeError("start failed")
                if fail_start
                else None
            )
        )
        self.stop = AsyncMock(return_value=None)
        self.shutdown = AsyncMock(return_value=None)
        self.updater = _FakeUpdater(fail_polling=fail_polling)

    def add_handler(self, handler) -> None:
        self.handlers.append(handler)


class _FakePTBRuntime:
    def __init__(self, app_factory):
        self._app_factory = app_factory
        self.filters = SimpleNamespace(
            TEXT=_FakeFilter(),
            VOICE=_FakeFilter(),
            PHOTO=_FakeFilter(),
            CAPTION=_FakeFilter(),
        )
        self.MessageHandler = _FakeMessageHandler

    def ApplicationBuilder(self):
        app_factory = self._app_factory

        class _Builder:
            def __init__(self):
                self._token = None

            def token(self, token):
                self._token = token
                return self

            def build(self):
                return app_factory(self._token)

        return _Builder()


class _ImmediateLoop:
    def __init__(self):
        self.calls: list[tuple[object, tuple[object, ...]]] = []

    async def run_in_executor(self, _executor, func, *args):
        self.calls.append((func, args))
        return func(*args)


# ---------------------------------------------------------------------------
# RunningBotApplication / RunningMultiBotRuntime
# ---------------------------------------------------------------------------


def test_running_bot_application_happy_path():
    running = script.RunningBotApplication(
        identity=_identity(),
        application=object(),
        send_callable=lambda _out: None,
    )

    assert running.identity.agent_role == COORDINATOR_ROLE


def test_running_bot_application_rejects_invalid_identity():
    with pytest.raises(
        ValueError,
        match="invalid_running_bot_identity_type:str",
    ):
        script.RunningBotApplication(  # type: ignore[arg-type]
            identity="not-identity",
            application=object(),
            send_callable=lambda _out: None,
        )


def test_running_bot_application_rejects_non_callable_send():
    with pytest.raises(
        ValueError,
        match="running_bot_send_callable_not_callable",
    ):
        script.RunningBotApplication(
            identity=_identity(),
            application=object(),
            send_callable="not-callable",
        )


def test_running_multi_bot_runtime_happy_path():
    bridge = _multi_bridge()
    coordinator = script.RunningBotApplication(
        identity=bridge.resolve_identity(COORDINATOR_ROLE),
        application=object(),
        send_callable=lambda _out: None,
    )
    writer = script.RunningBotApplication(
        identity=bridge.resolve_identity("writer_agent"),
        application=object(),
        send_callable=lambda _out: None,
    )

    runtime = script.RunningMultiBotRuntime(
        bridge=bridge,
        applications_by_role={
            "writer_agent": writer,
            COORDINATOR_ROLE: coordinator,
        },
        outbound_sender=_outbound_sender_for_bridge(bridge),
        progress_sender=_progress_sender_for_bridge(bridge),
        primary_role=COORDINATOR_ROLE,
    )

    assert tuple(runtime.applications_by_role.keys()) == (
        COORDINATOR_ROLE,
        "writer_agent",
    )


def test_running_multi_bot_runtime_rejects_missing_role():
    bridge = _multi_bridge()
    coordinator = script.RunningBotApplication(
        identity=bridge.resolve_identity(COORDINATOR_ROLE),
        application=object(),
        send_callable=lambda _out: None,
    )

    with pytest.raises(
        ValueError,
        match="missing_running_multi_bot_role:writer_agent",
    ):
        script.RunningMultiBotRuntime(
            bridge=bridge,
            applications_by_role={COORDINATOR_ROLE: coordinator},
            outbound_sender=_outbound_sender_for_bridge(bridge),
            progress_sender=_progress_sender_for_bridge(bridge),
            primary_role=COORDINATOR_ROLE,
        )


def test_running_multi_bot_runtime_rejects_role_identity_mismatch():
    bridge = _multi_bridge()
    coordinator = script.RunningBotApplication(
        identity=bridge.resolve_identity(COORDINATOR_ROLE),
        application=object(),
        send_callable=lambda _out: None,
    )

    with pytest.raises(
        ValueError,
        match="running_bot_role_identity_mismatch:writer_agent!=coordinator_agent",
    ):
        script.RunningMultiBotRuntime(
            bridge=bridge,
            applications_by_role={
                COORDINATOR_ROLE: coordinator,
                "writer_agent": coordinator,
            },
            outbound_sender=_outbound_sender_for_bridge(bridge),
            progress_sender=_progress_sender_for_bridge(bridge),
            primary_role=COORDINATOR_ROLE,
        )


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
# _build_send_progress_envelope_callable — thread-safe scheduling
# ---------------------------------------------------------------------------


def test_send_progress_envelope_schedules_run_coroutine_threadsafe():
    app, loop = _make_app_and_loop()
    try:
        fn = script._build_send_progress_envelope_callable(app, loop)
        fake_future = MagicMock()
        envelope = OutgoingEnvelope(
            message=OutgoingMessage(chat_id=42, text="writer update"),
            sender_role="writer_agent",
        )

        with patch(
            "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
            side_effect=_close_coro_and_return(fake_future),
        ) as mock_rctf:
            app.bot.send_message = AsyncMock(return_value=None)
            fn(envelope)

        assert mock_rctf.call_count == 1
        _coro_arg, loop_arg = mock_rctf.call_args.args
        assert loop_arg is loop
        fake_future.add_done_callback.assert_called_once()
        fake_future.result.assert_not_called()
    finally:
        loop.close()


def test_send_progress_envelope_routes_message_fields_without_rewriting_text():
    app, loop = _make_app_and_loop()
    try:
        fn = script._build_send_progress_envelope_callable(app, loop)
        fake_future = MagicMock()
        app.bot.send_message = AsyncMock(return_value=None)
        envelope = OutgoingEnvelope(
            message=OutgoingMessage(
                chat_id=99,
                text="Архитектор: progress update",
                reply_to_message_id=7,
            ),
            sender_role="architect_agent",
        )

        with patch(
            "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
            side_effect=_close_coro_and_return(fake_future),
        ):
            fn(envelope)

        app.bot.send_message.assert_called_once_with(
            chat_id=99,
            text="Архитектор: progress update",
            reply_to_message_id=7,
        )
    finally:
        loop.close()


def test_send_progress_envelope_does_not_block_worker_on_slow_telegram():
    import time

    app, loop = _make_app_and_loop()
    try:
        fn = script._build_send_progress_envelope_callable(app, loop)
        fake_future = MagicMock()
        envelope = OutgoingEnvelope(
            message=OutgoingMessage(chat_id=1, text="fire and forget"),
            sender_role=COORDINATOR_ROLE,
        )

        with patch(
            "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
            side_effect=_close_coro_and_return(fake_future),
        ):
            app.bot.send_message = AsyncMock(return_value=None)
            start = time.monotonic()
            fn(envelope)
            elapsed = time.monotonic() - start

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

    def _fake_build_bridge(
        env_arg,
        *,
        send_callable,
        send_progress_callable=None,
        send_progress_envelope_callable=None,
    ):
        captured["send_progress_callable"] = send_progress_callable
        captured["send_progress_envelope_callable"] = (
            send_progress_envelope_callable
        )
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
        patch(
            "core.bot_runner.cleanup_orphan_worktrees_from_env",
            return_value=0,
        ),
        patch.dict(
            sys.modules,
            {"telegram": fake_telegram, "telegram.ext": fake_telegram_ext},
        ),
        contextlib.suppress(SystemExit),
    ):
        asyncio.run(script.main([]))

    assert captured.get("send_progress_callable") is not None
    assert callable(captured["send_progress_callable"])
    assert captured.get("send_progress_envelope_callable") is not None
    assert callable(captured["send_progress_envelope_callable"])


# ---------------------------------------------------------------------------
# Multi-bot runtime helpers / main wiring
# ---------------------------------------------------------------------------


def test_build_running_multi_bot_runtime_returns_none_for_legacy_env():
    loop = _ImmediateLoop()
    assert (
        script._build_running_multi_bot_runtime(
            {
                "TELEGRAM_BOT_TOKEN": "123:legacy",
                "TELEGRAM_OWNER_CHAT_ID": "777",
            },
            loop,
            ptb_runtime=_FakePTBRuntime(lambda token: _FakeApplication(token)),
        )
        is None
    )


def test_build_running_multi_bot_runtime_builds_application_per_enabled_role(tmp_path):
    built_tokens: list[str] = []

    def _app_factory(token):
        built_tokens.append(token)
        return _FakeApplication(token)

    runtime = script._build_running_multi_bot_runtime(
        {
            "TELEGRAM_OWNER_CHAT_ID": "777",
            "STATE_DB_PATH": str(tmp_path / "state.db"),
            "TELEGRAM_AGENT_TOKENS": (
                "coordinator_agent=TELEGRAM_BOT_TOKEN,"
                "writer_agent=TELEGRAM_WRITER_BOT_TOKEN"
            ),
            "TELEGRAM_BOT_TOKEN": "123:coord",
            "TELEGRAM_WRITER_BOT_TOKEN": "456:writer",
        },
        _ImmediateLoop(),
        ptb_runtime=_FakePTBRuntime(_app_factory),
    )

    assert runtime is not None
    assert built_tokens == ["123:coord", "456:writer"]
    assert tuple(runtime.applications_by_role.keys()) == (
        COORDINATOR_ROLE,
        "writer_agent",
    )
    assert runtime.outbound_sender.enabled_roles() == (
        COORDINATOR_ROLE,
        "writer_agent",
    )
    assert runtime.progress_sender.enabled_roles() == (
        COORDINATOR_ROLE,
        "writer_agent",
    )
    assert len(runtime.applications_by_role[COORDINATOR_ROLE].application.handlers) == 1
    assert len(runtime.applications_by_role["writer_agent"].application.handlers) == 1


def test_multi_bot_runtime_routes_coordinator_reply_through_coordinator_sender(
    tmp_path,
):
    runtime_bridge = _multi_bridge_with_task_handler(
        lambda _text, _msg: BridgeReply(
            persona_role=COORDINATOR_ROLE,
            body="контрольный ответ",
        )
    )
    with patch(
        "scripts.run_telegram_bot.build_multi_bot_bridge_from_env",
        return_value=runtime_bridge,
    ):
        runtime = script._build_running_multi_bot_runtime(
            {
                "TELEGRAM_OWNER_CHAT_ID": "777",
                "STATE_DB_PATH": str(tmp_path / "state.db"),
                "TELEGRAM_AGENT_TOKENS": (
                    "coordinator_agent=TELEGRAM_BOT_TOKEN,"
                    "writer_agent=TELEGRAM_WRITER_BOT_TOKEN"
                ),
                "TELEGRAM_BOT_TOKEN": "123:coord",
                "TELEGRAM_WRITER_BOT_TOKEN": "456:writer",
            },
            _ImmediateLoop(),
            ptb_runtime=_FakePTBRuntime(lambda token: _FakeApplication(token)),
        )

    assert runtime is not None
    coordinator_app = runtime.applications_by_role[COORDINATOR_ROLE].application
    writer_app = runtime.applications_by_role["writer_agent"].application
    fake_future = MagicMock()
    fake_future.result.return_value = None

    with patch(
        "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
        side_effect=_close_coro_and_return(fake_future),
    ):
        result = runtime.bridge.primary_bridge.handle(
            IncomingMessage(
                chat_id=777,
                user_id=777,
                message_id=1,
                text="сделай задачу",
            )
        )

    assert result.handled is True
    coordinator_app.bot.send_message.assert_called_once()
    writer_app.bot.send_message.assert_not_called()
    assert "Координатор:" in coordinator_app.bot.send_message.call_args.kwargs["text"]


def test_multi_bot_runtime_routes_writer_reply_through_writer_sender(tmp_path):
    runtime_bridge = _multi_bridge_with_task_handler(
        lambda _text, _msg: BridgeReply(
            persona_role="writer_agent",
            body="черновик готов",
        )
    )
    with patch(
        "scripts.run_telegram_bot.build_multi_bot_bridge_from_env",
        return_value=runtime_bridge,
    ):
        runtime = script._build_running_multi_bot_runtime(
            {
                "TELEGRAM_OWNER_CHAT_ID": "777",
                "STATE_DB_PATH": str(tmp_path / "state.db"),
                "TELEGRAM_AGENT_TOKENS": (
                    "coordinator_agent=TELEGRAM_BOT_TOKEN,"
                    "writer_agent=TELEGRAM_WRITER_BOT_TOKEN"
                ),
                "TELEGRAM_BOT_TOKEN": "123:coord",
                "TELEGRAM_WRITER_BOT_TOKEN": "456:writer",
            },
            _ImmediateLoop(),
            ptb_runtime=_FakePTBRuntime(lambda token: _FakeApplication(token)),
        )

    assert runtime is not None
    coordinator_app = runtime.applications_by_role[COORDINATOR_ROLE].application
    writer_app = runtime.applications_by_role["writer_agent"].application
    fake_future = MagicMock()
    fake_future.result.return_value = None

    with patch(
        "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
        side_effect=_close_coro_and_return(fake_future),
    ):
        result = runtime.bridge.primary_bridge.handle(
            IncomingMessage(
                chat_id=777,
                user_id=777,
                message_id=2,
                text="сделай текст",
            )
        )

    assert result.handled is True
    writer_app.bot.send_message.assert_called_once()
    coordinator_app.bot.send_message.assert_not_called()
    sent_text = writer_app.bot.send_message.call_args.kwargs["text"]
    assert sent_text.startswith("Программист:")
    assert "черновик готов" in sent_text


def test_multi_bot_runtime_unknown_role_falls_back_to_coordinator_sender(tmp_path):
    runtime_bridge = _multi_bridge_with_task_handler(
        lambda _text, _msg: BridgeReply(
            persona_role="ghost_agent",
            body="неизвестная роль ответила",
        )
    )
    with patch(
        "scripts.run_telegram_bot.build_multi_bot_bridge_from_env",
        return_value=runtime_bridge,
    ):
        runtime = script._build_running_multi_bot_runtime(
            {
                "TELEGRAM_OWNER_CHAT_ID": "777",
                "STATE_DB_PATH": str(tmp_path / "state.db"),
                "TELEGRAM_AGENT_TOKENS": (
                    "coordinator_agent=TELEGRAM_BOT_TOKEN,"
                    "writer_agent=TELEGRAM_WRITER_BOT_TOKEN"
                ),
                "TELEGRAM_BOT_TOKEN": "123:coord",
                "TELEGRAM_WRITER_BOT_TOKEN": "456:writer",
            },
            _ImmediateLoop(),
            ptb_runtime=_FakePTBRuntime(lambda token: _FakeApplication(token)),
        )

    assert runtime is not None
    coordinator_app = runtime.applications_by_role[COORDINATOR_ROLE].application
    writer_app = runtime.applications_by_role["writer_agent"].application
    fake_future = MagicMock()
    fake_future.result.return_value = None

    with patch(
        "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
        side_effect=_close_coro_and_return(fake_future),
    ):
        result = runtime.bridge.primary_bridge.handle(
            IncomingMessage(
                chat_id=777,
                user_id=777,
                message_id=3,
                text="что-то странное",
            )
        )

    assert result.handled is True
    coordinator_app.bot.send_message.assert_called_once()
    writer_app.bot.send_message.assert_not_called()
    sent_text = coordinator_app.bot.send_message.call_args.kwargs["text"]
    assert "Координатор:" in sent_text
    assert "[неизвестная роль 'ghost_agent']" in sent_text


def test_build_running_multi_bot_runtime_wires_envelope_progress_sender_into_bridge(
    tmp_path,
):
    captured: dict[str, object] = {}

    def _fake_build_multi_bot_bridge(
        _env,
        *,
        send_callable,
        send_progress_callable=None,
        send_progress_envelope_callable=None,
    ):
        captured["send_progress_callable"] = send_progress_callable
        captured["send_progress_envelope_callable"] = send_progress_envelope_callable
        captured["send_callable"] = send_callable
        return _multi_bridge()

    with patch(
        "scripts.run_telegram_bot.build_multi_bot_bridge_from_env",
        side_effect=_fake_build_multi_bot_bridge,
    ):
        runtime = script._build_running_multi_bot_runtime(
            {
                "TELEGRAM_OWNER_CHAT_ID": "777",
                "STATE_DB_PATH": str(tmp_path / "state.db"),
                "TELEGRAM_AGENT_TOKENS": (
                    "coordinator_agent=TELEGRAM_BOT_TOKEN,"
                    "writer_agent=TELEGRAM_WRITER_BOT_TOKEN"
                ),
                "TELEGRAM_BOT_TOKEN": "123:coord",
                "TELEGRAM_WRITER_BOT_TOKEN": "456:writer",
            },
            _ImmediateLoop(),
            ptb_runtime=_FakePTBRuntime(lambda token: _FakeApplication(token)),
        )

    assert runtime is not None
    assert callable(captured["send_progress_callable"])
    assert callable(captured["send_progress_envelope_callable"])


def test_multi_bot_progress_sender_routes_writer_envelope_through_writer_application(
    tmp_path,
):
    runtime = script._build_running_multi_bot_runtime(
        {
            "TELEGRAM_OWNER_CHAT_ID": "777",
            "STATE_DB_PATH": str(tmp_path / "state.db"),
            "TELEGRAM_AGENT_TOKENS": (
                "coordinator_agent=TELEGRAM_BOT_TOKEN,"
                "writer_agent=TELEGRAM_WRITER_BOT_TOKEN"
            ),
            "TELEGRAM_BOT_TOKEN": "123:coord",
            "TELEGRAM_WRITER_BOT_TOKEN": "456:writer",
        },
        _ImmediateLoop(),
        ptb_runtime=_FakePTBRuntime(lambda token: _FakeApplication(token)),
    )
    assert runtime is not None
    coordinator_app = runtime.applications_by_role[COORDINATOR_ROLE].application
    writer_app = runtime.applications_by_role["writer_agent"].application
    fake_future = MagicMock()

    with patch(
        "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
        side_effect=_close_coro_and_return(fake_future),
    ):
        used_role = runtime.progress_sender.send(
            OutgoingEnvelope(
                message=OutgoingMessage(chat_id=-100123, text="Программист: update"),
                sender_role="writer_agent",
            )
        )

    assert used_role == "writer_agent"
    writer_app.bot.send_message.assert_called_once_with(
        chat_id=-100123,
        text="Программист: update",
        reply_to_message_id=None,
    )
    coordinator_app.bot.send_message.assert_not_called()
    fake_future.result.assert_not_called()


def test_multi_bot_progress_sender_falls_back_to_coordinator_for_unknown_role(
    tmp_path,
):
    runtime = script._build_running_multi_bot_runtime(
        {
            "TELEGRAM_OWNER_CHAT_ID": "777",
            "STATE_DB_PATH": str(tmp_path / "state.db"),
            "TELEGRAM_AGENT_TOKENS": (
                "coordinator_agent=TELEGRAM_BOT_TOKEN,"
                "writer_agent=TELEGRAM_WRITER_BOT_TOKEN"
            ),
            "TELEGRAM_BOT_TOKEN": "123:coord",
            "TELEGRAM_WRITER_BOT_TOKEN": "456:writer",
        },
        _ImmediateLoop(),
        ptb_runtime=_FakePTBRuntime(lambda token: _FakeApplication(token)),
    )
    assert runtime is not None
    coordinator_app = runtime.applications_by_role[COORDINATOR_ROLE].application
    writer_app = runtime.applications_by_role["writer_agent"].application
    fake_future = MagicMock()

    with patch(
        "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
        side_effect=_close_coro_and_return(fake_future),
    ):
        used_role = runtime.progress_sender.send(
            OutgoingEnvelope(
                message=OutgoingMessage(chat_id=-100123, text="Архитектор: update"),
                sender_role="architect_agent",
            )
        )

    assert used_role == COORDINATOR_ROLE
    coordinator_app.bot.send_message.assert_called_once_with(
        chat_id=-100123,
        text="Архитектор: update",
        reply_to_message_id=None,
    )
    writer_app.bot.send_message.assert_not_called()
    fake_future.result.assert_not_called()


def test_multi_bot_handler_routes_coordinator_inbound_through_multi_bot_bridge(tmp_path):
    runtime = script._build_running_multi_bot_runtime(
        {
            "TELEGRAM_OWNER_CHAT_ID": "777",
            "STATE_DB_PATH": str(tmp_path / "state.db"),
            "TELEGRAM_AGENT_TOKENS": (
                "coordinator_agent=TELEGRAM_BOT_TOKEN,"
                "writer_agent=TELEGRAM_WRITER_BOT_TOKEN"
            ),
            "TELEGRAM_BOT_TOKEN": "123:coord",
            "TELEGRAM_WRITER_BOT_TOKEN": "456:writer",
        },
        _ImmediateLoop(),
        ptb_runtime=_FakePTBRuntime(lambda token: _FakeApplication(token)),
    )
    assert runtime is not None
    handler = runtime.applications_by_role[COORDINATOR_ROLE].application.handlers[0]
    update = SimpleNamespace(
        message=SimpleNamespace(
            chat=SimpleNamespace(id=777),
            from_user=SimpleNamespace(id=777),
            message_id=1,
            text="сделай задачу",
            caption=None,
            voice=None,
            photo=None,
            date=None,
        )
    )
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

    with patch.object(
        script.MultiBotBridge,
        "handle_incoming",
        autospec=True,
        return_value=BridgeResult(
            chat_id=777,
            handled=True,
            reason="ok",
            sent_count=1,
            extracted_text="сделай задачу",
        ),
    ) as mock_handle:
        asyncio.run(handler.callback(update, context))

    assert mock_handle.call_count == 1
    _self, call_role, incoming = mock_handle.call_args.args
    assert call_role == COORDINATOR_ROLE
    assert isinstance(incoming, IncomingMessage)
    assert incoming.text == "сделай задачу"


def test_multi_bot_handler_routes_secondary_inbound_without_fake_reply(tmp_path):
    runtime = script._build_running_multi_bot_runtime(
        {
            "TELEGRAM_OWNER_CHAT_ID": "777",
            "STATE_DB_PATH": str(tmp_path / "state.db"),
            "TELEGRAM_AGENT_TOKENS": (
                "coordinator_agent=TELEGRAM_BOT_TOKEN,"
                "writer_agent=TELEGRAM_WRITER_BOT_TOKEN"
            ),
            "TELEGRAM_BOT_TOKEN": "123:coord",
            "TELEGRAM_WRITER_BOT_TOKEN": "456:writer",
        },
        _ImmediateLoop(),
        ptb_runtime=_FakePTBRuntime(lambda token: _FakeApplication(token)),
    )
    assert runtime is not None
    handler = runtime.applications_by_role["writer_agent"].application.handlers[0]
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    update = SimpleNamespace(
        message=SimpleNamespace(
            chat=SimpleNamespace(id=777),
            from_user=SimpleNamespace(id=777),
            message_id=2,
            text="ping",
            caption=None,
            voice=None,
            photo=None,
            date=None,
        )
    )

    with patch.object(
        script.MultiBotBridge,
        "handle_incoming",
        autospec=True,
        return_value=BridgeResult(
            chat_id=777,
            handled=False,
            reason="secondary_bot_inbound_not_enabled",
            sent_count=0,
            extracted_text="ping",
        ),
    ) as mock_handle:
        asyncio.run(handler.callback(update, context))

    assert mock_handle.call_count == 1
    _self, call_role, _incoming = mock_handle.call_args.args
    assert call_role == "writer_agent"
    context.bot.send_message.assert_not_called()


def test_multi_bot_handler_suppresses_attachment_error_reply_for_secondary_role(
    tmp_path,
):
    runtime = script._build_running_multi_bot_runtime(
        {
            "TELEGRAM_OWNER_CHAT_ID": "777",
            "STATE_DB_PATH": str(tmp_path / "state.db"),
            "TELEGRAM_AGENT_TOKENS": (
                "coordinator_agent=TELEGRAM_BOT_TOKEN,"
                "writer_agent=TELEGRAM_WRITER_BOT_TOKEN"
            ),
            "TELEGRAM_BOT_TOKEN": "123:coord",
            "TELEGRAM_WRITER_BOT_TOKEN": "456:writer",
        },
        _ImmediateLoop(),
        ptb_runtime=_FakePTBRuntime(lambda token: _FakeApplication(token)),
    )
    assert runtime is not None
    handler = runtime.applications_by_role["writer_agent"].application.handlers[0]
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    update = SimpleNamespace(
        message=SimpleNamespace(
            chat=SimpleNamespace(id=777),
            from_user=SimpleNamespace(id=777),
            message_id=3,
            text=None,
            caption=None,
            voice=object(),
            photo=None,
            date=None,
        )
    )

    with (
        patch(
            "scripts.run_telegram_bot._download_voice",
            side_effect=RuntimeError("boom"),
        ),
        patch.object(
            script.MultiBotBridge,
            "handle_incoming",
            autospec=True,
        ) as mock_handle,
    ):
        asyncio.run(handler.callback(update, context))

    context.bot.send_message.assert_not_called()
    mock_handle.assert_not_called()


def test_start_and_shutdown_running_multi_bot_runtime_cover_all_apps(tmp_path):
    runtime = script._build_running_multi_bot_runtime(
        {
            "TELEGRAM_OWNER_CHAT_ID": "777",
            "STATE_DB_PATH": str(tmp_path / "state.db"),
            "TELEGRAM_AGENT_TOKENS": (
                "coordinator_agent=TELEGRAM_BOT_TOKEN,"
                "writer_agent=TELEGRAM_WRITER_BOT_TOKEN"
            ),
            "TELEGRAM_BOT_TOKEN": "123:coord",
            "TELEGRAM_WRITER_BOT_TOKEN": "456:writer",
        },
        _ImmediateLoop(),
        ptb_runtime=_FakePTBRuntime(lambda token: _FakeApplication(token)),
    )
    assert runtime is not None

    asyncio.run(script._start_running_multi_bot_runtime(runtime))
    asyncio.run(script._shutdown_running_multi_bot_runtime(runtime))

    for running in runtime.applications_by_role.values():
        running.application.initialize.assert_awaited_once()
        running.application.start.assert_awaited_once()
        running.application.updater.start_polling.assert_awaited_once()
        running.application.updater.stop.assert_awaited_once()
        running.application.stop.assert_awaited_once()
        running.application.shutdown.assert_awaited_once()


def test_start_running_multi_bot_runtime_rolls_back_when_second_app_start_fails(tmp_path):
    built_apps: dict[str, _FakeApplication] = {}
    token_roles = {
        "123:coord": COORDINATOR_ROLE,
        "456:writer": "writer_agent",
    }

    def _app_factory(token):
        role = token_roles[token]
        app = _FakeApplication(token, fail_start=(role == "writer_agent"))
        built_apps[role] = app
        return app

    runtime = script._build_running_multi_bot_runtime(
        {
            "TELEGRAM_OWNER_CHAT_ID": "777",
            "STATE_DB_PATH": str(tmp_path / "state.db"),
            "TELEGRAM_AGENT_TOKENS": (
                "coordinator_agent=TELEGRAM_BOT_TOKEN,"
                "writer_agent=TELEGRAM_WRITER_BOT_TOKEN"
            ),
            "TELEGRAM_BOT_TOKEN": "123:coord",
            "TELEGRAM_WRITER_BOT_TOKEN": "456:writer",
        },
        _ImmediateLoop(),
        ptb_runtime=_FakePTBRuntime(_app_factory),
    )
    assert runtime is not None

    with pytest.raises(
        RuntimeError,
        match="multi_bot_application_start_failed:writer_agent",
    ):
        asyncio.run(script._start_running_multi_bot_runtime(runtime))

    built_apps[COORDINATOR_ROLE].updater.stop.assert_awaited_once()
    built_apps[COORDINATOR_ROLE].stop.assert_awaited_once()
    built_apps[COORDINATOR_ROLE].shutdown.assert_awaited_once()
    built_apps["writer_agent"].updater.stop.assert_not_awaited()
    built_apps["writer_agent"].stop.assert_not_awaited()
    built_apps["writer_agent"].shutdown.assert_awaited_once()


def test_start_running_multi_bot_runtime_rolls_back_when_second_app_polling_fails(
    tmp_path,
):
    built_apps: dict[str, _FakeApplication] = {}
    token_roles = {
        "123:coord": COORDINATOR_ROLE,
        "456:writer": "writer_agent",
    }

    def _app_factory(token):
        role = token_roles[token]
        app = _FakeApplication(token, fail_polling=(role == "writer_agent"))
        built_apps[role] = app
        return app

    runtime = script._build_running_multi_bot_runtime(
        {
            "TELEGRAM_OWNER_CHAT_ID": "777",
            "STATE_DB_PATH": str(tmp_path / "state.db"),
            "TELEGRAM_AGENT_TOKENS": (
                "coordinator_agent=TELEGRAM_BOT_TOKEN,"
                "writer_agent=TELEGRAM_WRITER_BOT_TOKEN"
            ),
            "TELEGRAM_BOT_TOKEN": "123:coord",
            "TELEGRAM_WRITER_BOT_TOKEN": "456:writer",
        },
        _ImmediateLoop(),
        ptb_runtime=_FakePTBRuntime(_app_factory),
    )
    assert runtime is not None

    with pytest.raises(
        RuntimeError,
        match="multi_bot_application_polling_failed:writer_agent",
    ):
        asyncio.run(script._start_running_multi_bot_runtime(runtime))

    built_apps[COORDINATOR_ROLE].updater.stop.assert_awaited_once()
    built_apps[COORDINATOR_ROLE].stop.assert_awaited_once()
    built_apps[COORDINATOR_ROLE].shutdown.assert_awaited_once()
    built_apps["writer_agent"].updater.stop.assert_awaited_once()
    built_apps["writer_agent"].stop.assert_awaited_once()
    built_apps["writer_agent"].shutdown.assert_awaited_once()


def test_main_multi_bot_mode_starts_multiple_applications_and_cleans_orphans_once(tmp_path):
    built_apps: list[_FakeApplication] = []

    def _app_factory(token):
        app = _FakeApplication(token)
        built_apps.append(app)
        return app

    env = {
        "TELEGRAM_OWNER_CHAT_ID": "777",
        "STATE_DB_PATH": str(tmp_path / "state.db"),
        "TELEGRAM_AGENT_TOKENS": (
            "coordinator_agent=TELEGRAM_BOT_TOKEN,"
            "writer_agent=TELEGRAM_WRITER_BOT_TOKEN"
        ),
        "TELEGRAM_BOT_TOKEN": "123:coord",
        "TELEGRAM_WRITER_BOT_TOKEN": "456:writer",
    }

    class _ImmediateEvent:
        async def wait(self):
            return None

    with (
        patch("scripts.run_telegram_bot.load_dotenv"),
        patch("scripts.run_telegram_bot.os.environ", env),
        patch(
            "scripts.run_telegram_bot._load_ptb_runtime",
            return_value=_FakePTBRuntime(_app_factory),
        ),
        patch(
            "core.bot_runner.cleanup_orphan_worktrees_from_env",
            return_value=2,
        ) as mock_cleanup,
        patch("scripts.run_telegram_bot.asyncio.Event", _ImmediateEvent),
    ):
        rc = asyncio.run(script.main([]))

    assert rc == 0
    assert len(built_apps) == 2
    assert [app.token for app in built_apps] == ["123:coord", "456:writer"]
    mock_cleanup.assert_called_once_with(env)
    for app in built_apps:
        app.initialize.assert_awaited_once()
        app.start.assert_awaited_once()
        app.updater.start_polling.assert_awaited_once()
        app.updater.stop.assert_awaited_once()
        app.stop.assert_awaited_once()
        app.shutdown.assert_awaited_once()


def test_main_multi_bot_start_failure_returns_error_and_stops_started_apps(tmp_path):
    built_apps: dict[str, _FakeApplication] = {}
    token_roles = {
        "123:coord": COORDINATOR_ROLE,
        "456:writer": "writer_agent",
    }

    def _app_factory(token):
        role = token_roles[token]
        app = _FakeApplication(token, fail_start=(role == "writer_agent"))
        built_apps[role] = app
        return app

    env = {
        "TELEGRAM_OWNER_CHAT_ID": "777",
        "STATE_DB_PATH": str(tmp_path / "state.db"),
        "TELEGRAM_AGENT_TOKENS": (
            "coordinator_agent=TELEGRAM_BOT_TOKEN,"
            "writer_agent=TELEGRAM_WRITER_BOT_TOKEN"
        ),
        "TELEGRAM_BOT_TOKEN": "123:coord",
        "TELEGRAM_WRITER_BOT_TOKEN": "456:writer",
    }

    with (
        patch("scripts.run_telegram_bot.load_dotenv"),
        patch("scripts.run_telegram_bot.os.environ", env),
        patch(
            "scripts.run_telegram_bot._load_ptb_runtime",
            return_value=_FakePTBRuntime(_app_factory),
        ),
        patch(
            "core.bot_runner.cleanup_orphan_worktrees_from_env",
            return_value=0,
        ),
    ):
        rc = asyncio.run(script.main([]))

    assert rc == 5
    built_apps[COORDINATOR_ROLE].updater.stop.assert_awaited_once()
    built_apps[COORDINATOR_ROLE].stop.assert_awaited_once()
    built_apps[COORDINATOR_ROLE].shutdown.assert_awaited_once()


def test_main_legacy_mode_still_builds_single_application(tmp_path):
    built_apps: list[_FakeApplication] = []

    def _app_factory(token):
        app = _FakeApplication(token)
        built_apps.append(app)
        return app

    fake_bridge = SimpleNamespace(handle=MagicMock())
    env = {
        "TELEGRAM_OWNER_CHAT_ID": "777",
        "STATE_DB_PATH": str(tmp_path / "state.db"),
        "TELEGRAM_BOT_TOKEN": "123:legacy",
    }

    class _ImmediateEvent:
        async def wait(self):
            return None

    with (
        patch("scripts.run_telegram_bot.load_dotenv"),
        patch("scripts.run_telegram_bot.os.environ", env),
        patch(
            "scripts.run_telegram_bot._load_ptb_runtime",
            return_value=_FakePTBRuntime(_app_factory),
        ),
        patch(
            "scripts.run_telegram_bot.build_bridge_from_env",
            return_value=fake_bridge,
        ) as mock_build_bridge,
        patch(
            "core.bot_runner.cleanup_orphan_worktrees_from_env",
            return_value=0,
        ),
        patch("scripts.run_telegram_bot.asyncio.Event", _ImmediateEvent),
    ):
        rc = asyncio.run(script.main([]))

    assert rc == 0
    assert len(built_apps) == 1
    assert built_apps[0].token == "123:legacy"
    mock_build_bridge.assert_called_once()
