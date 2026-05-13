from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from core.agent_dm_models import AgentDmMessage, AgentDmSession
from core.agent_owner_notifications import (
    AgentOwnerNotificationDispatchResult,
    AgentOwnerNotificationRequest,
    AgentOwnerNotificationService,
)
from core.bot_runner import build_bridge_from_env
from core.coordinator_role import COORDINATOR_ROLE
from core.llm_dispatcher import LLMAttempt, LLMDispatcher, LLMDispatchError, LLMResponse
from core.multi_bot_bridge import MultiBotBridge
from core.multi_bot_runtime import BotIdentity, MultiBotRuntimeSpec, PerRoleBotMap
from core.project_models import Project, ProjectChatBinding, ProjectPolicy
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.state_db import StateDB
from core.telegram_bridge import IncomingMessage, OutgoingEnvelope
from core.vision_client import VisionResult
from core.whisper_client import TranscriptionResult

OWNER_ID = 101
OTHER_OWNER_ID = 202
BOUND_CHAT_ID = -100123450001


class EnvelopeCapture:
    def __init__(self, *, fail_call_index: int | None = None) -> None:
        self.fail_call_index = fail_call_index
        self.sent: list[OutgoingEnvelope] = []
        self.call_count = 0

    def __call__(self, envelope: OutgoingEnvelope) -> None:
        self.call_count += 1
        if (
            self.fail_call_index is not None
            and self.call_count == self.fail_call_index
        ):
            raise RuntimeError("simulated_send_failure")
        self.sent.append(envelope)


class ClockSequence:
    def __init__(self, *values: float) -> None:
        if not values:
            raise ValueError("clock_sequence_requires_values")
        self._values = list(values)
        self._index = 0

    def __call__(self) -> float:
        if self._index < len(self._values):
            value = self._values[self._index]
            self._index += 1
            return value
        return self._values[-1]


class FakeWhisper:
    def __init__(self, result_text: str) -> None:
        self.result_text = result_text

    def transcribe(
        self,
        audio_bytes: bytes,
        *,
        mime_type: str,
        filename: str,
        language: str | None = None,
    ) -> TranscriptionResult:
        return TranscriptionResult(
            text=self.result_text,
            duration_seconds=3.0,
            cost_usd=0.0003,
            cost_estimated=False,
            language=language or "ru",
        )


class FakeVision:
    def __init__(self, result_text: str) -> None:
        self.result_text = result_text

    def describe(
        self,
        image_bytes: bytes,
        *,
        mime_type: str,
        prompt: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> VisionResult:
        return VisionResult(
            text=self.result_text,
            model=model or "openai/gpt-4o-mini",
            prompt_tokens=100,
            completion_tokens=20,
        )


def _db(tmp_path):
    return StateDB(tmp_path / "state.db")


def _project(**overrides) -> Project:
    data = {
        "project_id": "alpha_project",
        "slug": "alpha-project",
        "name": "Alpha Project",
        "description": "Primary AI Office project.",
        "owner_user_id": OWNER_ID,
        "status": "active",
    }
    data.update(overrides)
    return Project(**data)


def _policy(project_id: str = "alpha_project", **overrides) -> ProjectPolicy:
    data = {
        "project_id": project_id,
        "allow_hiring": True,
        "allow_agent_dm": True,
        "require_owner_approval_for_hires": True,
    }
    data.update(overrides)
    return ProjectPolicy(**data)


def _binding(project_id: str = "alpha_project", **overrides) -> ProjectChatBinding:
    data = {
        "project_id": project_id,
        "chat_provider": "telegram",
        "chat_id": BOUND_CHAT_ID,
    }
    data.update(overrides)
    return ProjectChatBinding(**data)


def _snapshot(
    *,
    project: Project | None = None,
    policy: ProjectPolicy | None = None,
    chat_binding: ProjectChatBinding | None = None,
) -> ProjectSnapshot:
    return ProjectSnapshot(
        project=_project() if project is None else project,
        policy=_policy() if policy is None else policy,
        chat_binding=chat_binding,
    )


def _response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        model_used="model-x",
        prompt_tokens=10,
        completion_tokens=5,
        attempts=(
            LLMAttempt(model="model-x", ok=True, reason="ok", duration_ms=1),
        ),
    )


