"""Tests for backend-bus public projection into Telegram project chat."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.agent_bus import StateBackedAgentBus
from core.agent_bus_models import AgentMessageRef, AgentReply, AgentRequest
from core.agent_bus_projection import (
    AgentBusProjectionService,
    ProjectingAgentBus,
)
from core.project_models import Project, ProjectChatBinding
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.state_db import StateDB


class CapturingEnvelopeSender:
    def __init__(self, raise_exc: Exception | None = None) -> None:
        self.raise_exc = raise_exc
        self.sent = []

    def __call__(self, envelope) -> None:
        if self.raise_exc is not None:
            raise self.raise_exc
        self.sent.append(envelope)


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


def _binding(**overrides: object) -> ProjectChatBinding:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "chat_id": -1001234567890,
        "chat_provider": "telegram",
    }
    data.update(overrides)
    return ProjectChatBinding(**data)


def _register_project(
    registry: ProjectRegistry,
    *,
    with_binding: bool,
    **project_overrides: object,
) -> ProjectSnapshot:
    project = _project(**project_overrides)
    snapshot = ProjectSnapshot(
        project=project,
        chat_binding=(
            _binding(project_id=project.project_id)
            if with_binding
            else None
        ),
    )
    registry.register_project(snapshot)
    loaded = registry.get_project_snapshot(project.project_id)
    assert loaded is not None
    return loaded


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


def _reply(
    thread_id: str,
    in_reply_to: AgentMessageRef,
    **overrides: object,
) -> AgentReply:
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


def test_resolve_target_returns_project_chat_target(tmp_path: Path):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    _register_project(registry, with_binding=True)
    bus = StateBackedAgentBus(db)
    thread = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
        task_id="task-42",
    )
    service = AgentBusProjectionService(registry, CapturingEnvelopeSender())

    target = service.resolve_target(thread)

    assert target is not None
    assert target.project_id == "alpha_project"
    assert target.chat_id == -1001234567890
    assert target.chat_provider == "telegram"
    assert target.thread_id == thread.thread_id
    assert target.task_id == "task-42"


def test_resolve_target_returns_none_without_chat_binding(tmp_path: Path):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    _register_project(registry, with_binding=False)
    bus = StateBackedAgentBus(db)
    thread = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    service = AgentBusProjectionService(registry, CapturingEnvelopeSender())

    assert service.resolve_target(thread) is None


def test_build_envelope_uses_public_project_chat_transport_semantics(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    _register_project(registry, with_binding=True)
    bus = StateBackedAgentBus(db)
    thread = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
        task_id="task-42",
    )
    message = bus.publish_request(_request(thread.thread_id))
    service = AgentBusProjectionService(registry, CapturingEnvelopeSender())

    envelope = service.build_envelope(thread, message)

    assert envelope is not None
    assert envelope.sender_role == message.sender_role
    assert envelope.delivery_role is None
    assert envelope.message.chat_id == -1001234567890
    assert "writer_agent" in envelope.message.text
    assert "task-42" in envelope.message.text
    assert message.body in envelope.message.text


def test_build_envelope_falls_back_to_thread_id_when_task_id_missing(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    _register_project(registry, with_binding=True)
    bus = StateBackedAgentBus(db)
    thread = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    message = bus.publish_request(_request(thread.thread_id))
    service = AgentBusProjectionService(registry, CapturingEnvelopeSender())

    envelope = service.build_envelope(thread, message)

    assert envelope is not None
    assert thread.thread_id in envelope.message.text
    assert "Задача:" not in envelope.message.text


def test_project_message_projects_request_successfully(tmp_path: Path):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    _register_project(registry, with_binding=True)
    sender = CapturingEnvelopeSender()
    service = AgentBusProjectionService(registry, sender)
    bus = StateBackedAgentBus(db)
    thread = bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    message = bus.publish_request(_request(thread.thread_id))

    result = service.project_message(thread, message)

    assert result.status == "projected"
    assert result.projected_chat_id == -1001234567890
    assert result.failure_reason is None
    assert result.envelope is not None
    assert len(sender.sent) == 1
    assert sender.sent[0] == result.envelope
    assert "Need a first draft" in result.envelope.message.text
    assert "coordinator_agent -> writer_agent" in result.envelope.message.text


def test_project_message_projects_reply_successfully(tmp_path: Path):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    _register_project(registry, with_binding=True)
    sender = CapturingEnvelopeSender()
    service = AgentBusProjectionService(registry, sender)
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
        )
    )

    result = service.project_message(thread, reply)

    assert result.status == "projected"
    assert result.envelope is not None
    assert len(sender.sent) == 1
    assert "writer_agent -> coordinator_agent" in result.envelope.message.text
    assert "Draft is ready" in result.envelope.message.text


def test_projection_prefers_task_id_over_thread_id_in_public_body(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    _register_project(registry, with_binding=True)
    bus = StateBackedAgentBus(db)
    thread = bus.get_or_open_task_thread(
        "alpha_project",
        "task-abc-001",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    message = bus.publish_request(_request(thread.thread_id))
    service = AgentBusProjectionService(registry, CapturingEnvelopeSender())

    body = service.format_public_projection(
        thread,
        message,
        project_slug="alpha-project",
    )

    assert "task-abc-001" in body
    assert thread.thread_id not in body
    assert "alpha-project (alpha_project)" in body


def test_projection_send_failure_returns_failed_result_and_bus_message_survives(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    _register_project(registry, with_binding=True)
    sender = CapturingEnvelopeSender(RuntimeError("telegram down"))
    service = AgentBusProjectionService(registry, sender)
    bus = StateBackedAgentBus(db)
    thread = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    message = bus.publish_request(_request(thread.thread_id))

    result = service.project_message(thread, message)

    assert result.status == "projection_send_failed"
    assert result.envelope is not None
    assert result.projected_chat_id == -1001234567890
    assert result.failure_reason == "RuntimeError:telegram down"
    assert bus.list_thread_messages("alpha_project", thread.thread_id) == (
        message,
    )


def test_no_chat_binding_keeps_backend_message_and_reports_not_projected(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    _register_project(registry, with_binding=False)
    sender = CapturingEnvelopeSender()
    backend_bus = StateBackedAgentBus(db)
    projecting_bus = ProjectingAgentBus(
        backend_bus,
        AgentBusProjectionService(registry, sender),
    )
    thread = projecting_bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )

    result = projecting_bus.publish_request(_request(thread.thread_id))

    assert result.status == "not_projected_no_chat_binding"
    assert result.envelope is None
    assert sender.sent == []
    assert backend_bus.list_thread_messages("alpha_project", thread.thread_id) == (
        result.message,
    )


def test_unsupported_provider_is_reported_truthfully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    snapshot = _register_project(registry, with_binding=True)
    assert snapshot.chat_binding is not None
    object.__setattr__(snapshot.chat_binding, "chat_provider", "slack")
    sender = CapturingEnvelopeSender()
    bus = StateBackedAgentBus(db)
    thread = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    message = bus.publish_request(_request(thread.thread_id))
    monkeypatch.setattr(
        registry,
        "get_project_snapshot",
        lambda project_id: snapshot if project_id == "alpha_project" else None,
    )
    service = AgentBusProjectionService(registry, sender)

    result = service.project_message(thread, message)

    assert service.resolve_target(thread) is None
    assert result.status == "not_projected_unsupported_provider"
    assert result.envelope is None
    assert sender.sent == []


def test_another_projects_chat_binding_does_not_leak(tmp_path: Path):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    _register_project(registry, with_binding=False)
    _register_project(
        registry,
        with_binding=True,
        project_id="beta_project",
        slug="beta-project",
        name="Beta Project",
    )
    sender = CapturingEnvelopeSender()
    backend_bus = StateBackedAgentBus(db)
    projecting_bus = ProjectingAgentBus(
        backend_bus,
        AgentBusProjectionService(registry, sender),
    )
    thread = projecting_bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )

    result = projecting_bus.publish_request(_request(thread.thread_id))

    assert result.status == "not_projected_no_chat_binding"
    assert sender.sent == []


def test_projecting_agent_bus_projects_request_and_reply_after_persist(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    _register_project(registry, with_binding=True)
    sender = CapturingEnvelopeSender()
    backend_bus = StateBackedAgentBus(db)
    projecting_bus = ProjectingAgentBus(
        backend_bus,
        AgentBusProjectionService(registry, sender),
    )
    thread = projecting_bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )

    request_result = projecting_bus.publish_request(_request(thread.thread_id))
    reply_result = projecting_bus.publish_reply(
        _reply(
            thread.thread_id,
            AgentMessageRef(
                project_id=thread.project_id,
                thread_id=thread.thread_id,
                message_id=request_result.message.message_id,
            ),
        )
    )

    assert request_result.status == "projected"
    assert reply_result.status == "projected"
    assert len(sender.sent) == 2
    assert backend_bus.list_thread_messages("alpha_project", thread.thread_id) == (
        request_result.message,
        reply_result.message,
    )
