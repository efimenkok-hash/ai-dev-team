from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

import scripts.run_telegram_bot as script
from core.agent_personas import default_registry
from core.coordinator_role import COORDINATOR_ROLE
from core.multi_bot_bridge import MultiBotBridge
from core.multi_bot_runtime import BotIdentity, MultiBotRuntimeSpec, PerRoleBotMap
from core.progress_emitter import ProgressEvent
from core.project_chat_posting import (
    ProjectChatPostingContext,
    ProjectChatPostingService,
)
from core.project_models import Project, ProjectChatBinding, ProjectPolicy
from core.project_registry import ProjectSnapshot
from core.project_runtime import ProjectRuntimeBinding
from core.telegram_bridge import (
    BridgeReply,
    OutgoingEnvelope,
    OutgoingMessage,
    TelegramBridge,
)
from tests.support.mock_ptb import (
    ImmediateExecutorLoop,
    InvalidToken,
    MockPtbApplication,
    MockPtbApplicationSpec,
    MockPtbBot,
    MockPtbMessageHandler,
    MockPtbRuntime,
    MockPtbUpdateFactory,
)


def _close_coro_and_return(fake_future):
    def _submit(coro, _loop):
        coro.close()
        return fake_future

    return _submit


def _identity(
    role: str = COORDINATOR_ROLE,
    *,
    token_env_key: str | None = None,
    token: str = "123:token",
) -> BotIdentity:
    return BotIdentity(
        bot_id=role,
        agent_role=role,
        token_env_key=token_env_key or f"TELEGRAM_{role.upper()}_TOKEN",
        token=token,
    )


def _role_map(*identities: BotIdentity) -> PerRoleBotMap:
    return PerRoleBotMap(
        {identity.agent_role: identity for identity in identities}
    )


def _runtime_env(tmp_path) -> dict[str, str]:
    return {
        "TELEGRAM_OWNER_CHAT_ID": "777",
        "STATE_DB_PATH": str(tmp_path / "state.db"),
        "TELEGRAM_AGENT_TOKENS": (
            "coordinator_agent=TELEGRAM_BOT_TOKEN,"
            "writer_agent=TELEGRAM_WRITER_BOT_TOKEN,"
            "reviewer_agent=TELEGRAM_REVIEWER_BOT_TOKEN"
        ),
        "TELEGRAM_BOT_TOKEN": "123:coord",
        "TELEGRAM_WRITER_BOT_TOKEN": "456:writer",
        "TELEGRAM_REVIEWER_BOT_TOKEN": "789:reviewer",
    }


def _make_bridge(task_handler) -> MultiBotBridge:
    coordinator = _identity(
        token_env_key="TELEGRAM_BOT_TOKEN",
        token="123:coord",
    )
    writer = _identity(
        "writer_agent",
        token_env_key="TELEGRAM_WRITER_BOT_TOKEN",
        token="456:writer",
    )
    reviewer = _identity(
        "reviewer_agent",
        token_env_key="TELEGRAM_REVIEWER_BOT_TOKEN",
        token="789:reviewer",
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
        runtime_spec=MultiBotRuntimeSpec(
            primary_bot=coordinator,
            role_map=_role_map(coordinator, writer, reviewer),
            source="telegram_agent_tokens",
        ),
        primary_bridge=primary_bridge,
    )


def _build_runtime(
    tmp_path,
    *,
    ptb_runtime: MockPtbRuntime | None = None,
    bridge: MultiBotBridge | None = None,
) -> script.RunningMultiBotRuntime:
    resolved_ptb_runtime = ptb_runtime or MockPtbRuntime()
    resolved_bridge = bridge or _make_bridge(
        lambda _text, _msg: BridgeReply(
            persona_role=COORDINATOR_ROLE,
            body="принял",
        )
    )
    with patch(
        "scripts.run_telegram_bot.build_multi_bot_bridge_from_env",
        return_value=resolved_bridge,
    ):
        runtime = script._build_running_multi_bot_runtime(
            _runtime_env(tmp_path),
            ImmediateExecutorLoop(),
            ptb_runtime=resolved_ptb_runtime,
        )
    assert runtime is not None
    return runtime


def _project_snapshot(tmp_path) -> ProjectSnapshot:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    project_id = "alpha_project"
    return ProjectSnapshot(
        project=Project(
            project_id=project_id,
            slug="alpha-project",
            name="Alpha Project",
            description="Mock PTB runtime project",
            owner_user_id=777,
            status="active",
        ),
        policy=ProjectPolicy(
            project_id=project_id,
            allow_hiring=True,
            allow_agent_dm=False,
            require_owner_approval_for_hires=True,
        ),
        chat_binding=ProjectChatBinding(
            project_id=project_id,
            chat_id=-1001234567890,
            chat_provider="telegram",
        ),
        runtime_binding=ProjectRuntimeBinding(
            project_id=project_id,
            adapter_name="beta_adapter",
            repo_path=repo,
        ),
    )


