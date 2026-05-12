from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from core.agent_dm_models import AgentDmSession
from core.agent_owner_notifications import (
    AgentOwnerNotification,
    AgentOwnerNotificationDispatchResult,
    AgentOwnerNotificationRequest,
    AgentOwnerNotificationService,
)
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


def _policy(project_id: str = "alpha_project", **overrides):
    data = {
        "project_id": project_id,
        "allow_hiring": True,
        "allow_agent_dm": True,
        "require_owner_approval_for_hires": True,
    }
    data.update(overrides)
    return ProjectPolicy(**data)


def _register_project(
    registry: ProjectRegistry,
    *,
    project: Project | None = None,
    policy: ProjectPolicy | None = None,
) -> ProjectSnapshot:
    snapshot = ProjectSnapshot(
        project=_project() if project is None else project,
        policy=_policy() if policy is None else policy,
    )
    registry.register_project(snapshot)
    loaded = registry.get_project_snapshot(snapshot.project.project_id)
    assert loaded is not None
    return loaded


def _session(**overrides):
    data = {
        "owner_user_id": OWNER_ID,
        "project_id": "alpha_project",
        "agent_role": "writer_agent",
        "thread_bot_role": "writer_agent",
        "dm_chat_id": OWNER_ID,
        "chat_provider": "telegram",
        "status": "active",
        "created_at": 10.0,
        "last_interaction_at": 20.0,
    }
    data.update(overrides)
    return AgentDmSession(**data)


def _notification(**overrides):
    data = {
        "notification_id": None,
        "owner_user_id": OWNER_ID,
        "project_id": "alpha_project",
        "agent_role": "writer_agent",
        "thread_bot_role": "writer_agent",
        "body": "Нужен быстрый owner input.",
        "chat_provider": "telegram",
        "status": "queued",
        "created_at": 1000.0,
        "delivered_at": None,
    }
    data.update(overrides)
    return AgentOwnerNotification(**data)


def _request(**overrides):
    data = {
        "owner_user_id": OWNER_ID,
        "project_id": "alpha_project",
        "agent_role": "writer_agent",
        "thread_bot_role": "writer_agent",
        "body": "Нужен быстрый owner input.",
    }
    data.update(overrides)
    return AgentOwnerNotificationRequest(**data)


def _service(tmp_path, *, db: StateDB | None = None, clock=lambda: 1000.0):
    resolved_db = _db(tmp_path) if db is None else db
    return AgentOwnerNotificationService(resolved_db, clock=clock)


def test_queued_notification_happy_path():
    notification = _notification()

    assert notification.status == "queued"
    assert notification.delivered_at is None


def test_delivered_notification_happy_path():
    notification = _notification(
        notification_id=1,
        status="delivered",
        delivered_at=1010.0,
    )

    assert notification.notification_id == 1
    assert notification.status == "delivered"
    assert notification.delivered_at == 1010.0


def test_notification_model_is_frozen():
    notification = _notification()

    with pytest.raises(FrozenInstanceError):
        notification.body = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("owner_user_id", 0, "invalid_owner_user_id"),
        ("project_id", "bad-id", "invalid_project_id"),
        ("agent_role", "writer-agent", "invalid_agent_role"),
        ("thread_bot_role", "writer-agent", "invalid_thread_bot_role"),
        ("body", "   ", "empty_body"),
    ],
)
def test_notification_rejects_invalid_fields(field, value, match):
    with pytest.raises(ValueError, match=match):
        _notification(**{field: value})


def test_notification_rejects_delivered_without_delivered_at():
    with pytest.raises(ValueError, match="delivered_notification_requires_delivered_at"):
        _notification(status="delivered", delivered_at=None)


def test_notification_rejects_queued_with_delivered_at():
    with pytest.raises(ValueError, match="queued_notification_forbids_delivered_at"):
        _notification(delivered_at=1001.0)


def test_request_rejects_agent_thread_mismatch():
    with pytest.raises(ValueError, match="agent_role_thread_bot_role_mismatch"):
        _request(thread_bot_role="reviewer_agent")


