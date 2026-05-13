"""Tests for task correlation semantics in StateBackedAgentBus."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.agent_bus import StateBackedAgentBus
from core.agent_bus_models import AgentRequest, ProjectThread
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


def _closed_thread(thread: ProjectThread) -> ProjectThread:
    return ProjectThread(
        project_id=thread.project_id,
        thread_id=thread.thread_id,
        opened_by_role=thread.opened_by_role,
        status="closed",
        created_at=thread.created_at,
        last_message_at=thread.last_message_at,
        task_id=thread.task_id,
    )


def test_get_or_open_task_thread_creates_and_reuses_canonical_thread(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    bus = StateBackedAgentBus(db)

    first = bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    second = bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role="writer_agent",
        created_at=2000.0,
    )

    assert first.thread_id == "thread_000001"
    assert second == first
    assert second.task_id == "task-42"
    assert bus.get_task_thread("alpha_project", "task-42") == first


def test_task_correlation_continues_across_bus_instances(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    first_bus = StateBackedAgentBus(db)
    thread = first_bus.get_or_open_task_thread(
        "alpha_project",
        "task-abc-001",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    request = first_bus.publish_request(
        AgentRequest(
            project_id="alpha_project",
            thread_id=thread.thread_id,
            sender_role="coordinator_agent",
            recipient_role="writer_agent",
            body="Need a first draft",
            created_at=1001.0,
        )
    )

    second_bus = StateBackedAgentBus(db)
    resolved = second_bus.get_or_open_task_thread(
        "alpha_project",
        "task-abc-001",
        opened_by_role="writer_agent",
        created_at=2000.0,
    )
    follow_up = second_bus.publish_request(
        AgentRequest(
            project_id="alpha_project",
            thread_id=resolved.thread_id,
            sender_role="reviewer_agent",
            recipient_role="writer_agent",
            body="Add citations",
            created_at=1002.0,
        )
    )

    assert resolved.thread_id == thread.thread_id
    assert resolved.task_id == thread.task_id
    assert resolved.last_message_at == 1001.0
    assert follow_up.message_id == "msg_000002"
    assert second_bus.list_thread_messages("alpha_project", thread.thread_id) == (
        request,
        follow_up,
    )


def test_different_task_id_in_same_project_creates_new_thread(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    bus = StateBackedAgentBus(db)

    first = bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    second = bus.get_or_open_task_thread(
        "alpha_project",
        "task-43",
        opened_by_role="coordinator_agent",
        created_at=1001.0,
    )

    assert first.thread_id == "thread_000001"
    assert second.thread_id == "thread_000002"
    assert first.task_id == "task-42"
    assert second.task_id == "task-43"


def test_same_task_id_in_different_project_does_not_conflict(tmp_path: Path):
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

    alpha_thread = bus.get_or_open_task_thread(
        "alpha_project",
        "task-abc-001",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    beta_thread = bus.get_or_open_task_thread(
        "beta_project",
        "task-abc-001",
        opened_by_role="coordinator_agent",
        created_at=1001.0,
    )

    assert alpha_thread.thread_id == "thread_000001"
    assert beta_thread.thread_id == "thread_000002"
    assert bus.get_task_thread("alpha_project", "task-abc-001") == alpha_thread
    assert bus.get_task_thread("beta_project", "task-abc-001") == beta_thread


def test_unbound_thread_is_not_correlated_match(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    bus = StateBackedAgentBus(db)

    thread = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )

    assert thread.task_id is None
    assert bus.get_task_thread("alpha_project", "task-42") is None


def test_closed_correlated_thread_does_not_reopen_or_duplicate(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    bus = StateBackedAgentBus(db)
    thread = bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    db.upsert_project_thread(_closed_thread(thread))

    with pytest.raises(
        ValueError,
        match="project_task_thread_closed:alpha_project:task-42",
    ):
        bus.get_or_open_task_thread(
            "alpha_project",
            "task-42",
            opened_by_role="writer_agent",
            created_at=2000.0,
        )

    assert db.list_project_threads("alpha_project") == (_closed_thread(thread),)


def test_duplicate_task_bound_thread_creation_is_rejected(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    bus = StateBackedAgentBus(db)
    bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )

    with pytest.raises(
        ValueError,
        match="duplicate_project_task_thread:alpha_project:task-42",
    ):
        bus.open_thread(
            project_id="alpha_project",
            opened_by_role="writer_agent",
            created_at=1001.0,
            task_id="task-42",
        )


def test_corrupted_duplicate_task_state_is_not_masked(tmp_path: Path):
    db = _make_db(tmp_path)
    db.upsert_project(_project())
    bus = StateBackedAgentBus(db)
    bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )

    conn = sqlite3.connect(db.path)
    try:
        conn.execute(
            """
            INSERT INTO project_threads(
                project_id,
                thread_id,
                opened_by_role,
                status,
                created_at,
                last_message_at,
                task_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "alpha_project",
                "thread_999999",
                "writer_agent",
                "open",
                1001.0,
                1001.0,
                "task-42",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(
        ValueError,
        match="duplicate_project_task_thread:alpha_project:task-42",
    ):
        bus.get_task_thread("alpha_project", "task-42")