def test_mock_ptb_support_layer_records_bot_calls_and_dispatches_handlers():
    bot = MockPtbBot(user_id=7001, username="writer_bot")
    me = asyncio.run(bot.get_me())
    asyncio.run(
        bot.send_message(chat_id=77, text="hello", reply_to_message_id=9)
    )

    called: list[tuple[object, object]] = []

    async def _callback(update, context):
        called.append((update, context))

    app = MockPtbApplication("123:coord")
    handler = MockPtbMessageHandler(filters=object(), callback=_callback)
    app.add_handler(handler)
    update = MockPtbUpdateFactory.text(
        chat_id=77,
        user_id=77,
        text="ping",
        message_id=1,
    )
    asyncio.run(app.dispatch_update(update))

    assert me.id == 7001
    assert me.username == "writer_bot"
    assert bot.sent_messages == [
        {
            "chat_id": 77,
            "text": "hello",
            "reply_to_message_id": 9,
        }
    ]
    assert len(called) == 1
    assert called[0][1].bot is app.bot


def test_mock_ptb_runtime_builds_per_role_applications_truthfully(tmp_path):
    ptb_runtime = MockPtbRuntime(
        app_specs_by_token={
            "123:coord": MockPtbApplicationSpec(bot_username="coord_bot"),
            "456:writer": MockPtbApplicationSpec(bot_username="writer_bot"),
            "789:reviewer": MockPtbApplicationSpec(
                bot_username="reviewer_bot"
            ),
        }
    )

    runtime = _build_runtime(tmp_path, ptb_runtime=ptb_runtime)

    assert ptb_runtime.built_tokens == [
        "123:coord",
        "789:reviewer",
        "456:writer",
    ]
    assert tuple(runtime.applications_by_role.keys()) == (
        COORDINATOR_ROLE,
        "reviewer_agent",
        "writer_agent",
    )
    for running in runtime.applications_by_role.values():
        assert len(running.application.handlers) == 1


def test_coordinator_inbound_reaches_bridge_through_registered_handler(tmp_path):
    runtime = _build_runtime(
        tmp_path,
        bridge=_make_bridge(
            lambda _text, _msg: BridgeReply(
                persona_role=COORDINATOR_ROLE,
                body="взял в работу",
            )
        ),
    )
    coordinator_app = runtime.applications_by_role[COORDINATOR_ROLE].application
    update = MockPtbUpdateFactory.text(
        chat_id=-100500,
        user_id=777,
        text="сделай задачу",
        message_id=10,
    )
    captured: dict[str, object] = {}
    fake_future = MagicMock()
    fake_future.result.return_value = None
    original_handle = MultiBotBridge.handle_incoming

    def _spy(self, role, incoming):
        captured["role"] = role
        captured["incoming"] = incoming
        result = original_handle(self, role, incoming)
        captured["result"] = result
        return result

    with (
        patch.object(
            MultiBotBridge,
            "handle_incoming",
            autospec=True,
            side_effect=_spy,
        ) as mock_handle,
        patch(
            "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
            side_effect=_close_coro_and_return(fake_future),
        ),
    ):
        asyncio.run(coordinator_app.dispatch_update(update))

    assert mock_handle.call_count == 1
    assert captured["role"] == COORDINATOR_ROLE
    incoming = captured["incoming"]
    assert incoming.text == "сделай задачу"
    assert incoming.incoming_bot_role == COORDINATOR_ROLE
    result = captured["result"]
    assert result.handled is True
    coordinator_app.bot.send_message.assert_called_once()
    sent = coordinator_app.bot.send_message.call_args.kwargs
    assert "Координатор:" in sent["text"]
    assert "взял в работу" in sent["text"]


def test_secondary_group_inbound_stays_disabled_without_fake_reply(tmp_path):
    runtime = _build_runtime(tmp_path)
    writer_app = runtime.applications_by_role["writer_agent"].application
    reviewer_app = runtime.applications_by_role["reviewer_agent"].application
    coordinator_app = runtime.applications_by_role[COORDINATOR_ROLE].application
    update = MockPtbUpdateFactory.text(
        chat_id=-100700,
        user_id=777,
        text="групповое сообщение",
        message_id=11,
    )
    captured: list[object] = []
    original_handle = MultiBotBridge.handle_incoming

    def _spy(self, role, incoming):
        result = original_handle(self, role, incoming)
        captured.append((role, incoming, result))
        return result

    with patch.object(
        MultiBotBridge,
        "handle_incoming",
        autospec=True,
        side_effect=_spy,
    ):
        asyncio.run(writer_app.dispatch_update(update))

    assert len(captured) == 1
    role, incoming, result = captured[0]
    assert role == "writer_agent"
    assert incoming.incoming_bot_role == "writer_agent"
    assert result.handled is False
    assert result.reason == "secondary_bot_inbound_not_enabled"
    writer_app.bot.send_message.assert_not_called()
    reviewer_app.bot.send_message.assert_not_called()
    coordinator_app.bot.send_message.assert_not_called()


