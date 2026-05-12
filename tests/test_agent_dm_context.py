from __future__ import annotations

import pytest

from core.agent_dm_context import (
    AgentDmContextService,
    AgentDmPromptContext,
    AgentDmTranscriptWindow,
)
from core.agent_dm_models import AgentDmMessage, AgentDmSession
from core.agent_dm_reply import AgentDmSingleReplyContext
from core.project_models import Project, ProjectPolicy
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.state_db import StateDB

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


def _context(snapshot: ProjectSnapshot, **overrides) -> AgentDmSingleReplyContext:
    data = {
        "snapshot": snapshot,
        "owner_user_id": OWNER_ID,
        "dm_chat_id": OWNER_ID,
        "agent_role": "writer_agent",
        "thread_bot_role": "writer_agent",
        "owner_text": "Текущий вопрос owner.",
        "project_context_source": "owner_dm_single_project",
    }
    data.update(overrides)
    return AgentDmSingleReplyContext(**data)


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
        "last_interaction_at": 20.0,
    }
    data.update(overrides)
    return AgentDmSession(**data)


def test_transcript_window_empty_happy_path():
    window = AgentDmTranscriptWindow(
        owner_user_id=OWNER_ID,
        project_id="alpha_project",
        agent_role="writer_agent",
        messages=(),
    )

    assert window.messages == ()


def test_transcript_window_non_empty_happy_path():
    owner_message = _message(created_at=10.0)
    agent_message = _message(
        sender_kind="agent",
        sender_role="writer_agent",
        body="Agent replies",
        created_at=11.0,
    )

    window = AgentDmTranscriptWindow(
        owner_user_id=OWNER_ID,
        project_id="alpha_project",
        agent_role="writer_agent",
        messages=(owner_message, agent_message),
    )

    assert [message.body for message in window.messages] == [
        "Owner says hi",
        "Agent replies",
    ]


def test_transcript_window_rejects_mixed_owner_project_agent_rows():
    with pytest.raises(ValueError, match="transcript_project_id_mismatch"):
        AgentDmTranscriptWindow(
            owner_user_id=OWNER_ID,
            project_id="alpha_project",
            agent_role="writer_agent",
            messages=(
                _message(created_at=10.0),
                _message(
                    project_id="beta_project",
                    sender_kind="agent",
                    sender_role="writer_agent",
                    body="bad",
                    created_at=11.0,
                ),
            ),
        )


def test_transcript_window_rejects_non_chronological_order():
    with pytest.raises(ValueError, match="transcript_messages_not_chronological"):
        AgentDmTranscriptWindow(
            owner_user_id=OWNER_ID,
            project_id="alpha_project",
            agent_role="writer_agent",
            messages=(
                _message(created_at=20.0),
                _message(
                    sender_kind="agent",
                    sender_role="writer_agent",
                    body="earlier",
                    created_at=10.0,
                ),
            ),
        )


def test_load_transcript_empty_happy_path(tmp_path):
    db = _db(tmp_path)
    _register_snapshot(db)
    service = AgentDmContextService(db)

    window = service.load_transcript(OWNER_ID, "alpha_project", "writer_agent")

    assert window.messages == ()


def test_load_transcript_preserves_chronological_order(tmp_path):
    db = _db(tmp_path)
    _register_snapshot(db)
    db.upsert_agent_dm_session(_session())
    db.record_agent_dm_message(_message(body="first", created_at=10.0))
    db.record_agent_dm_message(
        _message(
            sender_kind="agent",
            sender_role="writer_agent",
            body="second",
            created_at=11.0,
        )
    )
    service = AgentDmContextService(db)

    window = service.load_transcript(OWNER_ID, "alpha_project", "writer_agent")

    assert [message.body for message in window.messages] == ["first", "second"]


def test_load_transcript_respects_explicit_limit(tmp_path):
    db = _db(tmp_path)
    _register_snapshot(db)
    db.upsert_agent_dm_session(_session())
    db.record_agent_dm_message(_message(body="m1", created_at=1.0))
    db.record_agent_dm_message(
        _message(
            sender_kind="agent",
            sender_role="writer_agent",
            body="m2",
            created_at=2.0,
        )
    )
    db.record_agent_dm_message(_message(body="m3", created_at=3.0))
    service = AgentDmContextService(db, transcript_limit=20)

    window = service.load_transcript(
        OWNER_ID,
        "alpha_project",
        "writer_agent",
        limit=2,
    )

    assert [message.body for message in window.messages] == ["m2", "m3"]