def _dispatch_error() -> LLMDispatchError:
    return LLMDispatchError(
        "chain_exhausted",
        "all models failed",
        (LLMAttempt(model="model-x", ok=False, reason="timeout", duration_ms=1),),
    )


def _dispatcher_with_responses(*texts: str) -> LLMDispatcher:
    dispatcher = LLMDispatcher(api_key="sk-test")
    dispatcher.dispatch = MagicMock(  # type: ignore[method-assign]
        side_effect=[_response(text) for text in texts]
    )
    return dispatcher


def _dispatch_content(dispatcher: LLMDispatcher, call_index: int = -1) -> str:
    request = dispatcher.dispatch.call_args_list[call_index].args[0]
    return "\n".join(message["content"] for message in request.messages)


def _incoming_dm(
    *,
    text: str | None = "Подскажи по API",
    incoming_bot_role: str = "writer_agent",
    message_id: int = 1,
    project_id: str | None = None,
    project_slug: str | None = None,
    project_context_source: str | None = None,
    project_context_reason: str | None = None,
    voice_bytes: bytes | None = None,
    photo_bytes: bytes | None = None,
) -> IncomingMessage:
    return IncomingMessage(
        chat_id=OWNER_ID,
        user_id=OWNER_ID,
        message_id=message_id,
        text=text,
        voice_bytes=voice_bytes,
        photo_bytes=photo_bytes,
        project_id=project_id,
        project_slug=project_slug,
        project_context_source=project_context_source,
        project_context_reason=project_context_reason,
        incoming_bot_role=incoming_bot_role,
    )


def _bound_chat_message(
    *,
    text: str = "group task",
    incoming_bot_role: str = "writer_agent",
    message_id: int = 1,
) -> IncomingMessage:
    return IncomingMessage(
        chat_id=BOUND_CHAT_ID,
        user_id=OWNER_ID,
        message_id=message_id,
        text=text,
        incoming_bot_role=incoming_bot_role,
    )


def _seed_transcript(
    db: StateDB,
    *,
    owner_user_id: int,
    project_id: str,
    agent_role: str,
    thread_bot_role: str,
    bodies: tuple[str, ...],
    session_status: str = "active",
    created_at: float = 200.0,
) -> None:
    db.upsert_agent_dm_session(
        AgentDmSession(
            owner_user_id=owner_user_id,
            project_id=project_id,
            agent_role=agent_role,
            thread_bot_role=thread_bot_role,
            dm_chat_id=owner_user_id,
            status="active",
            created_at=created_at,
            last_interaction_at=created_at,
        )
    )
    for index, body in enumerate(bodies):
        sender_kind = "owner" if index % 2 == 0 else "agent"
        sender_role = "owner" if sender_kind == "owner" else agent_role
        db.record_agent_dm_message(
            AgentDmMessage(
                owner_user_id=owner_user_id,
                project_id=project_id,
                agent_role=agent_role,
                sender_kind=sender_kind,
                sender_role=sender_role,
                body=body,
                created_at=created_at + index + 1.0,
            )
        )
    if session_status != "active":
        session = db.get_agent_dm_session(owner_user_id, project_id, agent_role)
        assert session is not None
        db.upsert_agent_dm_session(replace(session, status=session_status))


