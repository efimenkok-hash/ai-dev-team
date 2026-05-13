"""Tests for state-backed agent bus implementation."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.agent_bus import StateBackedAgentBus
from core.agent_bus_models import AgentMessageRef, AgentReply, AgentRequest
from core.project_models import Project
from core.state_db import StateDB


def _make_db(tmp_path: Path) -> StateDB:
    return StateDB(tmp_path / "state.db")


def _project(**overrides: object) -> Project:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "slug": "alpha-project",
        "name": "Alpha Project",
        "description": "Primary AI Office project.",
        "owner_user_id": 101,
        "status": "active",
    }
    data.update(overrides)
    return Project(**data)


def _request(thread_id: str, **overrides: object) -> AgentRequest:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "thread_id": thread_id,
        "sender_role": "coordinator_agent",
        "recipient_role": "writer_agent",
        "body": "Need a first draft",
        "created_at": 1001.0,
    }
    data.update(overrides)
    return AgentRequest(**data)


def _reply(thread_id: str, in_reply_to: AgentMessageRef, **overrides: object) -> AgentReply:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "thread_id": thread_id,
        "sender_role": "writer_agent",
        "recipient_role": "coordinator_agent",
        "in_reply_to": in_reply_to,
        "body": "Draft is ready",
        "created_at": 1002.0,
    }
    data.update(overrides)
    return AgentReply(**data)


def test_open_thread_creates_persisted_thread(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    bus = StateBackedAgentBus(db)

    thread = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )

    assert thread.thread_id == "thread_000001"
    assert bus.get_thread("alpha_project", "thread_000001") == thread


def test_open_thread_continues_sequence_across_new_bus_instances(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    first_bus = StateBackedAgentBus(db)
    first = first_bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )

    second_bus = StateBackedAgentBus(db)
    second = second_bus.open_thread(
        project_id="alpha_project",
        opened_by_role="writer_agent",
        created_at=1001.0,
    )

    assert first.thread_id == "thread_000001"
    assert second.thread_id == "thread_000002"


def test_publish_request_returns_typed_message_and_updates_thread(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    bus = StateBackedAgentBus(db)
    thread = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )

    message = bus.publish_request(_request(thread.thread_id, created_at=1005.0))

    assert message.message_id == "msg_000001"
    assert message.message_kind == "request"
    assert bus.get_thread("alpha_project", thread.thread_id).last_message_at == 1005.0


def test_publish_request_rejects_unknown_thread(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    bus = StateBackedAgentBus(db)

    with pytest.raises(ValueError, match="unknown_thread:alpha_project:thread_000001"):
        bus.publish_request(_request("thread_000001"))


def test_publish_request_rejects_closed_thread(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    bus = StateBackedAgentBus(db)
    thread = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    db.upsert_project_thread(
        bus.get_thread("alpha_project", thread.thread_id).__class__(
            project_id=thread.project_id,
            thread_id=thread.thread_id,
            opened_by_role=thread.opened_by_role,
            status="closed",
            created_at=thread.created_at,
            last_message_at=thread.last_message_at,
            task_id=thread.task_id,
        )
    )

    with pytest.raises(
        ValueError,
        match=f"project_thread_closed:alpha_project:{thread.thread_id}",
    ):
        bus.publish_request(_request(thread.thread_id))


def test_publish_reply_returns_typed_message(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    bus = StateBackedAgentBus(db)
    thread = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    request = bus.publish_request(_request(thread.thread_id))

    reply = bus.publish_reply(
        _reply(
            thread.thread_id,
            AgentMessageRef(
                project_id=thread.project_id,
                thread_id=thread.thread_id,
                message_id=request.message_id,
            ),
            created_at=1002.0,
        )
    )

    assert reply.message_id == "msg_000002"
    assert reply.message_kind == "reply"
    assert reply.in_reply_to == AgentMessageRef(
        project_id=thread.project_id,
        thread_id=thread.thread_id,
        message_id=request.message_id,
    )


def test_publish_reply_rejects_unknown_request(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    bus = StateBackedAgentBus(db)
    thread = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )

    with pytest.raises(ValueError, match="unknown_in_reply_to:msg_999999"):
        bus.publish_reply(
            _reply(
                thread.thread_id,
                AgentMessageRef(
                    project_id=thread.project_id,
                    thread_id=thread.thread_id,
                    message_id="msg_999999",
                ),
            )
        )


def test_publish_reply_rejects_reply_to_reply(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    bus = StateBackedAgentBus(db)
    thread = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    request = bus.publish_request(_request(thread.thread_id))
    first_reply = bus.publish_reply(
        _reply(
            thread.thread_id,
            AgentMessageRef(
                project_id=thread.project_id,
                thread_id=thread.thread_id,
                message_id=request.message_id,
            ),
        )
    )

    with pytest.raises(ValueError, match="reply_target_must_be_request:reply"):
        bus.publish_reply(
            _reply(
                thread.thread_id,
                AgentMessageRef(
                    project_id=thread.project_id,
                    thread_id=thread.thread_id,
                    message_id=first_reply.message_id,
                ),
                created_at=1003.0,
            )
        )


def test_publish_reply_rejects_cross_thread_reference(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    bus = StateBackedAgentBus(db)
    first = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    second = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1001.0,
    )
    request = bus.publish_request(_request(first.thread_id))

    with pytest.raises(ValueError, match=f"unknown_in_reply_to:{request.message_id}"):
        bus.publish_reply(
            _reply(
                second.thread_id,
                AgentMessageRef(
                    project_id=second.project_id,
                    thread_id=second.thread_id,
                    message_id=request.message_id,
                ),
                created_at=1003.0,
            )
        )


def test_publish_reply_rejects_cross_project_reference(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_project(
        _project(
            project_id="beta_project",
            slug="beta-project",
            name="Beta Project",
        )
    )
    bus = StateBackedAgentBus(db)
    alpha = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    beta = bus.open_thread(
        project_id="beta_project",
        opened_by_role="coordinator_agent",
        created_at=1001.0,
    )
    request = bus.publish_request(_request(alpha.thread_id))

    with pytest.raises(ValueError, match=f"unknown_in_reply_to:{request.message_id}"):
        bus.publish_reply(
            AgentReply(
                project_id="beta_project",
                thread_id=beta.thread_id,
                sender_role="writer_agent",
                recipient_role="coordinator_agent",
                in_reply_to=AgentMessageRef(
                    project_id="beta_project",
                    thread_id=beta.thread_id,
                    message_id=request.message_id,
                ),
                body="Draft is ready",
                created_at=1003.0,
            )
        )


def test_publish_reply_rejects_closed_thread(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    bus = StateBackedAgentBus(db)
    thread = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    request = bus.publish_request(_request(thread.thread_id))
    db.upsert_project_thread(
        bus.get_thread("alpha_project", thread.thread_id).__class__(
            project_id=thread.project_id,
            thread_id=thread.thread_id,
            opened_by_role=thread.opened_by_role,
            status="closed",
            created_at=thread.created_at,
            last_message_at=1001.0,
            task_id=thread.task_id,
        )
    )

    with pytest.raises(
        ValueError,
        match=f"project_thread_closed:alpha_project:{thread.thread_id}",
    ):
        bus.publish_reply(
            _reply(
                thread.thread_id,
                AgentMessageRef(
                    project_id=thread.project_id,
                    thread_id=thread.thread_id,
                    message_id=request.message_id,
                ),
                created_at=1002.0,
            )
        )


def test_persistence_continuity_across_bus_instances(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    first_bus = StateBackedAgentBus(db)
    thread = first_bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    request = first_bus.publish_request(_request(thread.thread_id, created_at=1001.0))

    second_bus = StateBackedAgentBus(db)
    reply = second_bus.publish_reply(
        _reply(
            thread.thread_id,
            AgentMessageRef(
                project_id=thread.project_id,
                thread_id=thread.thread_id,
                message_id=request.message_id,
            ),
            created_at=1002.0,
        )
    )
    second_request = second_bus.publish_request(
        _request(
            thread.thread_id,
            sender_role="reviewer_agent",
            recipient_role="writer_agent",
            body="Add citations",
            created_at=1003.0,
        )
    )

    assert reply.message_id == "msg_000002"
    assert second_request.message_id == "msg_000003"
    assert second_bus.list_thread_messages("alpha_project", thread.thread_id) == (
        request,
        reply,
        second_request,
    )


def test_list_views_are_chronological_and_scoped(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_project(
        _project(
            project_id="beta_project",
            slug="beta-project",
            name="Beta Project",
        )
    )
    bus = StateBackedAgentBus(db)
    alpha_first = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=500.0,
    )
    alpha_second = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=500.0,
    )
    beta_thread = bus.open_thread(
        project_id="beta_project",
        opened_by_role="coordinator_agent",
        created_at=500.0,
    )
    newer = bus.publish_request(
        _request(alpha_first.thread_id, body="newer", created_at=2000.0)
    )
    older = bus.publish_request(
        _request(alpha_second.thread_id, body="older", created_at=1000.0)
    )
    beta_message = bus.publish_request(
        AgentRequest(
            project_id="beta_project",
            thread_id=beta_thread.thread_id,
            sender_role="coordinator_agent",
            recipient_role="writer_agent",
            body="beta",
            created_at=1500.0,
        )
    )

    assert bus.list_inbox("alpha_project", "writer_agent") == (older, newer)
    assert bus.list_thread_messages("alpha_project", alpha_first.thread_id) == (newer,)
    assert beta_message not in bus.list_inbox("alpha_project", "writer_agent")


def test_get_thread_returns_none_for_missing_thread(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    bus = StateBackedAgentBus(db)

    assert bus.get_thread("alpha_project", "thread_000001") is None