def test_prompt_context_happy_path_with_no_prior_session(tmp_path):
    db = _db(tmp_path)
    snapshot = _register_snapshot(db)
    service = AgentDmContextService(db)

    prompt_context = service.build_prompt_context(_context(snapshot))

    assert prompt_context.session is None
    assert prompt_context.transcript.messages == ()


def test_prompt_context_happy_path_with_active_session(tmp_path):
    db = _db(tmp_path)
    snapshot = _register_snapshot(db)
    db.upsert_agent_dm_session(_session())
    db.record_agent_dm_message(_message(body="Earlier owner", created_at=10.0))
    service = AgentDmContextService(db)

    prompt_context = service.build_prompt_context(_context(snapshot))

    assert prompt_context.session is not None
    assert prompt_context.session.status == "active"
    assert [message.body for message in prompt_context.transcript.messages] == [
        "Earlier owner"
    ]


def test_prompt_context_rejects_mismatched_session_project():
    snapshot = _snapshot()
    transcript = AgentDmTranscriptWindow(
        owner_user_id=OWNER_ID,
        project_id="alpha_project",
        agent_role="writer_agent",
        messages=(),
    )

    with pytest.raises(ValueError, match="prompt_context_session_project_id_mismatch"):
        AgentDmPromptContext(
            snapshot=snapshot,
            session=_session(project_id="beta_project"),
            transcript=transcript,
            owner_text="Current",
            agent_role="writer_agent",
            thread_bot_role="writer_agent",
        )


def test_prompt_context_rejects_mismatched_transcript_project():
    with pytest.raises(
        ValueError,
        match="prompt_context_transcript_project_id_mismatch",
    ):
        AgentDmPromptContext(
            snapshot=_snapshot(),
            session=None,
            transcript=AgentDmTranscriptWindow(
                owner_user_id=OWNER_ID,
                project_id="beta_project",
                agent_role="writer_agent",
                messages=(),
            ),
            owner_text="Current",
            agent_role="writer_agent",
            thread_bot_role="writer_agent",
        )


def test_prompt_context_rejects_bad_owner_text():
    with pytest.raises(ValueError, match="empty_owner_text"):
        AgentDmPromptContext(
            snapshot=_snapshot(),
            session=None,
            transcript=AgentDmTranscriptWindow(
                owner_user_id=OWNER_ID,
                project_id="alpha_project",
                agent_role="writer_agent",
                messages=(),
            ),
            owner_text="   ",
            agent_role="writer_agent",
            thread_bot_role="writer_agent",
        )


def test_prompt_context_rejects_bad_roles():
    with pytest.raises(ValueError, match="invalid_agent_role:writer-agent"):
        AgentDmPromptContext(
            snapshot=_snapshot(),
            session=None,
            transcript=AgentDmTranscriptWindow(
                owner_user_id=OWNER_ID,
                project_id="alpha_project",
                agent_role="writer_agent",
                messages=(),
            ),
            owner_text="Current",
            agent_role="writer-agent",
            thread_bot_role="writer_agent",
        )


def test_build_dispatch_messages_includes_project_history_and_current_text(
    tmp_path,
):
    db = _db(tmp_path)
    snapshot = _register_snapshot(db)
    db.upsert_agent_dm_session(_session())
    db.record_agent_dm_message(_message(body="Earlier owner", created_at=10.0))
    db.record_agent_dm_message(
        _message(
            sender_kind="agent",
            sender_role="writer_agent",
            body="Earlier agent",
            created_at=11.0,
        )
    )
    service = AgentDmContextService(db)
    prompt_context = service.build_prompt_context(
        _context(snapshot, owner_text="Current owner text")
    )

    messages = service.build_dispatch_messages(prompt_context)
    flattened = "\n".join(message["content"] for message in messages)

    assert "alpha_project" in flattened
    assert "alpha-project" in flattened
    assert "Earlier owner" in flattened
    assert "Earlier agent" in flattened
    assert "Current owner text" in flattened
    assert flattened.count("Current owner text") == 1


def test_build_dispatch_messages_is_deterministic(tmp_path):
    db = _db(tmp_path)
    snapshot = _register_snapshot(db)
    db.upsert_agent_dm_session(_session())
    db.record_agent_dm_message(_message(body="Earlier owner", created_at=10.0))
    service = AgentDmContextService(db)
    prompt_context = service.build_prompt_context(_context(snapshot))

    first = service.build_dispatch_messages(prompt_context)
    second = service.build_dispatch_messages(prompt_context)

    assert first == second