def _build_dm_bridge(
    tmp_path,
    *,
    snapshots: tuple[ProjectSnapshot, ...],
    dispatcher: LLMDispatcher | None,
    set_tier: bool = True,
    whisper=None,
    vision=None,
    reply_clock=None,
    notification_clock=None,
    sender: EnvelopeCapture | None = None,
):
    db = _db(tmp_path)
    registry = ProjectRegistry(db)
    for snapshot in snapshots:
        registry.register_project(snapshot)
    if set_tier:
        db.set_tier(OWNER_ID, "STANDARD", last_changed_at=1.0)
    env = {
        "TELEGRAM_OWNER_CHAT_ID": str(OWNER_ID),
        "STATE_DB_PATH": str(db.path),
        "OPENROUTER_API_KEY": "sk-or-test",
    }
    legacy_outbound: list[object] = []

    def _legacy_send(outgoing) -> None:
        legacy_outbound.append(outgoing)

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "core.bot_runner.build_dispatcher_from_env",
                return_value=dispatcher,
            )
        )
        if whisper is not None:
            stack.enter_context(
                patch("core.bot_runner.build_whisper_client", return_value=whisper)
            )
        if vision is not None:
            stack.enter_context(
                patch("core.bot_runner.build_vision_client", return_value=vision)
            )
        if reply_clock is not None:
            stack.enter_context(
                patch("core.agent_dm_reply._default_clock", new=reply_clock)
            )
        if notification_clock is not None:
            stack.enter_context(
                patch(
                    "core.agent_owner_notifications._default_clock",
                    new=notification_clock,
                )
            )
        bridge = build_bridge_from_env(env, send_callable=_legacy_send)
    resolved_sender = sender if sender is not None else EnvelopeCapture()
    bridge.set_send_envelope(resolved_sender)
    return db, bridge, resolved_sender, legacy_outbound


def _runtime_spec() -> MultiBotRuntimeSpec:
    coordinator = BotIdentity(
        bot_id="coordinator_agent",
        agent_role="coordinator_agent",
        token_env_key="TELEGRAM_BOT_TOKEN",
        token="123:coordinator",
    )
    writer = BotIdentity(
        bot_id="writer_agent",
        agent_role="writer_agent",
        token_env_key="TELEGRAM_WRITER_BOT_TOKEN",
        token="456:writer",
    )
    return MultiBotRuntimeSpec(
        primary_bot=coordinator,
        role_map=PerRoleBotMap(
            {
                "coordinator_agent": coordinator,
                "writer_agent": writer,
            }
        ),
        source="telegram_agent_tokens",
    )


def test_direct_writer_dm_contour_creates_updates_and_reopens_session(tmp_path):
    dispatcher = _dispatcher_with_responses(
        "Первый ответ writer.",
        "Второй ответ writer.",
        "Третий ответ writer.",
    )
    db, bridge, sender, _legacy = _build_dm_bridge(
        tmp_path,
        snapshots=(_snapshot(),),
        dispatcher=dispatcher,
        reply_clock=ClockSequence(1000.0, 1010.0, 1020.0),
    )

    first = bridge.handle(_incoming_dm(text="Первый вопрос owner", message_id=1))
    session_after_first = db.get_agent_dm_session(OWNER_ID, "alpha_project", "writer_agent")
    assert session_after_first is not None
    second = bridge.handle(_incoming_dm(text="Второй вопрос owner", message_id=2))
    session_after_second = db.get_agent_dm_session(OWNER_ID, "alpha_project", "writer_agent")
    assert session_after_second is not None
    db.upsert_agent_dm_session(replace(session_after_second, status="closed"))
    third = bridge.handle(_incoming_dm(text="Третий вопрос owner", message_id=3))
    session_after_third = db.get_agent_dm_session(OWNER_ID, "alpha_project", "writer_agent")
    assert session_after_third is not None

    assert first.handled is True
    assert second.handled is True
    assert third.handled is True
    assert [envelope.sender_role for envelope in sender.sent] == [
        "writer_agent",
        "writer_agent",
        "writer_agent",
    ]
    assert [envelope.delivery_role for envelope in sender.sent] == [
        "writer_agent",
        "writer_agent",
        "writer_agent",
    ]
    assert session_after_first.status == "active"
    assert session_after_first.created_at == 1000.0
    assert session_after_first.last_interaction_at == 1000.0
    assert session_after_second.created_at == 1000.0
    assert session_after_second.last_interaction_at == 1010.0
    assert session_after_third.status == "active"
    assert session_after_third.created_at == 1000.0
    assert session_after_third.last_interaction_at == 1020.0
    assert [message.body for message in db.list_agent_dm_messages(
        OWNER_ID,
        "alpha_project",
        "writer_agent",
    )] == [
        "Первый вопрос owner",
        "Первый ответ writer.",
        "Второй вопрос owner",
        "Второй ответ writer.",
        "Третий вопрос owner",
        "Третий ответ writer.",
    ]
    assert db.recent_tasks(10) == []


