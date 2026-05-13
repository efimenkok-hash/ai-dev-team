"""Tests for throttled public projection of backend agent-bus exchange."""

from __future__ import annotations

from pathlib import Path

from core.agent_bus import StateBackedAgentBus
from core.agent_bus_models import AgentRequest
from core.agent_bus_projection import AgentBusProjectionService, ProjectingAgentBus
from core.agent_bus_projection_throttle import (
    AgentBusProjectionThrottlePolicy,
    ThrottledProjectingAgentBus,
)
from core.coordinator_role import COORDINATOR_ROLE
from core.project_models import Project, ProjectChatBinding
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.state_db import StateDB


class CapturingEnvelopeSender:
    def __init__(self) -> None:
        self.sent = []

    def __call__(self, envelope) -> None:
        self.sent.append(envelope)


class SummaryFailingEnvelopeSender:
    def __init__(self) -> None:
        self.sent = []

    def __call__(self, envelope) -> None:
        if envelope.sender_role == COORDINATOR_ROLE:
            raise RuntimeError("summary transport down")
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
    chat_id = project_overrides.pop("chat_id", None)
    project = _project(**project_overrides)
    snapshot = ProjectSnapshot(
        project=project,
        chat_binding=(
            _binding(
                project_id=project.project_id,
                **({} if chat_id is None else {"chat_id": chat_id}),
            )
            if with_binding
            else None
        ),
    )
    registry.register_project(snapshot)
    loaded = registry.get_project_snapshot(project.project_id)
    assert loaded is not None
    return loaded


def _request(
    thread_id: str,
    *,
    body: str,
    created_at: float,
    sender_role: str = "writer_agent",
    recipient_role: str = "reviewer_agent",
    project_id: str = "alpha_project",
) -> AgentRequest:
    return AgentRequest(
        project_id=project_id,
        thread_id=thread_id,
        sender_role=sender_role,
        recipient_role=recipient_role,
        body=body,
        created_at=created_at,
    )


def _make_throttled_bus(
    tmp_path: Path,
    *,
    with_binding: bool = True,
    sender=None,
    policy: AgentBusProjectionThrottlePolicy | None = None,
    **project_overrides: object,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    _register_project(
        registry,
        with_binding=with_binding,
        **project_overrides,
    )
    envelope_sender = sender if sender is not None else CapturingEnvelopeSender()
    backend_bus = StateBackedAgentBus(db)
    projection_service = AgentBusProjectionService(registry, envelope_sender)
    projecting_bus = ProjectingAgentBus(backend_bus, projection_service)
    throttled_bus = ThrottledProjectingAgentBus(projecting_bus, policy=policy)
    return throttled_bus, projecting_bus, backend_bus, envelope_sender


def test_raw_burst_limit_projects_first_messages_individually(tmp_path: Path):
    policy = AgentBusProjectionThrottlePolicy(
        raw_burst_limit=2,
        summary_batch_size=3,
    )
    throttled_bus, projecting_bus, backend_bus, sender = _make_throttled_bus(
        tmp_path,
        policy=policy,
    )
    thread = projecting_bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )

    first = throttled_bus.publish_request(
        _request(
            thread.thread_id,
            body="Первый raw запрос",
            created_at=1001.0,
            sender_role="writer_agent",
            recipient_role="reviewer_agent",
        )
    )
    second = throttled_bus.publish_request(
        _request(
            thread.thread_id,
            body="Второй raw запрос",
            created_at=1002.0,
            sender_role="reviewer_agent",
            recipient_role="writer_agent",
        )
    )

    assert first.projection_results[0].status == "projected"
    assert second.projection_results[0].status == "projected"
    assert first.projection_results[0].envelope is not None
    assert second.projection_results[0].envelope is not None
    assert first.projection_results[0].envelope.sender_role == "writer_agent"
    assert second.projection_results[0].envelope.sender_role == "reviewer_agent"
    assert sender.sent[0].sender_role == "writer_agent"
    assert sender.sent[1].sender_role == "reviewer_agent"
    assert backend_bus.list_thread_messages("alpha_project", thread.thread_id) == (
        first.source_message,
        second.source_message,
    )


