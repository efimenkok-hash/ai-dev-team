"""Tests for agent bus persistence on top of core.state_db."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.agent_bus_models import (
    AgentMessage,
    AgentMessageRef,
    ProjectThread,
)
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


def _thread(**overrides: object) -> ProjectThread:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "thread_id": "thread_000001",
        "opened_by_role": "coordinator_agent",
        "status": "open",
        "created_at": 1000.0,
        "last_message_at": 1000.0,
        "task_id": None,
    }
    data.update(overrides)
    return ProjectThread(**data)


def _message(**overrides: object) -> AgentMessage:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "thread_id": "thread_000001",
        "message_id": "msg_000001",
        "sender_role": "coordinator_agent",
        "recipient_role": "writer_agent",
        "message_kind": "request",
        "body": "Need a first draft",
        "created_at": 1001.0,
        "in_reply_to": None,
    }
    data.update(overrides)
    return AgentMessage(**data)


def test_upsert_project_thread_round_trip_works(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    thread = _thread()

    db.upsert_project_thread(thread)

    assert db.get_project_thread("alpha_project", "thread_000001") == thread


def test_upsert_project_thread_overwrites_existing_thread(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_project_thread(_thread())
    updated = _thread(
        opened_by_role="writer_agent",
        status="closed",
        last_message_at=1005.0,
        task_id="task_one",
    )

    db.upsert_project_thread(updated)

    assert db.get_project_thread("alpha_project", "thread_000001") == updated


def test_upsert_project_thread_rejects_unknown_project(tmp_path: Path):
    db = _make_db(tmp_path)

    with pytest.raises(ValueError, match="unknown_project_id:alpha_project"):
        db.upsert_project_thread(_thread())


def test_list_project_threads_is_deterministic(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    older = _thread(thread_id="thread_000002", last_message_at=1001.0)
    newer = _thread(thread_id="thread_000003", last_message_at=1005.0)
    same_time_lower_id = _thread(thread_id="thread_000001", last_message_at=1005.0)
    for thread in (older, newer, same_time_lower_id):
        db.upsert_project_thread(thread)

    assert db.list_project_threads("alpha_project") == (
        same_time_lower_id,
        newer,
        older,
    )


def test_request_message_inserts_successfully(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_project_thread(_thread())
    request = _message()

    db.insert_agent_bus_message(request)

    assert db.get_agent_bus_message("alpha_project", "thread_000001", "msg_000001") == request


def test_reply_message_inserts_successfully(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_project_thread(_thread())
    request = _message()
    db.insert_agent_bus_message(request)
    reply = _message(
        message_id="msg_000002",
        sender_role="writer_agent",
        recipient_role="coordinator_agent",
        message_kind="reply",
        body="Draft is ready",
        created_at=1002.0,
        in_reply_to=AgentMessageRef(
            project_id="alpha_project",
            thread_id="thread_000001",
            message_id="msg_000001",
        ),
    )

    db.insert_agent_bus_message(reply)

    assert db.get_agent_bus_message("alpha_project", "thread_000001", "msg_000002") == reply


def test_reply_to_unknown_request_rejected(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_project_thread(_thread())
    reply = _message(
        message_id="msg_000002",
        sender_role="writer_agent",
        recipient_role="coordinator_agent",
        message_kind="reply",
        body="Draft is ready",
        created_at=1002.0,
        in_reply_to=AgentMessageRef(
            project_id="alpha_project",
            thread_id="thread_000001",
            message_id="msg_999999",
        ),
    )

    with pytest.raises(ValueError, match="unknown_in_reply_to:msg_999999"):
        db.insert_agent_bus_message(reply)


def test_reply_to_reply_rejected(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_project_thread(_thread())
    request = _message()
    reply = _message(
        message_id="msg_000002",
        sender_role="writer_agent",
        recipient_role="coordinator_agent",
        message_kind="reply",
        body="Draft is ready",
        created_at=1002.0,
        in_reply_to=AgentMessageRef(
            project_id="alpha_project",
            thread_id="thread_000001",
            message_id="msg_000001",
        ),
    )
    db.insert_agent_bus_message(request)
    db.insert_agent_bus_message(reply)
    reply_to_reply = _message(
        message_id="msg_000003",
        sender_role="reviewer_agent",
        recipient_role="writer_agent",
        message_kind="reply",
        body="Need citations",
        created_at=1003.0,
        in_reply_to=AgentMessageRef(
            project_id="alpha_project",
            thread_id="thread_000001",
            message_id="msg_000002",
        ),
    )

    with pytest.raises(ValueError, match="reply_target_must_be_request:reply"):
        db.insert_agent_bus_message(reply_to_reply)


def test_cross_thread_reply_rejected(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    first = _thread(thread_id="thread_000001")
    second = _thread(thread_id="thread_000002")
    db.upsert_project_thread(first)
    db.upsert_project_thread(second)
    db.insert_agent_bus_message(_message())
    reply = _message(
        project_id="alpha_project",
        thread_id="thread_000002",
        message_id="msg_000001",
        sender_role="writer_agent",
        recipient_role="coordinator_agent",
        message_kind="reply",
        body="Draft is ready",
        created_at=1002.0,
        in_reply_to=AgentMessageRef(
            project_id="alpha_project",
            thread_id="thread_000002",
            message_id="msg_000001",
        ),
    )

    with pytest.raises(ValueError, match="unknown_in_reply_to:msg_000001"):
        db.insert_agent_bus_message(reply)


def test_cross_project_reply_rejected(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_project(
        _project(
            project_id="beta_project",
            slug="beta-project",
            name="Beta Project",
        )
    )
    db.upsert_project_thread(_thread())
    db.upsert_project_thread(
        _thread(
            project_id="beta_project",
            thread_id="thread_000001",
        )
    )
    db.insert_agent_bus_message(_message())
    reply = _message(
        project_id="beta_project",
        thread_id="thread_000001",
        message_id="msg_000001",
        sender_role="writer_agent",
        recipient_role="coordinator_agent",
        message_kind="reply",
        body="Draft is ready",
        created_at=1002.0,
        in_reply_to=AgentMessageRef(
            project_id="beta_project",
            thread_id="thread_000001",
            message_id="msg_000001",
        ),
    )

    with pytest.raises(ValueError, match="unknown_in_reply_to:msg_000001"):
        db.insert_agent_bus_message(reply)


def test_thread_history_returns_oldest_first(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_project_thread(_thread())
    request = _message()
    reply = _message(
        message_id="msg_000002",
        sender_role="writer_agent",
        recipient_role="coordinator_agent",
        message_kind="reply",
        body="Draft is ready",
        created_at=1002.0,
        in_reply_to=AgentMessageRef(
            project_id="alpha_project",
            thread_id="thread_000001",
            message_id="msg_000001",
        ),
    )
    db.insert_agent_bus_message(request)
    db.insert_agent_bus_message(reply)

    assert db.list_agent_bus_messages("alpha_project", "thread_000001") == (
        request,
        reply,
    )


def test_inbox_filters_by_project_and_recipient(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    db.upsert_project(
        _project(
            project_id="beta_project",
            slug="beta-project",
            name="Beta Project",
        )
    )
    db.upsert_project_thread(_thread(thread_id="thread_000001"))
    db.upsert_project_thread(_thread(thread_id="thread_000002"))
    db.upsert_project_thread(
        _thread(project_id="beta_project", thread_id="thread_000001")
    )
    writer_first = _message(
        thread_id="thread_000001",
        message_id="msg_000001",
        recipient_role="writer_agent",
        created_at=1001.0,
    )
    reviewer_message = _message(
        thread_id="thread_000001",
        message_id="msg_000002",
        recipient_role="reviewer_agent",
        created_at=1002.0,
    )
    writer_second = _message(
        thread_id="thread_000002",
        message_id="msg_000001",
        recipient_role="writer_agent",
        created_at=1003.0,
    )
    beta_writer = _message(
        project_id="beta_project",
        thread_id="thread_000001",
        message_id="msg_000001",
        recipient_role="writer_agent",
        created_at=1004.0,
    )
    for message in (writer_first, reviewer_message, writer_second, beta_writer):
        db.insert_agent_bus_message(message)

    assert db.list_agent_bus_inbox("alpha_project", "writer_agent") == (
        writer_first,
        writer_second,
    )


def test_inbox_returns_oldest_first_across_threads_not_insert_order(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    first = _thread(thread_id="thread_000001")
    second = _thread(thread_id="thread_000002")
    db.upsert_project_thread(first)
    db.upsert_project_thread(second)
    newer_inserted_first = _message(
        thread_id="thread_000001",
        message_id="msg_000001",
        recipient_role="writer_agent",
        body="newer",
        created_at=2000.0,
    )
    older_inserted_second = _message(
        thread_id="thread_000002",
        message_id="msg_000001",
        recipient_role="writer_agent",
        body="older",
        created_at=1000.0,
    )

    db.insert_agent_bus_message(newer_inserted_first)
    db.insert_agent_bus_message(older_inserted_second)

    assert db.list_agent_bus_inbox("alpha_project", "writer_agent") == (
        older_inserted_second,
        newer_inserted_first,
    )


def test_bus_list_views_do_not_leak_another_thread(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    first = _thread(thread_id="thread_000001")
    second = _thread(thread_id="thread_000002")
    db.upsert_project_thread(first)
    db.upsert_project_thread(second)
    first_message = _message(thread_id="thread_000001", message_id="msg_000001")
    second_message = _message(thread_id="thread_000002", message_id="msg_000001")
    db.insert_agent_bus_message(first_message)
    db.insert_agent_bus_message(second_message)

    assert db.list_agent_bus_messages("alpha_project", "thread_000001") == (
        first_message,
    )


def test_missing_project_thread_and_duplicate_message_rejected(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())

    with pytest.raises(
        ValueError,
        match="unknown_project_thread:alpha_project:thread_000001",
    ):
        db.insert_agent_bus_message(_message())

    db.upsert_project_thread(_thread())
    db.insert_agent_bus_message(_message())

    with pytest.raises(
        ValueError,
        match="duplicate_agent_bus_message:alpha_project:thread_000001:msg_000001",
    ):
        db.insert_agent_bus_message(_message())