def test_contextual_memory_switches_project_anchor_and_stays_scoped(tmp_path):
    dispatcher = _dispatcher_with_responses(
        "Alpha reply 1.",
        "Alpha reply 2.",
        "Alpha reply 3.",
    )
    db, bridge, sender, _legacy = _build_dm_bridge(
        tmp_path,
        snapshots=(
            _snapshot(),
            _snapshot(
                project=_project(
                    project_id="beta_project",
                    slug="beta-project",
                    name="Beta Project",
                ),
                policy=_policy("beta_project", allow_agent_dm=True),
            ),
            _snapshot(
                project=_project(
                    project_id="gamma_project",
                    slug="gamma-project",
                    name="Gamma Project",
                    owner_user_id=OTHER_OWNER_ID,
                ),
                policy=_policy("gamma_project", allow_agent_dm=True),
            ),
        ),
        dispatcher=dispatcher,
        reply_clock=ClockSequence(2000.0, 2010.0, 2020.0),
    )
    _seed_transcript(
        db,
        owner_user_id=OWNER_ID,
        project_id="beta_project",
        agent_role="writer_agent",
        thread_bot_role="writer_agent",
        bodies=("BETA OWNER LEAK", "BETA AGENT LEAK"),
        session_status="active",
        created_at=300.0,
    )
    _seed_transcript(
        db,
        owner_user_id=OWNER_ID,
        project_id="alpha_project",
        agent_role="reviewer_agent",
        thread_bot_role="reviewer_agent",
        bodies=("REVIEWER OWNER LEAK", "REVIEWER AGENT LEAK"),
        session_status="active",
        created_at=400.0,
    )
    _seed_transcript(
        db,
        owner_user_id=OTHER_OWNER_ID,
        project_id="gamma_project",
        agent_role="writer_agent",
        thread_bot_role="writer_agent",
        bodies=("OTHER OWNER LEAK", "OTHER OWNER AGENT LEAK"),
        session_status="active",
        created_at=500.0,
    )

    first = bridge.handle(
        _incoming_dm(
            text="project alpha-project: первый alpha вопрос",
            message_id=1,
        )
    )
    second = bridge.handle(_incoming_dm(text="второй alpha вопрос", message_id=2))
    third = bridge.handle(_incoming_dm(text="третий alpha вопрос", message_id=3))

    alpha_session = db.get_agent_dm_session(OWNER_ID, "alpha_project", "writer_agent")
    beta_session = db.get_agent_dm_session(OWNER_ID, "beta_project", "writer_agent")
    assert alpha_session is not None
    assert beta_session is not None
    assert first.handled is True
    assert second.handled is True
    assert third.handled is True
    assert alpha_session.status == "active"
    assert beta_session.status == "closed"
    assert [envelope.delivery_role for envelope in sender.sent] == [
        "writer_agent",
        "writer_agent",
        "writer_agent",
    ]
    alpha_messages = db.list_agent_dm_messages(OWNER_ID, "alpha_project", "writer_agent")
    assert [message.body for message in alpha_messages] == [
        "первый alpha вопрос",
        "Alpha reply 1.",
        "второй alpha вопрос",
        "Alpha reply 2.",
        "третий alpha вопрос",
        "Alpha reply 3.",
    ]
    second_content = _dispatch_content(dispatcher, 1)
    third_content = _dispatch_content(dispatcher, 2)
    assert "alpha_project" in second_content
    assert "alpha-project" in second_content
    assert "Owner: первый alpha вопрос" in second_content
    assert "Writer Agent: Alpha reply 1." in second_content
    assert "Owner: второй alpha вопрос" in second_content
    assert second_content.count("Owner: второй alpha вопрос") == 1
    assert "project alpha-project: первый alpha вопрос" not in second_content
    assert "BETA OWNER LEAK" not in second_content
    assert "REVIEWER OWNER LEAK" not in second_content
    assert "OTHER OWNER LEAK" not in second_content
    assert "Owner: второй alpha вопрос" in third_content
    assert "Writer Agent: Alpha reply 2." in third_content
    assert "Owner: третий alpha вопрос" in third_content
    assert third_content.count("Owner: третий alpha вопрос") == 1
    assert "task-v" not in third_content.lower()
    assert "repo_path" not in third_content.lower()


