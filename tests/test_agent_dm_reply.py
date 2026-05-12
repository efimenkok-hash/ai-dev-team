from __future__ import annotations

from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock

import pytest

from core.agent_dm_models import AgentDmMessage, AgentDmSession
from core.agent_dm_reply import (
    AgentDmSingleReplyContext,
    AgentDmSingleReplyResult,
    AgentDmSingleReplyService,
)
from core.agent_personas import default_registry
from core.llm_dispatcher import (
    LLMAttempt,
    LLMDispatcher,
    LLMDispatchError,
    LLMResponse,
)
from core.model_tier import default_registry as default_tier_registry
from core.project_models import Project, ProjectPolicy
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.state_db import StateDB
from core.telegram_bridge import BridgeReply, IncomingMessage

OWNER_ID = 101


def _db(tmp_path):
    return StateDB(tmp_path / "state.db")


def _project(**overrides):
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


def _policy(**overrides):
    data = {
        "project_id": "alpha_project",
        "allow_hiring": True,
        "allow_agent_dm": True,
        "require_owner_approval_for_hires": True,
    }
    data.update(overrides)
    return ProjectPolicy(**data)


def _snapshot(**overrides):
    data = {
        "project": _project(),
        "policy": _policy(),
    }
    data.update(overrides)
    return ProjectSnapshot(**data)


def _register_snapshot(db: StateDB, **overrides) -> ProjectSnapshot:
    registry = ProjectRegistry(db)
    snapshot = _snapshot(**overrides)
    registry.register_project(snapshot)
    loaded = registry.get_project_snapshot(snapshot.project.project_id)
    assert loaded is not None
    return loaded


def _msg(**overrides) -> IncomingMessage:
    data = {
        "chat_id": OWNER_ID,
        "user_id": OWNER_ID,
        "message_id": 1,
        "text": "Как лучше оформить API?",
        "project_id": "alpha_project",
        "project_slug": "alpha-project",
        "project_context_source": "owner_dm_single_project",
        "incoming_bot_role": "writer_agent",
    }
    data.update(overrides)
    return IncomingMessage(**data)


def _message(**overrides) -> AgentDmMessage:
    data = {
        "owner_user_id": OWNER_ID,
        "project_id": "alpha_project",
        "agent_role": "writer_agent",
        "sender_kind": "owner",
        "sender_role": "owner",
        "body": "Owner says hi",
        "created_at": 10.0,
    }
    data.update(overrides)
    return AgentDmMessage(**data)


def _session(**overrides) -> AgentDmSession:
    data = {
        "owner_user_id": OWNER_ID,
        "project_id": "alpha_project",
        "agent_role": "writer_agent",
        "thread_bot_role": "writer_agent",
        "dm_chat_id": OWNER_ID,
        "status": "active",
        "created_at": 10.0,
        "last_interaction_at": 10.0,
    }
    data.update(overrides)
    return AgentDmSession(**data)


def _response(text: str = "Я бы начал с тонкого HTTP-слоя.") -> LLMResponse:
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


def _dispatcher(text: str = "Я бы начал с тонкого HTTP-слоя.") -> LLMDispatcher:
    dispatcher = LLMDispatcher(api_key="sk-test")
    dispatcher.dispatch = MagicMock(return_value=_response(text))  # type: ignore[method-assign]
    return dispatcher


def _service(
    db: StateDB,
    *,
    dispatcher: LLMDispatcher | None = None,
    clock=lambda: 1000.0,
) -> AgentDmSingleReplyService:
    return AgentDmSingleReplyService(
        dispatcher=dispatcher,
        state_db=db,
        personas=default_registry(),
        clock=clock,
        tier_registry=default_tier_registry(),
    )


def _request_content(dispatcher: LLMDispatcher, call_index: int = -1) -> str:
    request = dispatcher.dispatch.call_args_list[call_index].args[0]
    return "\n".join(message["content"] for message in request.messages)


def test_context_happy_path():
    ctx = AgentDmSingleReplyContext(
        snapshot=_snapshot(),
        owner_user_id=OWNER_ID,
        dm_chat_id=OWNER_ID,
        agent_role="writer_agent",
        thread_bot_role="writer_agent",
        owner_text="Подскажи по API",
        project_context_source="owner_dm_single_project",
    )

    assert ctx.agent_role == "writer_agent"
    assert ctx.thread_bot_role == "writer_agent"
    assert ctx.owner_text == "Подскажи по API"