def test_suppression_then_summary_emits_coordinator_compaction(tmp_path: Path):
    policy = AgentBusProjectionThrottlePolicy(
        raw_burst_limit=2,
        summary_batch_size=3,
        preview_chars=20,
    )
    throttled_bus, projecting_bus, backend_bus, sender = _make_throttled_bus(
        tmp_path,
        policy=policy,
    )
    thread = projecting_bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )

    first = throttled_bus.publish_request(
        _request(
            thread.thread_id,
            body="Raw 1",
            created_at=1001.0,
            sender_role="writer_agent",
            recipient_role="reviewer_agent",
        )
    )
    second = throttled_bus.publish_request(
        _request(
            thread.thread_id,
            body="Raw 2",
            created_at=1002.0,
            sender_role="reviewer_agent",
            recipient_role="writer_agent",
        )
    )
    third = throttled_bus.publish_request(
        _request(
            thread.thread_id,
            body="Третий suppressed запрос",
            created_at=1003.0,
            sender_role="writer_agent",
            recipient_role="reviewer_agent",
        )
    )
    fourth = throttled_bus.publish_request(
        _request(
            thread.thread_id,
            body="Четвёртый suppressed запрос",
            created_at=1004.0,
            sender_role="reviewer_agent",
            recipient_role="writer_agent",
        )
    )
    fifth = throttled_bus.publish_request(
        _request(
            thread.thread_id,
            body="Пятый suppressed запрос с очень длинным текстом для preview truncation.",
            created_at=1005.0,
            sender_role="architect_agent",
            recipient_role="writer_agent",
        )
    )

    assert third.projection_results == ()
    assert fourth.projection_results == ()
    assert third.suppressed_messages == (third.source_message,)
    assert fourth.suppressed_messages == (fourth.source_message,)
    assert len(fifth.projection_results) == 1
    summary = fifth.projection_results[0]
    assert summary.status == "projected"
    assert summary.envelope is not None
    assert summary.envelope.sender_role == COORDINATOR_ROLE
    assert len(fifth.suppressed_messages) == 3
    assert fifth.suppressed_messages == (
        third.source_message,
        fourth.source_message,
        fifth.source_message,
    )
    assert "Сообщений: 3" in summary.envelope.message.text
    assert "task-42" in summary.envelope.message.text
    assert "writer_agent -> reviewer_agent" in summary.envelope.message.text
    assert "reviewer_agent -> writer_agent" in summary.envelope.message.text
    assert "architect_agent -> writer_agent" in summary.envelope.message.text
    assert "Пятый suppressed за…" in summary.envelope.message.text
    assert len(sender.sent) == 3
    assert backend_bus.list_thread_messages("alpha_project", thread.thread_id) == (
        first.source_message,
        second.source_message,
        third.source_message,
        fourth.source_message,
        fifth.source_message,
    )