def test_transcript_retention_keeps_last_twenty_messages_in_scope(tmp_path):
    owner_questions = [f"Q{i}" for i in range(1, 12)]
    agent_answers = [f"A{i}" for i in range(1, 12)]
    dispatcher = _dispatcher_with_responses(*agent_answers)
    db, bridge, _sender, _legacy = _build_dm_bridge(
        tmp_path,
        snapshots=(_snapshot(),),
        dispatcher=dispatcher,
        reply_clock=ClockSequence(*[1000.0 + index for index in range(11)]),
    )

    for index, question in enumerate(owner_questions, start=1):
        result = bridge.handle(_incoming_dm(text=question, message_id=index))
        assert result.handled is True

    messages = db.list_agent_dm_messages(OWNER_ID, "alpha_project", "writer_agent")
    assert len(messages) == 20
    assert messages[0].body == "Q2"
    assert messages[-1].body == "A11"
    assert all(message.project_id == "alpha_project" for message in messages)
    assert all(message.agent_role == "writer_agent" for message in messages)


def test_ambiguous_multi_project_dm_is_truthful_and_side_effect_free(tmp_path):
    dispatcher = _dispatcher_with_responses("should not be used")
    db, bridge, sender, _legacy = _build_dm_bridge(
        tmp_path,
        snapshots=(
            _snapshot(),
            _snapshot(
                project=_project(
                    project_id="beta_project",
                    slug="beta-project",
                    name="Beta Project",
                ),
                policy=_policy("beta_project", allow_agent_dm=True),
            ),
        ),
        dispatcher=dispatcher,
    )

    result = bridge.handle(_incoming_dm(text="подскажи по API"))

    assert result.handled is True
    assert len(sender.sent) == 1
    assert sender.sent[0].sender_role == "writer_agent"
    assert sender.sent[0].delivery_role == "writer_agent"
    assert "alpha-project" in sender.sent[0].text
    assert "beta-project" in sender.sent[0].text
    assert "project <slug>: <текст>" in sender.sent[0].text
    dispatcher.dispatch.assert_not_called()
    assert db.list_agent_dm_sessions_for_owner(OWNER_ID) == ()
    assert db.list_agent_dm_messages(OWNER_ID, "alpha_project", "writer_agent") == ()
    assert db.list_agent_dm_messages(OWNER_ID, "beta_project", "writer_agent") == ()
    assert db.recent_tasks(10) == []


def test_queued_notification_requires_coordinator_then_drains_on_first_open(
    tmp_path,
):
    dispatcher = _dispatcher_with_responses("Текущий ответ writer.")
    db, bridge, sender, _legacy = _build_dm_bridge(
        tmp_path,
        snapshots=(
            _snapshot(),
            _snapshot(
                project=_project(
                    project_id="beta_project",
                    slug="beta-project",
                    name="Beta Project",
                ),
                policy=_policy("beta_project", allow_agent_dm=True),
            ),
        ),
        dispatcher=dispatcher,
        reply_clock=ClockSequence(3010.0, 3020.0),
    )
    notification_service = AgentOwnerNotificationService(db, clock=lambda: 3000.0)

    queued_result = notification_service.dispatch_or_queue(
        AgentOwnerNotificationRequest(
            owner_user_id=OWNER_ID,
            project_id="alpha_project",
            agent_role="writer_agent",
            thread_bot_role="writer_agent",
            body="Очередное уведомление writer.",
        )
    )

    assert isinstance(queued_result, AgentOwnerNotificationDispatchResult)
    assert queued_result.status == "queued_requires_coordinator"
    assert queued_result.coordinator_fallback_reply is not None
    assert queued_result.coordinator_fallback_reply.persona_role == COORDINATOR_ROLE
    assert "сохранено" in queued_result.coordinator_fallback_reply.body.lower()
    assert "не запускало project pipeline" in (
        queued_result.coordinator_fallback_reply.body.lower()
    )

    result = bridge.handle(
        _incoming_dm(
            text="project alpha-project: открываю личку",
            message_id=1,
        )
    )

    assert result.handled is True
    assert [envelope.sender_role for envelope in sender.sent] == [
        "writer_agent",
        "writer_agent",
    ]
    assert [envelope.delivery_role for envelope in sender.sent] == [
        "writer_agent",
        "writer_agent",
    ]
    assert [envelope.text for envelope in sender.sent] == [
        "Программист: Очередное уведомление writer.",
        "Программист: Текущий ответ writer.",
    ]
    assert db.list_queued_agent_owner_notifications(
        OWNER_ID,
        "alpha_project",
        "writer_agent",
        "writer_agent",
    ) == ()
    assert [message.body for message in db.list_agent_dm_messages(
        OWNER_ID,
        "alpha_project",
        "writer_agent",
    )] == [
        "Очередное уведомление writer.",
        "открываю личку",
        "Текущий ответ writer.",
    ]
    assert db.recent_tasks(10) == []