@pytest.mark.parametrize(
    "project_context_source",
    [
        "owner_dm_single_project",
        "agent_dm_explicit_project",
        "agent_dm_active_session",
        "agent_dm_single_candidate",
    ],
)
def test_context_accepts_resolved_agent_dm_context_sources(
    project_context_source: str,
):
    ctx = AgentDmSingleReplyContext(
        snapshot=_snapshot(),
        owner_user_id=OWNER_ID,
        dm_chat_id=OWNER_ID,
        agent_role="writer_agent",
        thread_bot_role="writer_agent",
        owner_text="Подскажи по API",
        project_context_source=project_context_source,
    )

    assert ctx.project_context_source == project_context_source


def test_context_is_frozen():
    ctx = AgentDmSingleReplyContext(
        snapshot=_snapshot(),
        owner_user_id=OWNER_ID,
        dm_chat_id=OWNER_ID,
        agent_role="writer_agent",
        thread_bot_role="writer_agent",
        owner_text="Подскажи по API",
        project_context_source="owner_dm_single_project",
    )

    with pytest.raises(FrozenInstanceError):
        ctx.agent_role = "reviewer_agent"  # type: ignore[misc]


def test_context_rejects_bad_snapshot():
    with pytest.raises(ValueError, match="invalid_project_snapshot_type"):
        AgentDmSingleReplyContext(
            snapshot="bad",  # type: ignore[arg-type]
            owner_user_id=OWNER_ID,
            dm_chat_id=OWNER_ID,
            agent_role="writer_agent",
            thread_bot_role="writer_agent",
            owner_text="text",
            project_context_source="owner_dm_single_project",
        )


def test_context_rejects_owner_mismatch():
    with pytest.raises(ValueError, match="owner_project_mismatch"):
        AgentDmSingleReplyContext(
            snapshot=_snapshot(project=_project(owner_user_id=999)),
            owner_user_id=OWNER_ID,
            dm_chat_id=OWNER_ID,
            agent_role="writer_agent",
            thread_bot_role="writer_agent",
            owner_text="text",
            project_context_source="owner_dm_single_project",
        )


def test_context_rejects_non_private_dm_shape():
    with pytest.raises(ValueError, match="owner_dm_requires_private_chat_shape"):
        AgentDmSingleReplyContext(
            snapshot=_snapshot(),
            owner_user_id=OWNER_ID,
            dm_chat_id=OWNER_ID + 1,
            agent_role="writer_agent",
            thread_bot_role="writer_agent",
            owner_text="text",
            project_context_source="owner_dm_single_project",
        )


def test_context_rejects_bad_agent_role():
    with pytest.raises(ValueError, match="invalid_agent_role:writer-agent"):
        AgentDmSingleReplyContext(
            snapshot=_snapshot(),
            owner_user_id=OWNER_ID,
            dm_chat_id=OWNER_ID,
            agent_role="writer-agent",
            thread_bot_role="writer_agent",
            owner_text="text",
            project_context_source="owner_dm_single_project",
        )


def test_context_rejects_bad_thread_bot_role():
    with pytest.raises(ValueError, match="invalid_thread_bot_role:writer-agent"):
        AgentDmSingleReplyContext(
            snapshot=_snapshot(),
            owner_user_id=OWNER_ID,
            dm_chat_id=OWNER_ID,
            agent_role="writer_agent",
            thread_bot_role="writer-agent",
            owner_text="text",
            project_context_source="owner_dm_single_project",
        )


def test_context_rejects_empty_owner_text():
    with pytest.raises(ValueError, match="empty_owner_text"):
        AgentDmSingleReplyContext(
            snapshot=_snapshot(),
            owner_user_id=OWNER_ID,
            dm_chat_id=OWNER_ID,
            agent_role="writer_agent",
            thread_bot_role="writer_agent",
            owner_text="   ",
            project_context_source="owner_dm_single_project",
        )


def test_context_rejects_bad_context_source():
    with pytest.raises(ValueError, match="invalid_project_context_source:none"):
        AgentDmSingleReplyContext(
            snapshot=_snapshot(),
            owner_user_id=OWNER_ID,
            dm_chat_id=OWNER_ID,
            agent_role="writer_agent",
            thread_bot_role="writer_agent",
            owner_text="text",
            project_context_source="none",
        )


def test_context_rejects_agent_role_thread_bot_role_mismatch():
    with pytest.raises(ValueError, match="agent_role_thread_bot_role_mismatch"):
        AgentDmSingleReplyContext(
            snapshot=_snapshot(),
            owner_user_id=OWNER_ID,
            dm_chat_id=OWNER_ID,
            agent_role="writer_agent",
            thread_bot_role="reviewer_agent",
            owner_text="text",
            project_context_source="owner_dm_single_project",
        )