def test_dispatch_or_queue_returns_direct_dm_ready_for_active_session(tmp_path):
    db = _db(tmp_path)
    registry = ProjectRegistry(db)
    _register_project(registry)
    db.upsert_agent_dm_session(_session())
    service = AgentOwnerNotificationService(db, clock=lambda: 1500.0)

    result = service.dispatch_or_queue(_request())

    assert isinstance(result, AgentOwnerNotificationDispatchResult)
    assert result.status == "direct_dm_ready"
    assert result.session is not None
    assert result.coordinator_fallback_reply is None
    assert result.notification.notification_id is not None
    queued = db.list_queued_agent_owner_notifications(
        OWNER_ID,
        "alpha_project",
        "writer_agent",
        "writer_agent",
    )
    assert len(queued) == 1
    assert queued[0].body == "Нужен быстрый owner input."


def test_dispatch_or_queue_returns_coordinator_fallback_without_session(tmp_path):
    db = _db(tmp_path)
    registry = ProjectRegistry(db)
    _register_project(registry)
    service = AgentOwnerNotificationService(db, clock=lambda: 1600.0)

    result = service.dispatch_or_queue(_request())

    assert result.status == "queued_requires_coordinator"
    assert result.session is None
    assert result.coordinator_fallback_reply is not None
    assert result.coordinator_fallback_reply.persona_role == "coordinator_agent"
    assert "ещё не активирован" in result.coordinator_fallback_reply.body.lower()
    assert "сохранено" in result.coordinator_fallback_reply.body.lower()
    queued = db.list_queued_agent_owner_notifications(
        OWNER_ID,
        "alpha_project",
        "writer_agent",
        "writer_agent",
    )
    assert len(queued) == 1


def test_dispatch_or_queue_rejects_owner_project_mismatch(tmp_path):
    db = _db(tmp_path)
    registry = ProjectRegistry(db)
    _register_project(registry)
    service = AgentOwnerNotificationService(db)

    with pytest.raises(ValueError, match="agent_owner_notification_owner_project_mismatch"):
        service.dispatch_or_queue(_request(owner_user_id=202))


def test_list_pending_for_session_orders_oldest_first_and_ack_delivered_marks_one(
    tmp_path,
):
    db = _db(tmp_path)
    registry = ProjectRegistry(db)
    _register_project(registry)
    db.upsert_agent_dm_session(_session())
    service = AgentOwnerNotificationService(db, clock=lambda: 2000.0)

    first = service.dispatch_or_queue(_request(body="Первое уведомление")).notification
    second = service.dispatch_or_queue(
        _request(body="Второе уведомление")
    ).notification

    pending = service.list_pending_for_session(_session())
    assert [notification.body for notification in pending] == [
        "Первое уведомление",
        "Второе уведомление",
    ]

    delivered = service.ack_delivered(first)

    assert delivered.status == "delivered"
    assert delivered.delivered_at == 2000.0
    remaining = service.list_pending_for_session(_session())
    assert [notification.body for notification in remaining] == [
        "Второе уведомление"
    ]
    messages = db.list_agent_dm_messages(OWNER_ID, "alpha_project", "writer_agent")
    assert [message.body for message in messages] == ["Первое уведомление"]
    assert messages[0].sender_kind == "agent"
    assert messages[0].sender_role == "writer_agent"
    assert second.notification_id is not None


def test_ack_delivered_rejects_repeated_delivery(tmp_path):
    db = _db(tmp_path)
    registry = ProjectRegistry(db)
    _register_project(registry)
    db.upsert_agent_dm_session(_session())
    service = AgentOwnerNotificationService(db, clock=lambda: 2100.0)
    notification = service.dispatch_or_queue(_request()).notification

    service.ack_delivered(notification)

    with pytest.raises(ValueError, match="notification_not_queued"):
        service.ack_delivered(
            AgentOwnerNotification(
                notification_id=notification.notification_id,
                owner_user_id=notification.owner_user_id,
                project_id=notification.project_id,
                agent_role=notification.agent_role,
                thread_bot_role=notification.thread_bot_role,
                body=notification.body,
                chat_provider=notification.chat_provider,
                status="queued",
                created_at=notification.created_at,
                delivered_at=None,
            )
        )
