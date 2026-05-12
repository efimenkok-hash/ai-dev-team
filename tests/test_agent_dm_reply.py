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


def test_reply_once_prompt_does_not_include_previous_transcript(tmp_path):
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
            last_interaction_at=10.0,
        )
    )
    db.record_agent_dm_message(
        AgentDmMessage(
            owner_user_id=OWNER_ID,
            project_id="alpha_project",
            agent_role="writer_agent",
            sender_kind="owner",
            sender_role="owner",
            body="OLD OWNER CONTEXT MUST NOT APPEAR",
            created_at=11.0,
        )
    )
    db.record_agent_dm_message(
        AgentDmMessage(
            owner_user_id=OWNER_ID,
            project_id="alpha_project",
            agent_role="writer_agent",
            sender_kind="agent",
            sender_role="writer_agent",
            body="OLD AGENT CONTEXT MUST NOT APPEAR",
            created_at=12.0,
        )
    )
    dispatcher = _dispatcher("Сфокусируйся на текущем сообщении.")
    service = _service(
        db,
        dispatcher=dispatcher,
        clock=lambda: 20.0,
    )

    service.reply_once(
        service.build_context(_msg(text="Только новый вопрос"), snapshot),
        tier_name="STANDARD",
    )

    request = dispatcher.dispatch.call_args.args[0]
    content = "\n".join(message["content"] for message in request.messages)
    assert "OLD OWNER CONTEXT MUST NOT APPEAR" not in content
    assert "OLD AGENT CONTEXT MUST NOT APPEAR" not in content
    assert "Только новый вопрос" in content


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