def test_secondary_private_owner_dm_is_candidate(tmp_path):
    service = _service(_db(tmp_path))
    assert service.is_direct_reply_candidate(_msg()) is True


@pytest.mark.parametrize(
    "project_context_source",
    [
        "agent_dm_explicit_project",
        "agent_dm_active_session",
        "agent_dm_single_candidate",
    ],
)
def test_new_resolved_agent_dm_sources_are_direct_reply_candidates(
    tmp_path,
    project_context_source: str,
):
    service = _service(_db(tmp_path))
    assert (
        service.is_direct_reply_candidate(
            _msg(project_context_source=project_context_source)
        )
        is True
    )


def test_coordinator_dm_is_not_candidate(tmp_path):
    service = _service(_db(tmp_path))
    assert (
        service.is_direct_reply_candidate(
            _msg(incoming_bot_role="coordinator_agent")
        )
        is False
    )


def test_group_message_is_not_candidate(tmp_path):
    service = _service(_db(tmp_path))
    assert service.is_direct_reply_candidate(_msg(chat_id=-100123)) is False


def test_missing_incoming_bot_role_is_not_candidate(tmp_path):
    service = _service(_db(tmp_path))
    assert service.is_direct_reply_candidate(_msg(incoming_bot_role=None)) is False


def test_reply_once_creates_active_session_and_writes_transcript(tmp_path):
    db = _db(tmp_path)
    snapshot = _register_snapshot(db)
    service = _service(
        db,
        dispatcher=_dispatcher("Начни с простого контракта ответа."),
        clock=lambda: 1234.5,
    )

    result = service.reply_once(
        service.build_context(_msg(), snapshot),
        tier_name="STANDARD",
    )

    session = db.get_agent_dm_session(OWNER_ID, "alpha_project", "writer_agent")
    assert session is not None
    assert session.status == "active"
    assert session.created_at == 1234.5
    assert session.last_interaction_at == 1234.5
    messages = db.list_agent_dm_messages(OWNER_ID, "alpha_project", "writer_agent")
    assert [m.sender_kind for m in messages] == ["owner", "agent"]
    assert messages[0].body == "Как лучше оформить API?"
    assert messages[1].body == "Начни с простого контракта ответа."
    assert result.reply.persona_role == "writer_agent"


def test_reply_once_preserves_created_at_for_existing_active_session(tmp_path):
    db = _db(tmp_path)
    snapshot = _register_snapshot(db)
    db.upsert_agent_dm_session(
        AgentDmSession(
            owner_user_id=OWNER_ID,
            project_id="alpha_project",
            agent_role="writer_agent",
            thread_bot_role="writer_agent",
            dm_chat_id=OWNER_ID,
            status="active",
            created_at=10.0,
            last_interaction_at=20.0,
        )
    )
    service = _service(
        db,
        dispatcher=_dispatcher(),
        clock=lambda: 30.0,
    )

    result = service.reply_once(
        service.build_context(_msg(), snapshot),
        tier_name="STANDARD",
    )

    assert result.session.created_at == 10.0
    assert result.session.last_interaction_at == 30.0
    loaded = db.get_agent_dm_session(OWNER_ID, "alpha_project", "writer_agent")
    assert loaded is not None
    assert loaded.created_at == 10.0
    assert loaded.last_interaction_at == 30.0


def test_reply_once_reopens_closed_session(tmp_path):
    db = _db(tmp_path)
    snapshot = _register_snapshot(db)
    db.upsert_agent_dm_session(
        AgentDmSession(
            owner_user_id=OWNER_ID,
            project_id="alpha_project",
            agent_role="writer_agent",
            thread_bot_role="writer_agent",
            dm_chat_id=OWNER_ID,
            status="closed",
            created_at=10.0,
            last_interaction_at=20.0,
        )
    )
    service = _service(
        db,
        dispatcher=_dispatcher(),
        clock=lambda: 40.0,
    )

    result = service.reply_once(
        service.build_context(_msg(), snapshot),
        tier_name="STANDARD",
    )

    assert result.session.status == "active"
    assert result.session.created_at == 10.0
    assert result.session.last_interaction_at == 40.0