def test_other_thread_and_project_do_not_share_suppression_state(
    tmp_path: Path,
):
    policy = AgentBusProjectionThrottlePolicy(
        raw_burst_limit=1,
        summary_batch_size=2,
    )
    throttled_bus, projecting_bus, backend_bus, _sender = _make_throttled_bus(
        tmp_path,
        policy=policy,
    )
    registry = projecting_bus.projection_service.project_registry
    _register_project(
        registry,
        with_binding=True,
        project_id="beta_project",
        slug="beta-project",
        name="Beta Project",
        chat_id=-1001234567891,
    )
    alpha_first = projecting_bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    alpha_second = projecting_bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1001.0,
    )
    beta_first = projecting_bus.open_thread(
        project_id="beta_project",
        opened_by_role="coordinator_agent",
        created_at=1002.0,
    )

    throttled_bus.publish_request(
        _request(alpha_first.thread_id, body="alpha-1 raw", created_at=1003.0)
    )
    throttled_bus.publish_request(
        _request(alpha_first.thread_id, body="alpha-1 suppressed", created_at=1004.0)
    )
    alpha_summary = throttled_bus.publish_request(
        _request(alpha_first.thread_id, body="alpha-1 summary", created_at=1005.0)
    )
    alpha_second_raw = throttled_bus.publish_request(
        _request(alpha_second.thread_id, body="alpha-2 raw", created_at=1006.0)
    )
    beta_raw = throttled_bus.publish_request(
        _request(
            beta_first.thread_id,
            body="beta-1 raw",
            created_at=1007.0,
            project_id="beta_project",
        )
    )

    assert alpha_summary.projection_results[0].envelope is not None
    assert alpha_summary.projection_results[0].envelope.sender_role == COORDINATOR_ROLE
    assert alpha_second_raw.projection_results[0].status == "projected"
    assert beta_raw.projection_results[0].status == "projected"
    assert throttled_bus.pending_summary_count("alpha_project", alpha_second.thread_id) == 0
    assert throttled_bus.pending_summary_count("beta_project", beta_first.thread_id) == 0
    assert len(backend_bus.list_thread_messages("alpha_project", alpha_first.thread_id)) == 3
    assert len(backend_bus.list_thread_messages("alpha_project", alpha_second.thread_id)) == 1
    assert len(backend_bus.list_thread_messages("beta_project", beta_first.thread_id)) == 1


def test_flush_thread_emits_summary_for_pending_tail(tmp_path: Path):
    policy = AgentBusProjectionThrottlePolicy(
        raw_burst_limit=1,
        summary_batch_size=3,
    )
    throttled_bus, projecting_bus, backend_bus, sender = _make_throttled_bus(
        tmp_path,
        policy=policy,
    )
    thread = projecting_bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )

    first = throttled_bus.publish_request(
        _request(thread.thread_id, body="raw", created_at=1001.0)
    )
    second = throttled_bus.publish_request(
        _request(thread.thread_id, body="suppressed 1", created_at=1002.0)
    )
    third = throttled_bus.publish_request(
        _request(thread.thread_id, body="suppressed 2", created_at=1003.0)
    )

    flushed = throttled_bus.flush_thread("alpha_project", thread.thread_id)

    assert first.projection_results[0].status == "projected"
    assert second.projection_results == ()
    assert third.projection_results == ()
    assert len(flushed) == 1
    assert flushed[0].status == "projected"
    assert flushed[0].envelope is not None
    assert flushed[0].envelope.sender_role == COORDINATOR_ROLE
    assert "Сообщений: 2" in flushed[0].envelope.message.text
    assert throttled_bus.pending_summary_count("alpha_project", thread.thread_id) == 0
    assert throttled_bus.flush_thread("alpha_project", thread.thread_id) == ()
    assert len(backend_bus.list_thread_messages("alpha_project", thread.thread_id)) == 3
    assert len(sender.sent) == 2