@pytest.mark.parametrize("role", ["writer_agent", "reviewer_agent"])
def test_secondary_owner_private_dm_stays_on_same_identity_thread(
    tmp_path,
    role: str,
):
    runtime = _build_runtime(
        tmp_path,
        bridge=_make_bridge(
            lambda _text, _msg: BridgeReply(
                persona_role="architect_agent",
                body="готово",
            )
        ),
    )
    target_app = runtime.applications_by_role[role].application
    other_roles = tuple(r for r in runtime.applications_by_role if r != role)
    update = MockPtbUpdateFactory.text(
        chat_id=777,
        user_id=777,
        text="сделай черновик",
        message_id=12,
    )
    fake_future = MagicMock()
    fake_future.result.return_value = None
    captured: dict[str, object] = {}
    original_handle = MultiBotBridge.handle_incoming

    def _spy(self, call_role, incoming):
        captured["role"] = call_role
        captured["incoming"] = incoming
        result = original_handle(self, call_role, incoming)
        captured["result"] = result
        return result

    with (
        patch.object(
            MultiBotBridge,
            "handle_incoming",
            autospec=True,
            side_effect=_spy,
        ),
        patch(
            "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
            side_effect=_close_coro_and_return(fake_future),
        ),
    ):
        asyncio.run(target_app.dispatch_update(update))

    assert captured["role"] == role
    assert captured["incoming"].incoming_bot_role == role
    assert captured["result"].handled is True
    target_app.bot.send_message.assert_called_once()
    sent = target_app.bot.send_message.call_args.kwargs
    assert sent["text"] == "Архитектор: готово"
    for other_role in other_roles:
        runtime.applications_by_role[other_role].application.bot.send_message.assert_not_called()


def test_role_aware_outbound_uses_exact_delivery_role_and_coordinator_fallback(
    tmp_path,
):
    runtime = _build_runtime(tmp_path)
    fake_future = MagicMock()
    fake_future.result.return_value = None
    coordinator_app = runtime.applications_by_role[COORDINATOR_ROLE].application
    writer_app = runtime.applications_by_role["writer_agent"].application
    reviewer_app = runtime.applications_by_role["reviewer_agent"].application

    with patch(
        "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
        side_effect=_close_coro_and_return(fake_future),
    ):
        exact_role = runtime.outbound_sender.send(
            OutgoingEnvelope(
                message=OutgoingMessage(
                    chat_id=-1001,
                    text="Программист: черновик",
                ),
                sender_role="writer_agent",
            )
        )
        delivery_override = runtime.outbound_sender.send(
            OutgoingEnvelope(
                message=OutgoingMessage(
                    chat_id=-1002,
                    text="Координатор: отправь через reviewer",
                ),
                sender_role=COORDINATOR_ROLE,
                delivery_role="reviewer_agent",
            )
        )
        fallback_role = runtime.outbound_sender.send(
            OutgoingEnvelope(
                message=OutgoingMessage(
                    chat_id=-1003,
                    text="Архитектор: content stays intact",
                ),
                sender_role="ghost_agent",
            )
        )

    assert exact_role == "writer_agent"
    assert delivery_override == "reviewer_agent"
    assert fallback_role == COORDINATOR_ROLE
    assert writer_app.bot.send_message.call_args.kwargs["text"] == "Программист: черновик"
    assert reviewer_app.bot.send_message.call_args.kwargs["text"] == "Координатор: отправь через reviewer"
    assert coordinator_app.bot.send_message.call_args.kwargs["text"] == "Архитектор: content stays intact"