def test_reply_once_closes_other_agent_sessions_for_same_owner_and_agent(tmp_path):
    db = _db(tmp_path)
    snapshot = _register_snapshot(db)
    registry = ProjectRegistry(db)
    registry.register_project(
        ProjectSnapshot(
            project=_project(
                project_id="beta_project",
                slug="beta-project",
                name="Beta Project",
            ),
            policy=_policy(project_id="beta_project"),
        )
    )
    db.upsert_agent_dm_session(
        AgentDmSession(
            owner_user_id=OWNER_ID,
            project_id="beta_project",
            agent_role="writer_agent",
            thread_bot_role="writer_agent",
            dm_chat_id=OWNER_ID,
            status="active",
            created_at=5.0,
            last_interaction_at=6.0,
        )
    )
    service = _service(
        db,
        dispatcher=_dispatcher("Остаюсь на alpha."),
        clock=lambda: 50.0,
    )

    result = service.reply_once(
        service.build_context(
            _msg(project_context_source="agent_dm_explicit_project"),
            snapshot,
        ),
        tier_name="STANDARD",
    )

    assert result.session.project_id == "alpha_project"
    selected = db.get_agent_dm_session(OWNER_ID, "alpha_project", "writer_agent")
    other = db.get_agent_dm_session(OWNER_ID, "beta_project", "writer_agent")
    assert selected is not None
    assert selected.status == "active"
    assert other is not None
    assert other.status == "closed"


def test_reply_result_type_is_consistent(tmp_path):
    db = _db(tmp_path)
    snapshot = _register_snapshot(db)
    service = _service(
        db,
        dispatcher=_dispatcher("Кратко отвечу по архитектуре."),
    )

    result = service.reply_once(
        service.build_context(_msg(), snapshot),
        tier_name="STANDARD",
    )

    assert isinstance(result, AgentDmSingleReplyResult)
    assert result.owner_message.sender_kind == "owner"
    assert result.agent_message.sender_kind == "agent"
    assert result.reply.persona_role == "writer_agent"


def test_second_exchange_uses_prior_transcript_in_dispatch_prompt(tmp_path):
    db = _db(tmp_path)
    snapshot = _register_snapshot(db)
    dispatcher = LLMDispatcher(api_key="sk-test")
    dispatcher.dispatch = MagicMock(  # type: ignore[method-assign]
        side_effect=[
            _response("Первый ответ агента"),
            _response("Второй ответ агента"),
        ]
    )
    service = _service(db, dispatcher=dispatcher, clock=lambda: 20.0)

    service.reply_once(
        service.build_context(_msg(text="Первый вопрос owner"), snapshot),
        tier_name="STANDARD",
    )
    service.reply_once(
        service.build_context(_msg(text="Второй вопрос owner"), snapshot),
        tier_name="STANDARD",
    )

    content = _request_content(dispatcher, 1)
    assert "alpha_project" in content
    assert "alpha-project" in content
    assert "Первый вопрос owner" in content
    assert "Первый ответ агента" in content
    assert "Второй вопрос owner" in content
    assert content.count("Второй вопрос owner") == 1


def test_third_exchange_preserves_conversation_continuity(tmp_path):
    db = _db(tmp_path)
    snapshot = _register_snapshot(db)
    dispatcher = LLMDispatcher(api_key="sk-test")
    dispatcher.dispatch = MagicMock(  # type: ignore[method-assign]
        side_effect=[
            _response("Ответ 1"),
            _response("Ответ 2"),
            _response("Ответ 3"),
        ]
    )
    service = _service(db, dispatcher=dispatcher, clock=lambda: 30.0)

    service.reply_once(
        service.build_context(_msg(text="Вопрос 1"), snapshot),
        tier_name="STANDARD",
    )
    service.reply_once(
        service.build_context(_msg(text="Вопрос 2"), snapshot),
        tier_name="STANDARD",
    )
    service.reply_once(
        service.build_context(_msg(text="Вопрос 3"), snapshot),
        tier_name="STANDARD",
    )

    content = _request_content(dispatcher, 2)
    assert "Вопрос 1" in content
    assert "Ответ 1" in content
    assert "Вопрос 2" in content
    assert "Ответ 2" in content
    assert "Вопрос 3" in content
    messages = db.list_agent_dm_messages(OWNER_ID, "alpha_project", "writer_agent")
    assert len(messages) == 6
    assert [message.sender_kind for message in messages] == [
        "owner",
        "agent",
        "owner",
        "agent",
        "owner",
        "agent",
    ]


