"""Final proof-pack for the P4 backend bus delivery and projection contour."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.agent_bus import StateBackedAgentBus
from core.agent_bus_models import AgentMessageRef, AgentReply, AgentRequest
from core.agent_bus_projection import AgentBusProjectionService, ProjectingAgentBus
from core.agent_bus_projection_throttle import (
    AgentBusProjectionThrottlePolicy,
    ThrottledProjectingAgentBus,
)
from core.agent_collaboration import (
    AgentCollaborationContext,
    AgentCollaborationPolicy,
    AgentCollaborationService,
)
from core.background_runner import BackgroundTaskRunner
from core.coordinator_role import COORDINATOR_ROLE
from core.dispatcher_agents import build_dispatcher_agent_registry_factory
from core.llm_dispatcher import LLMAttempt, LLMDispatcher, LLMResponse
from core.model_tier import (
    REQUIRED_ROLES,
    TierConfig,
)
from core.model_tier import (
    default_registry as default_tier_registry,
)
from core.project_models import Project, ProjectChatBinding, ProjectPolicy
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.project_runtime import ProjectRuntimeBinding
from core.project_runtime_router import ProjectRuntimeRouter
from core.real_task_handler import make_real_task_handler
from core.sandbox_workspace import (
    SandboxConfig,
    SandboxWorkspace,
    _RunResult,
    _SubprocessRunner,
)
from core.state_db import StateDB
from core.task_history import TaskHistory
from core.telegram_bridge import BridgeReply, IncomingMessage, OutgoingEnvelope
from core.tier_session import TierSessionStore


class CapturingEnvelopeSender:
    def __init__(self) -> None:
        self.sent: list[OutgoingEnvelope] = []

    def __call__(self, envelope: OutgoingEnvelope) -> None:
        self.sent.append(envelope)


class AlwaysFailingEnvelopeSender:
    def __init__(self) -> None:
        self.attempted: list[OutgoingEnvelope] = []

    def __call__(self, envelope: OutgoingEnvelope) -> None:
        self.attempted.append(envelope)
        raise RuntimeError("transport down")


class FailFirstCoordinatorSummarySender:
    def __init__(self) -> None:
        self.sent: list[OutgoingEnvelope] = []
        self.failed_once = False

    def __call__(self, envelope: OutgoingEnvelope) -> None:
        if envelope.sender_role == COORDINATOR_ROLE and not self.failed_once:
            self.failed_once = True
            raise RuntimeError("summary transport down")
        self.sent.append(envelope)


class FakeGitRunner(_SubprocessRunner):
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, cmd, cwd, env, timeout):
        self.calls.append({"cmd": cmd, "cwd": cwd, "timeout": timeout})
        return _RunResult(returncode=0, stdout="", stderr="")


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "main_repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


@pytest.fixture
def sandbox(fake_repo: Path, tmp_path: Path) -> SandboxWorkspace:
    return SandboxWorkspace(
        SandboxConfig(
            main_repo_path=fake_repo,
            worktree_root=tmp_path / "worktrees",
        ),
        runner=FakeGitRunner(),
    )


@pytest.fixture
def tier_store() -> TierSessionStore:
    return TierSessionStore(default_tier_registry())


@pytest.fixture
def runner():
    background_runner = BackgroundTaskRunner()
    yield background_runner
    background_runner.shutdown()


def _make_state_db(tmp_path: Path, name: str) -> StateDB:
    return StateDB(tmp_path / name)


def _project(**overrides: object) -> Project:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "slug": "alpha-project",
        "name": "Alpha Project",
        "description": "Primary project.",
        "owner_user_id": 101,
        "status": "active",
    }
    data.update(overrides)
    return Project(**data)


def _policy(**overrides: object) -> ProjectPolicy:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "allow_hiring": True,
        "allow_agent_dm": False,
        "require_owner_approval_for_hires": True,
    }
    data.update(overrides)
    return ProjectPolicy(**data)


def _binding(**overrides: object) -> ProjectChatBinding:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "chat_provider": "telegram",
        "chat_id": -1001234567890,
    }
    data.update(overrides)
    return ProjectChatBinding(**data)


def _register_project(
    registry: ProjectRegistry,
    *,
    with_binding: bool,
    chat_id: int | None = None,
    **project_overrides: object,
) -> ProjectSnapshot:
    project = _project(**project_overrides)
    snapshot = ProjectSnapshot(
        project=project,
        policy=_policy(project_id=project.project_id),
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


def _make_request(
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


def _make_reply(
    thread_id: str,
    in_reply_to: AgentMessageRef,
    *,
    body: str,
    created_at: float,
    sender_role: str = "reviewer_agent",
    recipient_role: str = "writer_agent",
    project_id: str = "alpha_project",
) -> AgentReply:
    return AgentReply(
        project_id=project_id,
        thread_id=thread_id,
        sender_role=sender_role,
        recipient_role=recipient_role,
        in_reply_to=in_reply_to,
        body=body,
        created_at=created_at,
    )


def _make_tier() -> TierConfig:
    return TierConfig(
        name="test",
        description="test tier",
        estimated_cost_usd=0.5,
        models_per_role={role: ("model-x",) for role in REQUIRED_ROLES},
    )


def _make_dispatcher() -> LLMDispatcher:
    return LLMDispatcher(api_key="sk-test-key-1234")


def _make_response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        model_used="model-x",
        prompt_tokens=11,
        completion_tokens=7,
        attempts=(
            LLMAttempt(model="model-x", ok=True, reason="ok", duration_ms=1),
        ),
    )


def _msg(chat_id: int = 100, text: str = "build me a thing") -> IncomingMessage:
    return IncomingMessage(
        chat_id=chat_id,
        user_id=chat_id,
        message_id=1,
        text=text,
    )


def _runtime_binding(repo_path: Path, **overrides: object) -> ProjectRuntimeBinding:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "adapter_name": "alpha_adapter",
        "repo_path": repo_path,
        "worktree_root": repo_path.parent / "project-worktrees",
        "base_branch": "main",
        "branch_prefix": "feature/",
        "language": "python",
        "rules": (),
        "commands": (),
        "forbidden_paths": (),
        "forbidden_tokens": (),
    }
    data.update(overrides)
    return ProjectRuntimeBinding(**data)


def _project_snapshot(
    repo_path: Path,
    *,
    chat_binding: ProjectChatBinding | None = None,
    **overrides: object,
) -> ProjectSnapshot:
    data: dict[str, object] = {
        "project": _project(),
        "policy": _policy(),
        "chat_binding": chat_binding,
        "runtime_binding": _runtime_binding(repo_path),
    }
    data.update(overrides)
    return ProjectSnapshot(**data)


def _runtime_router_for_snapshot(
    tmp_path: Path,
    snapshot: ProjectSnapshot,
    *,
    db_name: str,
) -> ProjectRuntimeRouter:
    db = StateDB(tmp_path / db_name)
    registry = ProjectRegistry(db)
    registry.register_project(snapshot)
    return ProjectRuntimeRouter(registry, None)


def _make_progress_capture():
    captured: list[tuple[int, str]] = []
    lock = threading.Lock()

    def _send(chat_id: int, text: str) -> None:
        with lock:
            captured.append((chat_id, text))

    return _send, captured


def _make_progress_envelope_capture():
    captured: list[OutgoingEnvelope] = []
    lock = threading.Lock()

    def _send(envelope: OutgoingEnvelope) -> None:
        with lock:
            captured.append(envelope)

    return _send, captured


def _wait_until_idle(background_runner: BackgroundTaskRunner, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while background_runner.is_busy() and time.time() < deadline:
        time.sleep(0.02)


def _wait_for_count(captured, predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate(captured):
            return
        time.sleep(0.02)


def _ok_validation_report():
    from core.quality_gates import CheckResult
    from core.runtime_validator import ValidationReport, ValidationStrategy

    return ValidationReport(
        ok=True,
        strategy=ValidationStrategy.INPLACE,
        checks=(
            CheckResult(
                name="lint",
                ok=True,
                summary="ok",
                raw_output="",
                duration_ms=0,
            ),
        ),
        duration_ms=1,
    )


def test_agent_bus_contour_backend_truth_and_projection_continuity(
    tmp_path: Path,
):
    db = _make_state_db(tmp_path, "contour-backend.db")
    registry = ProjectRegistry(db)
    _register_project(registry, with_binding=True)
    _register_project(
        registry,
        with_binding=False,
        project_id="beta_project",
        slug="beta-project",
        name="Beta Project",
    )
    sender = CapturingEnvelopeSender()

    bus = ProjectingAgentBus(
        StateBackedAgentBus(db),
        AgentBusProjectionService(registry, sender),
    )
    thread = bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role=COORDINATOR_ROLE,
        created_at=1000.0,
    )

    first = bus.publish_request(
        _make_request(
            thread.thread_id,
            body="Нужен черновик API.",
            created_at=1001.0,
            sender_role="planning_agent",
            recipient_role="writer_agent",
        )
    )

    second_bus = ProjectingAgentBus(
        StateBackedAgentBus(db),
        AgentBusProjectionService(registry, sender),
    )
    reply_ref = AgentMessageRef(
        project_id=thread.project_id,
        thread_id=thread.thread_id,
        message_id=first.message.message_id,
    )
    second_bus.publish_reply(
        _make_reply(
            thread.thread_id,
            reply_ref,
            body="Черновик готов.",
            created_at=1002.0,
            sender_role="writer_agent",
            recipient_role="planning_agent",
        )
    )
    third = second_bus.publish_request(
        _make_request(
            thread.thread_id,
            body="Добавь retry и таймауты.",
            created_at=1003.0,
            sender_role="planning_agent",
            recipient_role="writer_agent",
        )
    )

    history = bus.list_thread_messages("alpha_project", thread.thread_id)
    assert tuple(message.message_kind for message in history) == (
        "request",
        "reply",
        "request",
    )
    assert tuple(message.message_id for message in history) == (
        "msg_000001",
        "msg_000002",
        "msg_000003",
    )
    assert history[1].in_reply_to is not None
    assert history[1].in_reply_to.message_id == history[0].message_id
    assert tuple(envelope.sender_role for envelope in sender.sent) == (
        "planning_agent",
        "writer_agent",
        "planning_agent",
    )
    assert all(envelope.delivery_role is None for envelope in sender.sent)
    assert "Маршрут: planning_agent -> writer_agent" in sender.sent[0].message.text
    assert "Задача: task-42" in sender.sent[0].message.text

    failing_sender = AlwaysFailingEnvelopeSender()
    failing_bus = ProjectingAgentBus(
        StateBackedAgentBus(db),
        AgentBusProjectionService(registry, failing_sender),
    )
    failed_reply = failing_bus.publish_reply(
        _make_reply(
            thread.thread_id,
            AgentMessageRef(
                project_id=thread.project_id,
                thread_id=thread.thread_id,
                message_id=third.message.message_id,
            ),
            body="Ответ persisted, но projection transport упал.",
            created_at=1004.0,
            sender_role="writer_agent",
            recipient_role="planning_agent",
        )
    )
    assert failed_reply.status == "projection_send_failed"
    assert failed_reply.envelope is not None
    assert failed_reply.projected_chat_id == -1001234567890
    history_after_failure = bus.list_thread_messages("alpha_project", thread.thread_id)
    assert tuple(message.message_kind for message in history_after_failure) == (
        "request",
        "reply",
        "request",
        "reply",
    )
    assert history_after_failure[-1].body == (
        "Ответ persisted, но projection transport упал."
    )

    beta_thread = bus.open_thread(
        project_id="beta_project",
        opened_by_role=COORDINATOR_ROLE,
        created_at=2000.0,
    )
    beta_result = bus.publish_request(
        _make_request(
            beta_thread.thread_id,
            project_id="beta_project",
            body="Beta sync.",
            created_at=2001.0,
        )
    )
    assert beta_result.status == "not_projected_no_chat_binding"
    assert bus.list_thread_messages("beta_project", beta_thread.thread_id) == (
        beta_result.message,
    )


def test_agent_bus_contour_task_correlation_continuity_and_guardrails(
    tmp_path: Path,
):
    db = _make_state_db(tmp_path, "contour-correlation.db")
    registry = ProjectRegistry(db)
    _register_project(registry, with_binding=True)
    _register_project(
        registry,
        with_binding=True,
        project_id="beta_project",
        slug="beta-project",
        name="Beta Project",
        chat_id=-1001234567891,
    )
    _register_project(
        registry,
        with_binding=False,
        project_id="gamma_project",
        slug="gamma-project",
        name="Gamma Project",
    )
    first_bus = StateBackedAgentBus(db)
    second_bus = StateBackedAgentBus(db)

    thread = first_bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role=COORDINATOR_ROLE,
        created_at=1000.0,
    )
    same_thread = second_bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role=COORDINATOR_ROLE,
        created_at=1001.0,
    )
    other_task = second_bus.get_or_open_task_thread(
        "alpha_project",
        "task-43",
        opened_by_role=COORDINATOR_ROLE,
        created_at=1002.0,
    )
    beta_thread = second_bus.get_or_open_task_thread(
        "beta_project",
        "task-42",
        opened_by_role=COORDINATOR_ROLE,
        created_at=1003.0,
    )

    assert same_thread.thread_id == thread.thread_id
    assert other_task.thread_id != thread.thread_id
    assert beta_thread.thread_id != thread.thread_id

    db.upsert_project_thread(replace(thread, status="closed"))
    with pytest.raises(
        ValueError,
        match="project_task_thread_closed:alpha_project:task-42",
    ):
        first_bus.get_or_open_task_thread(
            "alpha_project",
            "task-42",
            opened_by_role=COORDINATOR_ROLE,
            created_at=1004.0,
        )

    with db._connect() as conn:
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
                "gamma_project",
                "thread_900001",
                COORDINATOR_ROLE,
                "open",
                3000.0,
                3000.0,
                "task-dup",
            ),
        )
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
                "gamma_project",
                "thread_900002",
                COORDINATOR_ROLE,
                "open",
                3001.0,
                3001.0,
                "task-dup",
            ),
        )

    with pytest.raises(
        ValueError,
        match="duplicate_project_task_thread:gamma_project:task-dup",
    ):
        first_bus.get_task_thread("gamma_project", "task-dup")


def test_agent_bus_contour_throttle_summary_flush_and_failure_truthfulness(
    tmp_path: Path,
):
    db = _make_state_db(tmp_path, "contour-throttle.db")
    registry = ProjectRegistry(db)
    _register_project(registry, with_binding=True)
    _register_project(
        registry,
        with_binding=True,
        project_id="beta_project",
        slug="beta-project",
        name="Beta Project",
        chat_id=-1001234567891,
    )
    sender = FailFirstCoordinatorSummarySender()
    policy = AgentBusProjectionThrottlePolicy(
        raw_burst_limit=1,
        summary_batch_size=2,
        burst_window_seconds=30.0,
        preview_chars=24,
    )
    projecting_bus = ProjectingAgentBus(
        StateBackedAgentBus(db),
        AgentBusProjectionService(registry, sender),
    )
    throttled_bus = ThrottledProjectingAgentBus(projecting_bus, policy=policy)
    thread = projecting_bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role=COORDINATOR_ROLE,
        created_at=1000.0,
    )

    first = throttled_bus.publish_request(
        _make_request(
            thread.thread_id,
            body="Первый raw сигнал",
            created_at=1001.0,
            sender_role="writer_agent",
            recipient_role="reviewer_agent",
        )
    )
    second = throttled_bus.publish_request(
        _make_request(
            thread.thread_id,
            body="Подавленный хвост",
            created_at=1002.0,
            sender_role="writer_agent",
            recipient_role="reviewer_agent",
        )
    )
    third = throttled_bus.publish_request(
        _make_request(
            thread.thread_id,
            body="Новая волна после тишины",
            created_at=1100.0,
            sender_role="reviewer_agent",
            recipient_role="writer_agent",
        )
    )

    assert first.projection_results[0].status == "projected"
    assert second.projection_results == ()
    assert tuple(result.status for result in third.projection_results) == (
        "projection_send_failed",
        "projected",
    )
    assert throttled_bus.pending_summary_count("alpha_project", thread.thread_id) == 1
    assert third.projection_results[1].envelope is not None
    assert third.projection_results[1].envelope.sender_role == "reviewer_agent"
    history = projecting_bus.list_thread_messages("alpha_project", thread.thread_id)
    assert tuple(message.body for message in history) == (
        "Первый raw сигнал",
        "Подавленный хвост",
        "Новая волна после тишины",
    )

    flush_one = throttled_bus.flush_thread("alpha_project", thread.thread_id)
    assert len(flush_one) == 1
    assert flush_one[0].status == "projected"
    assert flush_one[0].envelope is not None
    assert flush_one[0].envelope.sender_role == COORDINATOR_ROLE
    assert throttled_bus.pending_summary_count("alpha_project", thread.thread_id) == 0
    assert projecting_bus.list_thread_messages("alpha_project", thread.thread_id) == history

    alpha_second_thread = projecting_bus.open_thread(
        project_id="alpha_project",
        opened_by_role=COORDINATOR_ROLE,
        created_at=1200.0,
    )
    beta_thread = projecting_bus.open_thread(
        project_id="beta_project",
        opened_by_role=COORDINATOR_ROLE,
        created_at=1300.0,
    )
    throttled_bus.publish_request(
        _make_request(
            alpha_second_thread.thread_id,
            body="alpha-raw",
            created_at=1201.0,
        )
    )
    throttled_bus.publish_request(
        _make_request(
            alpha_second_thread.thread_id,
            body="alpha-tail",
            created_at=1202.0,
        )
    )
    throttled_bus.publish_request(
        _make_request(
            beta_thread.thread_id,
            project_id="beta_project",
            body="beta-raw",
            created_at=1301.0,
        )
    )
    throttled_bus.publish_request(
        _make_request(
            beta_thread.thread_id,
            project_id="beta_project",
            body="beta-tail",
            created_at=1302.0,
        )
    )

    flush_all = throttled_bus.flush_all()
    assert [result.thread.thread_id for result in flush_all] == [
        alpha_second_thread.thread_id,
        beta_thread.thread_id,
    ]
    assert all(result.status == "projected" for result in flush_all)
    assert all(
        result.envelope is not None
        and result.envelope.sender_role == COORDINATOR_ROLE
        for result in flush_all
    )


def test_agent_bus_contour_collaboration_roundtrip_and_nested_failure(
    tmp_path: Path,
):
    db = _make_state_db(tmp_path, "contour-collaboration.db")
    registry = ProjectRegistry(db)
    _register_project(registry, with_binding=True)
    sender = CapturingEnvelopeSender()
    projecting_bus = ProjectingAgentBus(
        StateBackedAgentBus(db),
        AgentBusProjectionService(registry, sender),
    )
    throttled_bus = ThrottledProjectingAgentBus(
        projecting_bus,
        policy=AgentBusProjectionThrottlePolicy(
            raw_burst_limit=10,
            summary_batch_size=3,
        ),
    )
    thread = projecting_bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role=COORDINATOR_ROLE,
        created_at=1000.0,
    )
    context = AgentCollaborationContext(
        project_id="alpha_project",
        task_id="task-42",
        thread=thread,
        caller_role="writer_agent",
        owner_task_text="Собери API для billing.",
    )
    request = AgentCollaborationService(
        throttled_bus,
        _make_dispatcher(),
        _make_tier(),
    ).parse_consultation_request(
        '{"action":"ask_another_agent","recipient_role":"reviewer_agent","question":"Проверь риски API"}'
    )
    assert request is not None

    dispatcher = _make_dispatcher()
    dispatcher.dispatch = MagicMock(return_value=_make_response("Короткий экспертный ответ"))  # type: ignore[method-assign]
    service = AgentCollaborationService(throttled_bus, dispatcher, _make_tier())

    first = service.run_consultation(context, request, created_at=1000.0)
    second = service.run_consultation(context, request, created_at=1000.0005)

    messages = projecting_bus.list_thread_messages("alpha_project", thread.thread_id)
    assert tuple(message.message_kind for message in messages) == (
        "request",
        "reply",
        "request",
        "reply",
    )
    assert second.request_message.created_at >= first.reply_message.created_at
    assert second.reply_message.in_reply_to is not None
    assert (
        second.reply_message.in_reply_to.message_id
        == second.request_message.message_id
    )
    assert all(envelope.delivery_role is None for envelope in sender.sent)
    assert any(
        envelope.sender_role == "writer_agent" for envelope in sender.sent
    )
    assert any(
        envelope.sender_role == "reviewer_agent" for envelope in sender.sent
    )

    nested_dispatcher = _make_dispatcher()
    nested_dispatcher.dispatch = MagicMock(
        return_value=_make_response(
            '{"action":"ask_another_agent","recipient_role":"architect_agent","question":"Ещё один вопрос"}'
        )
    )  # type: ignore[method-assign]
    nested_service = AgentCollaborationService(
        throttled_bus,
        nested_dispatcher,
        _make_tier(),
    )

    with pytest.raises(
        ValueError,
        match="nested_consultation_not_allowed:reviewer_agent",
    ):
        nested_service.run_consultation(context, request, created_at=1001.0)

    history_after_failure = projecting_bus.list_thread_messages(
        "alpha_project",
        thread.thread_id,
    )
    assert tuple(message.message_kind for message in history_after_failure) == (
        "request",
        "reply",
        "request",
        "reply",
        "request",
    )
    assert history_after_failure[-1].sender_role == "writer_agent"
    assert history_after_failure[-1].recipient_role == "reviewer_agent"


def test_agent_bus_contour_dispatcher_registry_preserves_final_payload_and_limit(
    tmp_path: Path,
):
    db = _make_state_db(tmp_path, "contour-dispatcher.db")
    registry = ProjectRegistry(db)
    _register_project(registry, with_binding=True)
    sender = CapturingEnvelopeSender()
    projecting_bus = ProjectingAgentBus(
        StateBackedAgentBus(db),
        AgentBusProjectionService(registry, sender),
    )
    thread = projecting_bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role=COORDINATOR_ROLE,
        created_at=1000.0,
    )

    dispatcher = _make_dispatcher()
    planning_calls = 0

    def _dispatch(req, _tier):
        nonlocal planning_calls
        if req.agent_role == "planning_agent":
            planning_calls += 1
            if planning_calls == 1:
                return _make_response(
                    '{"action":"ask_another_agent","recipient_role":"reviewer_agent","question":"Нужен риск-анализ"}'
                )
            assert "INTERNAL CONSULTATION TRANSCRIPT" in req.messages[1]["content"]
            return _make_response('{"plan":"ok"}')
        if req.agent_role == "reviewer_agent":
            assert "INTERNAL CONSULTATION MODE" in req.messages[0]["content"]
            return _make_response("Главный риск — silent regression.")
        raise AssertionError(f"unexpected role {req.agent_role}")

    dispatcher.dispatch = MagicMock(side_effect=_dispatch)  # type: ignore[method-assign]
    factory = build_dispatcher_agent_registry_factory(dispatcher)
    collaboration_registry = factory.build_collaboration_registry(
        _make_tier(),
        project_id="alpha_project",
        task_id="task-42",
        thread=thread,
        owner_task_text="Собери API для billing.",
        bus=projecting_bus,
    )

    result = collaboration_registry["planning_agent"]("Собери API для billing.")

    assert result == '{"plan":"ok"}'
    thread_messages = projecting_bus.list_thread_messages(
        "alpha_project",
        thread.thread_id,
    )
    assert tuple(message.message_kind for message in thread_messages) == (
        "request",
        "reply",
    )

    second_db = _make_state_db(tmp_path, "contour-dispatcher-limit.db")
    second_registry = ProjectRegistry(second_db)
    _register_project(second_registry, with_binding=True)
    second_projecting_bus = ProjectingAgentBus(
        StateBackedAgentBus(second_db),
        AgentBusProjectionService(second_registry, CapturingEnvelopeSender()),
    )
    second_thread = second_projecting_bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role=COORDINATOR_ROLE,
        created_at=1000.0,
    )
    limit_dispatcher = _make_dispatcher()
    limit_dispatcher.dispatch = MagicMock(
        side_effect=lambda req, _tier: _make_response(
            '{"action":"ask_another_agent","recipient_role":"reviewer_agent","question":"ещё одна консультация"}'
            if req.agent_role == "planning_agent"
            else "Короткий ответ reviewer"
        )
    )  # type: ignore[method-assign]
    limit_factory = build_dispatcher_agent_registry_factory(limit_dispatcher)
    limit_registry = limit_factory.build_collaboration_registry(
        _make_tier(),
        project_id="alpha_project",
        task_id="task-42",
        thread=second_thread,
        owner_task_text="Собери API для billing.",
        bus=second_projecting_bus,
        policy=AgentCollaborationPolicy(max_consultations_per_call=1),
    )

    with pytest.raises(
        ValueError,
        match="consultation_limit_exceeded:planning_agent",
    ):
        limit_registry["planning_agent"]("Собери API для billing.")

    limit_messages = second_projecting_bus.list_thread_messages(
        "alpha_project",
        second_thread.thread_id,
    )
    assert tuple(message.message_kind for message in limit_messages) == (
        "request",
        "reply",
    )


def test_agent_bus_contour_real_handler_project_and_legacy_runtime_paths(
    runner,
    sandbox,
    tier_store,
    fake_repo: Path,
    tmp_path: Path,
):
    tier_store.set_active(-100123, "STANDARD")
    send_envelope, captured_envelopes = _make_progress_envelope_capture()
    task_history = TaskHistory()
    snapshot = _project_snapshot(
        fake_repo,
        chat_binding=_binding(chat_id=-100123),
    )
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="contour-runtime.db",
    )
    dispatcher = _make_dispatcher()
    factory = build_dispatcher_agent_registry_factory(dispatcher)
    planning_calls = 0

    def _project_dispatch(req, _tier):
        nonlocal planning_calls
        if req.agent_role == "planning_agent":
            planning_calls += 1
            if planning_calls == 1:
                return _make_response(
                    '{"action":"ask_another_agent","recipient_role":"reviewer_agent","question":"Нужен риск-анализ по API"}'
                )
            assert "INTERNAL CONSULTATION TRANSCRIPT" in req.messages[1]["content"]
            return _make_response('{"plan":"ok"}')
        if req.agent_role == "pm_agent":
            return _make_response('{"tasks":[]}')
        if req.agent_role == "architect_agent":
            return _make_response('{"arch":"spec"}')
        if req.agent_role == "writer_agent":
            return _make_response("def f(): return 42")
        if req.agent_role == "reviewer_agent":
            if "INTERNAL CONSULTATION MODE" in req.messages[0]["content"]:
                return _make_response("Главный риск — silent regression.")
            return _make_response('{"verdict":"APPROVED"}')
        if req.agent_role == "tester_agent":
            return _make_response("tests pass")
        if req.agent_role == "qa_agent":
            return _make_response('{"verdict":"PASS"}')
        if req.agent_role == "fixer_agent":
            return _make_response("def f(): return 42")
        raise AssertionError(f"unexpected role {req.agent_role}")

    dispatcher.dispatch = MagicMock(side_effect=_project_dispatch)  # type: ignore[method-assign]
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    with (
        patch("core.project_runtime_router._build_sandbox", return_value=sandbox),
        patch("core.real_task_handler.make_sandbox_hook", return_value=mock_hook_fn),
        patch.object(sandbox, "commit_in_worktree", return_value="abc123def456789"),
    ):
        project_handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress_envelope=send_envelope,
            agent_registry_factory=factory,
            task_id_factory=lambda: "task-42",
            task_history=task_history,
        )
        project_reply = project_handler(
            "Собери безопасный API для billing.",
            IncomingMessage(
                chat_id=-100123,
                user_id=777,
                message_id=1,
                text="Собери безопасный API для billing.",
                project_id="alpha_project",
                project_slug="alpha-project",
                project_context_source="bound_chat",
            ),
        )
        assert isinstance(project_reply, BridgeReply)
        _wait_until_idle(runner)
        _wait_for_count(
            captured_envelopes,
            lambda envelopes: any(
                "Готово" in envelope.message.text for envelope in envelopes
            ),
        )

    backend_bus = StateBackedAgentBus(runtime_router.registry.state_db)
    thread = backend_bus.get_task_thread("alpha_project", "task-42")
    assert thread is not None
    thread_messages = backend_bus.list_thread_messages(
        "alpha_project",
        thread.thread_id,
    )
    assert tuple(message.message_kind for message in thread_messages) == (
        "request",
        "reply",
    )
    assert any(
        "Маршрут: planning_agent -> reviewer_agent" in envelope.message.text
        for envelope in captured_envelopes
    )
    assert any(
        "Маршрут: reviewer_agent -> planning_agent" in envelope.message.text
        for envelope in captured_envelopes
    )
    assert any(
        envelope.sender_role == "planning_agent"
        and envelope.delivery_role is None
        for envelope in captured_envelopes
    )
    assert any(
        envelope.sender_role == "reviewer_agent"
        and envelope.delivery_role is None
        for envelope in captured_envelopes
    )
    summary = task_history.get("task-42")
    assert summary is not None
    assert summary.final_state == "SUCCESS"

    tier_store.set_active(42, "STANDARD")
    send_progress, captured_progress = _make_progress_capture()
    legacy_dispatcher = _make_dispatcher()
    legacy_factory = build_dispatcher_agent_registry_factory(legacy_dispatcher)

    def _legacy_dispatch(req, _tier):
        payloads = {
            "planning_agent": '{"plan":"ok"}',
            "pm_agent": '{"tasks":[]}',
            "architect_agent": '{"arch":"spec"}',
            "writer_agent": "def f(): return 42",
            "reviewer_agent": '{"verdict":"APPROVED"}',
            "tester_agent": "tests pass",
            "qa_agent": '{"verdict":"PASS"}',
            "fixer_agent": "def f(): return 42",
        }
        return _make_response(payloads[req.agent_role])

    legacy_dispatcher.dispatch = MagicMock(side_effect=_legacy_dispatch)  # type: ignore[method-assign]
    legacy_mock_report = _ok_validation_report()
    legacy_mock_hook_fn = MagicMock(return_value=legacy_mock_report)

    with (
        patch("core.real_task_handler.make_sandbox_hook", return_value=legacy_mock_hook_fn),
        patch.object(sandbox, "commit_in_worktree", return_value="abc123def456789"),
    ):
        legacy_handler = make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send_progress,
            agent_registry_factory=legacy_factory,
            task_id_factory=lambda: "task-plain-42",
        )
        legacy_reply = legacy_handler("Сделай CLI tool.", _msg(chat_id=42))
        assert isinstance(legacy_reply, BridgeReply)
        _wait_until_idle(runner)
        _wait_for_count(
            captured_progress,
            lambda rows: any("Готово" in text for _, text in rows),
        )

    assert not any("Маршрут:" in text for _, text in captured_progress)