@pytest.mark.parametrize(
    ("case_name", "allow_agent_dm", "set_tier", "dispatcher", "body_match"),
    [
        ("policy_disabled", False, True, _dispatcher_with_responses("unused"), "выключен"),
        ("missing_tier", True, False, _dispatcher_with_responses("unused"), "/tier set <имя>"),
        ("dispatcher_unavailable", True, True, None, "не запускало project pipeline"),
        ("dispatch_error", True, True, "dispatch_error", "не запускало project pipeline"),
    ],
)
def test_direct_dm_failure_paths_are_truthful_and_side_effect_free(
    tmp_path,
    case_name: str,
    allow_agent_dm: bool,
    set_tier: bool,
    dispatcher,
    body_match: str,
):
    resolved_dispatcher = dispatcher
    if dispatcher == "dispatch_error":
        resolved_dispatcher = LLMDispatcher(api_key="sk-test")
        resolved_dispatcher.dispatch = MagicMock(  # type: ignore[method-assign]
            side_effect=_dispatch_error()
        )
    db, bridge, sender, _legacy = _build_dm_bridge(
        tmp_path,
        snapshots=(
            _snapshot(
                policy=_policy(allow_agent_dm=allow_agent_dm),
            ),
        ),
        dispatcher=resolved_dispatcher,
        set_tier=set_tier,
    )

    result = bridge.handle(_incoming_dm(text=f"case {case_name}", message_id=1))

    assert result.handled is True
    assert len(sender.sent) == 1
    assert sender.sent[0].sender_role == "writer_agent"
    assert sender.sent[0].delivery_role == "writer_agent"
    assert body_match.lower() in sender.sent[0].text.lower()
    assert db.get_agent_dm_session(OWNER_ID, "alpha_project", "writer_agent") is None
    assert db.list_agent_dm_messages(OWNER_ID, "alpha_project", "writer_agent") == ()
    assert db.recent_tasks(10) == []
    if (
        hasattr(resolved_dispatcher, "dispatch")
        and case_name in {"policy_disabled", "missing_tier"}
    ):
        resolved_dispatcher.dispatch.assert_not_called()


def test_failed_notification_send_does_not_ack_or_write_transcript(tmp_path):
    dispatcher = _dispatcher_with_responses("Текущий ответ writer.")
    db, bridge, sender, _legacy = _build_dm_bridge(
        tmp_path,
        snapshots=(_snapshot(),),
        dispatcher=dispatcher,
        sender=EnvelopeCapture(fail_call_index=1),
    )
    db.upsert_agent_dm_session(
        AgentDmSession(
            owner_user_id=OWNER_ID,
            project_id="alpha_project",
            agent_role="writer_agent",
            thread_bot_role="writer_agent",
            dm_chat_id=OWNER_ID,
            status="active",
            created_at=10.0,
            last_interaction_at=10.0,
        )
    )
    notification_service = AgentOwnerNotificationService(db, clock=lambda: 4000.0)
    direct_result = notification_service.dispatch_or_queue(
        AgentOwnerNotificationRequest(
            owner_user_id=OWNER_ID,
            project_id="alpha_project",
            agent_role="writer_agent",
            thread_bot_role="writer_agent",
            body="Отложенное уведомление writer.",
        )
    )

    assert direct_result.status == "direct_dm_ready"
    result = bridge.handle(_incoming_dm(text="текущий вопрос owner", message_id=1))

    assert result.handled is True
    assert sender.sent == []
    dispatcher.dispatch.assert_not_called()
    queued = db.list_queued_agent_owner_notifications(
        OWNER_ID,
        "alpha_project",
        "writer_agent",
        "writer_agent",
    )
    assert len(queued) == 1
    assert queued[0].body == "Отложенное уведомление writer."
    assert db.list_agent_dm_messages(OWNER_ID, "alpha_project", "writer_agent") == ()