def test_idle_gap_closes_stale_suppressed_tail_and_starts_new_raw_burst(
    tmp_path: Path,
):
    policy = AgentBusProjectionThrottlePolicy(
        raw_burst_limit=1,
        summary_batch_size=3,
        burst_window_seconds=30.0,
    )
    throttled_bus, projecting_bus, backend_bus, sender = _make_throttled_bus(
        tmp_path,
        policy=policy,
    )
    thread = projecting_bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )

    raw = throttled_bus.publish_request(
        _request(thread.thread_id, body="raw", created_at=1001.0)
    )
    suppressed = throttled_bus.publish_request(
        _request(thread.thread_id, body="suppressed tail", created_at=1002.0)
    )
    after_gap = throttled_bus.publish_request(
        _request(thread.thread_id, body="new raw after quiet", created_at=1100.0)
    )

    assert raw.projection_results[0].status == "projected"
    assert suppressed.projection_results == ()
    assert throttled_bus.pending_summary_count("alpha_project", thread.thread_id) == 0
    assert len(after_gap.projection_results) == 2
    stale_summary, resumed_raw = after_gap.projection_results
    assert stale_summary.status == "projected"
    assert stale_summary.envelope is not None
    assert stale_summary.envelope.sender_role == COORDINATOR_ROLE
    assert "Сообщений: 1" in stale_summary.envelope.message.text
    assert "suppressed tail" in stale_summary.envelope.message.text
    assert resumed_raw.status == "projected"
    assert resumed_raw.envelope is not None
    assert resumed_raw.envelope.sender_role == "writer_agent"
    assert "new raw after quiet" in resumed_raw.envelope.message.text
    assert after_gap.suppressed_messages == (suppressed.source_message,)
    assert len(sender.sent) == 3
    assert sender.sent[-1].sender_role == "writer_agent"
    assert backend_bus.list_thread_messages("alpha_project", thread.thread_id) == (
        raw.source_message,
        suppressed.source_message,
        after_gap.source_message,
    )


def test_idle_gap_summary_failure_preserves_pending_tail_and_new_raw_visibility(
    tmp_path: Path,
):
    policy = AgentBusProjectionThrottlePolicy(
        raw_burst_limit=1,
        summary_batch_size=3,
        burst_window_seconds=30.0,
    )
    throttled_bus, projecting_bus, backend_bus, sender = _make_throttled_bus(
        tmp_path,
        policy=policy,
        sender=SummaryFailingEnvelopeSender(),
    )
    thread = projecting_bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )

    raw = throttled_bus.publish_request(
        _request(thread.thread_id, body="raw", created_at=1001.0)
    )
    suppressed = throttled_bus.publish_request(
        _request(thread.thread_id, body="suppressed tail", created_at=1002.0)
    )
    after_gap = throttled_bus.publish_request(
        _request(thread.thread_id, body="new raw after quiet", created_at=1100.0)
    )

    assert raw.projection_results[0].status == "projected"
    assert suppressed.projection_results == ()
    assert len(after_gap.projection_results) == 2
    stale_summary, resumed_raw = after_gap.projection_results
    assert stale_summary.status == "projection_send_failed"
    assert stale_summary.failure_reason == "RuntimeError:summary transport down"
    assert resumed_raw.status == "projected"
    assert resumed_raw.envelope is not None
    assert resumed_raw.envelope.sender_role == "writer_agent"
    assert "new raw after quiet" in resumed_raw.envelope.message.text
    assert throttled_bus.pending_summary_count("alpha_project", thread.thread_id) == 1
    assert after_gap.suppressed_messages == (suppressed.source_message,)
    assert len(sender.sent) == 2
    assert sender.sent[0].sender_role == "writer_agent"
    assert sender.sent[1].sender_role == "writer_agent"
    assert backend_bus.list_thread_messages("alpha_project", thread.thread_id) == (
        raw.source_message,
        suppressed.source_message,
        after_gap.source_message,
    )