def test_transcript_from_other_owner_project_agent_does_not_leak(tmp_path):
    db = _db(tmp_path)
    snapshot = _register_snapshot(db)
    registry = ProjectRegistry(db)
    registry.register_project(
        ProjectSnapshot(
            project=_project(
                project_id="beta_project",
                slug="beta-project",
                name="Beta Project",
                owner_user_id=202,
            ),
            policy=_policy(project_id="beta_project"),
        )
    )
    db.upsert_agent_dm_session(_session())
    db.record_agent_dm_message(_message(body="TARGET OWNER", created_at=10.0))
    db.record_agent_dm_message(
        _message(
            sender_kind="agent",
            sender_role="writer_agent",
            body="TARGET AGENT",
            created_at=11.0,
        )
    )
    db.upsert_agent_dm_session(
        AgentDmSession(
            owner_user_id=OWNER_ID,
            project_id="alpha_project",
            agent_role="reviewer_agent",
            thread_bot_role="reviewer_agent",
            dm_chat_id=OWNER_ID,
            status="active",
            created_at=10.0,
            last_interaction_at=10.0,
        )
    )
    db.record_agent_dm_message(
        AgentDmMessage(
            owner_user_id=OWNER_ID,
            project_id="alpha_project",
            agent_role="reviewer_agent",
            sender_kind="agent",
            sender_role="reviewer_agent",
            body="LEAK OTHER AGENT",
            created_at=12.0,
        )
    )
    db.upsert_agent_dm_session(
        AgentDmSession(
            owner_user_id=202,
            project_id="beta_project",
            agent_role="writer_agent",
            thread_bot_role="writer_agent",
            dm_chat_id=202,
            status="active",
            created_at=10.0,
            last_interaction_at=10.0,
        )
    )
    db.record_agent_dm_message(
        AgentDmMessage(
            owner_user_id=202,
            project_id="beta_project",
            agent_role="writer_agent",
            sender_kind="owner",
            sender_role="owner",
            body="LEAK OTHER PROJECT",
            created_at=13.0,
        )
    )
    db.upsert_agent_dm_session(
        AgentDmSession(
            owner_user_id=OWNER_ID,
            project_id="alpha_project",
            agent_role="coordinator_agent",
            thread_bot_role="coordinator_agent",
            dm_chat_id=OWNER_ID,
            status="active",
            created_at=10.0,
            last_interaction_at=10.0,
        )
    )
    db.record_agent_dm_message(
        AgentDmMessage(
            owner_user_id=OWNER_ID,
            project_id="alpha_project",
            agent_role="coordinator_agent",
            sender_kind="agent",
            sender_role="coordinator_agent",
            body="LEAK COORDINATOR",
            created_at=14.0,
        )
    )
    dispatcher = _dispatcher("Контекстный ответ без утечки.")
    service = _service(db, dispatcher=dispatcher, clock=lambda: 20.0)

    service.reply_once(
        service.build_context(_msg(text="Текущий вопрос owner"), snapshot),
        tier_name="STANDARD",
    )

    content = _request_content(dispatcher)
    assert "TARGET OWNER" in content
    assert "TARGET AGENT" in content
    assert "LEAK OTHER AGENT" not in content
    assert "LEAK OTHER PROJECT" not in content
    assert "LEAK COORDINATOR" not in content


def test_missing_dispatcher_returns_truthful_unavailable_reply_without_side_effects(
    tmp_path,
):
    db = _db(tmp_path)
    snapshot = _register_snapshot(db)
    service = _service(db, dispatcher=None)

    reply = service.reply_or_unavailable(
        service.build_context(_msg(), snapshot),
        tier_name="STANDARD",
    )

    assert isinstance(reply, BridgeReply)
    assert reply.persona_role == "writer_agent"
    assert "single-shot" in reply.body
    assert (
        db.get_agent_dm_session(OWNER_ID, "alpha_project", "writer_agent")
        is None
    )
    assert db.list_agent_dm_messages(OWNER_ID, "alpha_project", "writer_agent") == ()


def test_dispatch_error_returns_truthful_unavailable_reply_without_side_effects(
    tmp_path,
):
    db = _db(tmp_path)
    snapshot = _register_snapshot(db)
    dispatcher = LLMDispatcher(api_key="sk-test")
    dispatcher.dispatch = MagicMock(side_effect=_dispatch_error())  # type: ignore[method-assign]
    service = _service(db, dispatcher=dispatcher)

    reply = service.reply_or_unavailable(
        service.build_context(_msg(), snapshot),
        tier_name="STANDARD",
    )

    assert isinstance(reply, BridgeReply)
    assert reply.persona_role == "writer_agent"
    assert "не запускало project pipeline" in reply.body
    assert (
        db.get_agent_dm_session(OWNER_ID, "alpha_project", "writer_agent")
        is None
    )
    assert db.list_agent_dm_messages(OWNER_ID, "alpha_project", "writer_agent") == ()