@pytest.mark.parametrize(
    ("modality", "message_overrides", "patch_name", "client", "expected_body"),
    [
        (
            "voice",
            {"text": None, "voice_bytes": b"audio-data"},
            "core.bot_runner.build_whisper_client",
            FakeWhisper("Распознанный voice вопрос"),
            "Распознанный voice вопрос",
        ),
        (
            "photo",
            {"text": None, "photo_bytes": b"png-data"},
            "core.bot_runner.build_vision_client",
            FakeVision("Распознанный photo вопрос"),
            "Распознанный photo вопрос",
        ),
    ],
)
def test_voice_and_photo_resolved_text_paths_preserve_direct_dm_contour(
    tmp_path,
    modality: str,
    message_overrides: dict[str, object],
    patch_name: str,
    client,
    expected_body: str,
):
    dispatcher = _dispatcher_with_responses(f"Ответ writer на {modality}.")
    db = _db(tmp_path)
    ProjectRegistry(db).register_project(_snapshot())
    db.set_tier(OWNER_ID, "STANDARD", last_changed_at=1.0)
    env = {
        "TELEGRAM_OWNER_CHAT_ID": str(OWNER_ID),
        "STATE_DB_PATH": str(db.path),
        "OPENROUTER_API_KEY": "sk-or-test",
    }
    sender = EnvelopeCapture()

    with ExitStack() as stack:
        stack.enter_context(
            patch("core.bot_runner.build_dispatcher_from_env", return_value=dispatcher)
        )
        stack.enter_context(patch(patch_name, return_value=client))
        bridge = build_bridge_from_env(env, send_callable=lambda _out: None)
    bridge.set_send_envelope(sender)

    result = bridge.handle(_incoming_dm(message_id=1, **message_overrides))

    assert result.handled is True
    assert len(sender.sent) == 1
    assert sender.sent[0].sender_role == "writer_agent"
    assert sender.sent[0].delivery_role == "writer_agent"
    assert [message.body for message in db.list_agent_dm_messages(
        OWNER_ID,
        "alpha_project",
        "writer_agent",
    )] == [
        expected_body,
        f"Ответ writer на {modality}.",
    ]


def test_coordinator_bound_chat_and_secondary_group_paths_remain_compatible(
    tmp_path,
):
    dispatcher = _dispatcher_with_responses("unused")
    db, bridge, sender, _legacy = _build_dm_bridge(
        tmp_path,
        snapshots=(
            _snapshot(chat_binding=_binding()),
        ),
        dispatcher=dispatcher,
        set_tier=False,
    )
    multi_bridge = MultiBotBridge(
        runtime_spec=_runtime_spec(),
        primary_bridge=bridge,
    )

    coordinator_result = bridge.handle(
        _incoming_dm(
            text="coord path",
            incoming_bot_role="coordinator_agent",
            message_id=1,
        )
    )
    bound_chat_result = bridge.handle(
        _bound_chat_message(text="bound chat task", message_id=2)
    )
    multi_group_result = multi_bridge.handle_incoming(
        "writer_agent",
        _bound_chat_message(text="secondary group task", message_id=3),
    )

    assert coordinator_result.handled is True
    assert bound_chat_result.handled is True
    assert multi_group_result.handled is False
    assert multi_group_result.reason == "secondary_bot_inbound_not_enabled"
    assert sender.sent[0].sender_role == COORDINATOR_ROLE
    assert sender.sent[0].delivery_role == "coordinator_agent"
    assert sender.sent[1].sender_role == COORDINATOR_ROLE
    assert sender.sent[1].delivery_role is None
    assert db.list_agent_dm_sessions_for_owner(OWNER_ID) == ()
    assert db.list_agent_dm_messages(OWNER_ID, "alpha_project", "writer_agent") == ()