def test_multi_bot_startup_fails_fast_on_invalid_secondary_token_probe(tmp_path):
    ptb_runtime = MockPtbRuntime(
        app_specs_by_token={
            "123:coord": MockPtbApplicationSpec(bot_username="coord_bot"),
            "456:writer": MockPtbApplicationSpec(bot_username="writer_bot"),
            "789:reviewer": MockPtbApplicationSpec(
                fail_get_me=InvalidToken("bad token")
            ),
        }
    )
    runtime = _build_runtime(tmp_path, ptb_runtime=ptb_runtime)

    with pytest.raises(RuntimeError, match="bot_token_invalid:reviewer_agent"):
        asyncio.run(script._start_running_multi_bot_runtime(runtime))

    coordinator_app = runtime.applications_by_role[COORDINATOR_ROLE].application
    reviewer_app = runtime.applications_by_role["reviewer_agent"].application
    writer_app = runtime.applications_by_role["writer_agent"].application
    coordinator_app.bot.get_me.assert_awaited_once()
    reviewer_app.bot.get_me.assert_awaited_once()
    writer_app.bot.get_me.assert_not_awaited()
    coordinator_app.initialize.assert_not_awaited()
    reviewer_app.initialize.assert_not_awaited()
    writer_app.initialize.assert_not_awaited()
    assert runtime.lifecycle_report.states_by_role["reviewer_agent"].failure_reason == (
        "bot_token_invalid:reviewer_agent"
    )


def test_multi_bot_startup_rolls_back_on_polling_failure_with_truthful_report(
    tmp_path,
):
    ptb_runtime = MockPtbRuntime(
        app_specs_by_token={
            "123:coord": MockPtbApplicationSpec(bot_username="coord_bot"),
            "456:writer": MockPtbApplicationSpec(bot_username="writer_bot"),
            "789:reviewer": MockPtbApplicationSpec(
                fail_polling=RuntimeError("polling failed")
            ),
        }
    )
    runtime = _build_runtime(tmp_path, ptb_runtime=ptb_runtime)

    with pytest.raises(
        RuntimeError,
        match="multi_bot_application_polling_failed:reviewer_agent",
    ):
        asyncio.run(script._start_running_multi_bot_runtime(runtime))

    coordinator_app = runtime.applications_by_role[COORDINATOR_ROLE].application
    reviewer_app = runtime.applications_by_role["reviewer_agent"].application
    writer_app = runtime.applications_by_role["writer_agent"].application
    coordinator_app.updater.stop.assert_awaited_once()
    coordinator_app.stop.assert_awaited_once()
    coordinator_app.shutdown.assert_awaited_once()
    reviewer_app.updater.stop.assert_awaited_once()
    reviewer_app.stop.assert_awaited_once()
    reviewer_app.shutdown.assert_awaited_once()
    writer_app.initialize.assert_not_awaited()
    assert runtime.lifecycle_report.states_by_role[COORDINATOR_ROLE].polling_started is True
    assert runtime.lifecycle_report.states_by_role["reviewer_agent"].started is True
    assert runtime.lifecycle_report.states_by_role["reviewer_agent"].polling_started is False
    assert runtime.lifecycle_report.states_by_role["reviewer_agent"].failure_reason == (
        "multi_bot_application_polling_failed:reviewer_agent"
    )


def test_project_chat_posting_semantics_survive_through_mock_ptb_transport(
    tmp_path,
):
    runtime = _build_runtime(tmp_path)
    posting_service = ProjectChatPostingService()
    context = ProjectChatPostingContext(
        snapshot=_project_snapshot(tmp_path),
        chat_id=-1001234567890,
        context_source="bound_chat",
    )
    fake_future = MagicMock()

    with patch(
        "scripts.run_telegram_bot.asyncio.run_coroutine_threadsafe",
        side_effect=_close_coro_and_return(fake_future),
    ):
        writer_role = runtime.progress_sender.send(
            posting_service.build_event_envelope(
                context,
                ProgressEvent(
                    kind="agent_started",
                    timestamp=1.0,
                    agent_role="writer_agent",
                ),
            )
        )
        reviewer_role = runtime.progress_sender.send(
            posting_service.build_event_envelope(
                context,
                ProgressEvent(
                    kind="agent_finished",
                    timestamp=2.0,
                    agent_role="reviewer_agent",
                    duration_ms=25,
                ),
            )
        )
        coordinator_role = runtime.progress_sender.send(
            posting_service.build_terminal_envelope(
                context,
                "✅ Готово",
            )
        )

    coordinator_app = runtime.applications_by_role[COORDINATOR_ROLE].application
    writer_app = runtime.applications_by_role["writer_agent"].application
    reviewer_app = runtime.applications_by_role["reviewer_agent"].application

    assert writer_role == "writer_agent"
    assert reviewer_role == "reviewer_agent"
    assert coordinator_role == COORDINATOR_ROLE
    assert writer_app.bot.send_message.call_args.kwargs["text"] == "▶︎ writer_agent начал"
    assert reviewer_app.bot.send_message.call_args.kwargs["text"] == "✓ reviewer_agent закончил (25 мс)"
    assert coordinator_app.bot.send_message.call_args.kwargs["text"] == "✅ Готово"