def test_flush_all_emits_summaries_in_deterministic_order(tmp_path: Path):
    policy = AgentBusProjectionThrottlePolicy(
        raw_burst_limit=1,
        summary_batch_size=3,
    )
    throttled_bus, projecting_bus, backend_bus, sender = _make_throttled_bus(
        tmp_path,
        policy=policy,
    )
    registry = projecting_bus.projection_service.project_registry
    _register_project(
        registry,
        with_binding=True,
        project_id="beta_project",
        slug="beta-project",
        name="Beta Project",
        chat_id=-1001234567891,
    )
    alpha = projecting_bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    beta = projecting_bus.open_thread(
        project_id="beta_project",
        opened_by_role="coordinator_agent",
        created_at=1001.0,
    )
    throttled_bus.publish_request(
        _request(alpha.thread_id, body="alpha raw", created_at=1002.0)
    )
    throttled_bus.publish_request(
        _request(alpha.thread_id, body="alpha suppressed", created_at=1003.0)
    )
    throttled_bus.publish_request(
        _request(
            beta.thread_id,
            body="beta raw",
            created_at=1004.0,
            project_id="beta_project",
        )
    )
    throttled_bus.publish_request(
        _request(
            beta.thread_id,
            body="beta suppressed",
            created_at=1005.0,
            project_id="beta_project",
        )
    )

    flushed = throttled_bus.flush_all()

    assert len(flushed) == 2
    assert [(result.thread.project_id, result.thread.thread_id) for result in flushed] == [
        ("alpha_project", alpha.thread_id),
        ("beta_project", beta.thread_id),
    ]
    assert all(result.status == "projected" for result in flushed)
    assert len(backend_bus.list_thread_messages("alpha_project", alpha.thread_id)) == 2
    assert len(backend_bus.list_thread_messages("beta_project", beta.thread_id)) == 2
    assert throttled_bus.flush_all() == ()
    assert len(sender.sent) == 4


def test_summary_send_failure_keeps_pending_state_and_backend_history(
    tmp_path: Path,
):
    policy = AgentBusProjectionThrottlePolicy(
        raw_burst_limit=1,
        summary_batch_size=2,
    )
    throttled_bus, projecting_bus, backend_bus, sender = _make_throttled_bus(
        tmp_path,
        policy=policy,
        sender=SummaryFailingEnvelopeSender(),
    )
    thread = projecting_bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )

    raw = throttled_bus.publish_request(
        _request(thread.thread_id, body="raw", created_at=1001.0)
    )
    throttled_bus.publish_request(
        _request(thread.thread_id, body="suppressed 1", created_at=1002.0)
    )
    failed = throttled_bus.publish_request(
        _request(thread.thread_id, body="suppressed 2", created_at=1003.0)
    )

    assert raw.projection_results[0].status == "projected"
    assert len(failed.projection_results) == 1
    assert failed.projection_results[0].status == "projection_send_failed"
    assert failed.projection_results[0].failure_reason == "RuntimeError:summary transport down"
    assert throttled_bus.pending_summary_count("alpha_project", thread.thread_id) == 2
    retry = throttled_bus.flush_thread("alpha_project", thread.thread_id)
    assert len(retry) == 1
    assert retry[0].status == "projection_send_failed"
    assert throttled_bus.pending_summary_count("alpha_project", thread.thread_id) == 2
    assert len(backend_bus.list_thread_messages("alpha_project", thread.thread_id)) == 3
    assert len(sender.sent) == 1


def test_no_chat_binding_path_remains_truthful_under_summary_flow(
    tmp_path: Path,
):
    policy = AgentBusProjectionThrottlePolicy(
        raw_burst_limit=1,
        summary_batch_size=2,
    )
    throttled_bus, projecting_bus, backend_bus, sender = _make_throttled_bus(
        tmp_path,
        with_binding=False,
        policy=policy,
    )
    thread = projecting_bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )

    first = throttled_bus.publish_request(
        _request(thread.thread_id, body="raw no target", created_at=1001.0)
    )
    second = throttled_bus.publish_request(
        _request(thread.thread_id, body="suppressed no target 1", created_at=1002.0)
    )
    third = throttled_bus.publish_request(
        _request(thread.thread_id, body="suppressed no target 2", created_at=1003.0)
    )

    assert first.projection_results[0].status == "not_projected_no_chat_binding"
    assert second.projection_results == ()
    assert len(third.projection_results) == 1
    assert third.projection_results[0].status == "not_projected_no_chat_binding"
    assert throttled_bus.pending_summary_count("alpha_project", thread.thread_id) == 2
    assert sender.sent == []
    assert len(backend_bus.list_thread_messages("alpha_project", thread.thread_id)) == 3
