"""Tests for core.real_task_handler (Step 14b-5: bridge↔pipeline integration).

These tests use:
  * A fake _SubprocessRunner inside SandboxWorkspace so no real git is run.
  * A real BackgroundTaskRunner (single worker thread) but with very short
    fake agent calls — total runtime per test stays under a second.
  * Predictable agent registries (happy-path JSON, noop, exploding) to drive
    Orchestrator into SUCCESS / FAIL / agent-exception code paths without
    touching any LLM.
"""

import threading
import time
from pathlib import Path

import pytest

from core.agent_bus import StateBackedAgentBus
from core.background_runner import BackgroundTaskRunner
from core.coordinator_role import COORDINATOR_ROLE
from core.dispatcher_agents import build_dispatcher_agent_registry_factory
from core.llm_dispatcher import LLMAttempt, LLMDispatcher, LLMResponse
from core.memory import PipelineMemory
from core.model_tier import default_registry as default_tier_registry
from core.observability import Observability
from core.progress_emitter import ProgressEvent
from core.project_models import Project, ProjectChatBinding, ProjectPolicy
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.project_runtime import ProjectRuntimeBinding
from core.project_runtime_router import ProjectRuntimeRouter
from core.real_task_handler import (
    RealTaskHandlerConfig,
    _format_event,
    generate_task_id,
    make_real_task_handler,
)
from core.sandbox_workspace import (
    SandboxConfig,
    SandboxWorkspace,
    _RunResult,
    _SubprocessRunner,
)
from core.state_db import StateDB
from core.telegram_bridge import (
    BridgeReply,
    IncomingMessage,
    OutgoingEnvelope,
)
from core.tier_session import TierSessionStore

# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


class FakeGitRunner(_SubprocessRunner):
    """Always succeeds; records every git invocation for assertion."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

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
    cfg = SandboxConfig(
        main_repo_path=fake_repo,
        worktree_root=tmp_path / "worktrees",
    )
    return SandboxWorkspace(cfg, runner=FakeGitRunner())


@pytest.fixture
def tier_store() -> TierSessionStore:
    return TierSessionStore(default_tier_registry())


@pytest.fixture
def runner():
    r = BackgroundTaskRunner()
    yield r
    r.shutdown()


def _msg(chat_id: int = 100, text: str = "build me a thing") -> IncomingMessage:
    return IncomingMessage(
        chat_id=chat_id,
        user_id=chat_id,
        message_id=1,
        text=text,
    )


def _project(**overrides) -> Project:
    data = {
        "project_id": "alpha_project",
        "slug": "alpha-project",
        "name": "Alpha Project",
        "description": "Primary project.",
        "owner_user_id": 101,
        "status": "active",
    }
    data.update(overrides)
    return Project(**data)


def _policy(**overrides) -> ProjectPolicy:
    data = {
        "project_id": "alpha_project",
        "allow_hiring": True,
        "allow_agent_dm": False,
        "require_owner_approval_for_hires": True,
    }
    data.update(overrides)
    return ProjectPolicy(**data)


def _chat_binding(**overrides) -> ProjectChatBinding:
    data = {
        "project_id": "alpha_project",
        "chat_provider": "telegram",
        "chat_id": -100123,
    }
    data.update(overrides)
    return ProjectChatBinding(**data)


def _runtime_binding(repo_path: Path, **overrides) -> ProjectRuntimeBinding:
    data = {
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
    **overrides,
) -> ProjectSnapshot:
    data = {
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
    db_name: str = "runtime-state.db",
) -> ProjectRuntimeRouter:
    db = StateDB(tmp_path / db_name)
    registry = ProjectRegistry(db)
    registry.register_project(snapshot)
    return ProjectRuntimeRouter(registry, None)


def _capturing_happy_agents(captured_prompts: list[str]):
    def _planning_agent(task_prompt: str) -> str:
        captured_prompts.append(task_prompt)
        return '{"plan": "ok"}'

    agents = happy_agents(None)
    agents["planning_agent"] = _planning_agent
    return lambda _tier: agents


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


def _make_dispatcher() -> LLMDispatcher:
    return LLMDispatcher(api_key="sk-test-key-1234")


def _make_dispatch_response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        model_used="model-x",
        prompt_tokens=11,
        completion_tokens=7,
        attempts=(
            LLMAttempt(model="model-x", ok=True, reason="ok", duration_ms=1),
        ),
    )


def _wait_until_idle(runner: BackgroundTaskRunner, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while runner.is_busy() and time.time() < deadline:
        time.sleep(0.02)


def _wait_for_count(captured, predicate, timeout: float = 5.0) -> None:
    """Wait until predicate(captured) is True."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate(captured):
            return
        time.sleep(0.02)


# Predictable agent registries -------------------------------------------------


def happy_agents(_tier):
    return {
        "planning_agent": lambda *_a: '{"plan": "ok"}',
        "pm_agent": lambda *_a: '{"tasks": [], "specialization_hints": []}',
        "architect_agent": lambda *_a: '{"arch": "spec"}',
        "writer_agent": lambda *_a: "def f(): return 42",
        "reviewer_agent": lambda *_a: '{"verdict": "APPROVED"}',
        "tester_agent": lambda *_a: "tests pass",
        "qa_agent": lambda *_a: '{"verdict": "PASS"}',
        "fixer_agent": lambda *_a: "def f(): return 42",
    }


def noop_agents(_tier):
    """All agents return empty strings — orchestrator FAILs in PLANNING."""
    return {role: (lambda *_a: "") for role in (
        "planning_agent", "pm_agent", "architect_agent", "writer_agent",
        "reviewer_agent", "tester_agent", "qa_agent", "fixer_agent",
    )}


def exploding_agents(_tier):
    """planning_agent raises; orchestrator should terminate with agent_exception."""
    def boom(*_a):
        raise RuntimeError("planning kaboom")
    return {
        "planning_agent": boom,
        "pm_agent": lambda *_a: '{}',
        "architect_agent": lambda *_a: '{}',
        "writer_agent": lambda *_a: "x",
        "reviewer_agent": lambda *_a: '{"verdict":"APPROVED"}',
        "tester_agent": lambda *_a: "ok",
        "qa_agent": lambda *_a: '{"verdict":"PASS"}',
        "fixer_agent": lambda *_a: "x",
    }


# ---------------------------------------------------------------------------
# RealTaskHandlerConfig validation
# ---------------------------------------------------------------------------


def test_config_defaults_are_valid():
    c = RealTaskHandlerConfig()
    assert c.cost_budget_usd > 0
    assert c.max_task_chars > 0
    assert "@" in c.author_email


def test_config_is_frozen():
    c = RealTaskHandlerConfig()
    with pytest.raises(Exception):
        c.cost_budget_usd = 99  # type: ignore[misc]


@pytest.mark.parametrize("bad", [0, -1, True, "5"])
def test_config_rejects_invalid_cost_budget(bad):
    with pytest.raises(ValueError, match="invalid_cost_budget"):
        RealTaskHandlerConfig(cost_budget_usd=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "10"])
def test_config_rejects_invalid_max_task_chars(bad):
    with pytest.raises(ValueError, match="invalid_max_task_chars"):
        RealTaskHandlerConfig(max_task_chars=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["", "   "])
def test_config_rejects_empty_author_name(bad):
    with pytest.raises(ValueError, match="empty_author_name"):
        RealTaskHandlerConfig(author_name=bad)


@pytest.mark.parametrize("bad", ["no-at-sign", "", "  "])
def test_config_rejects_invalid_author_email(bad):
    with pytest.raises(ValueError, match="invalid_author_email"):
        RealTaskHandlerConfig(author_email=bad)


# ---------------------------------------------------------------------------
# generate_task_id
# ---------------------------------------------------------------------------


def test_generate_task_id_default():
    tid = generate_task_id()
    assert tid.startswith("task-")
    # Must be acceptable to SandboxWorkspace
    import re
    assert re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", tid)


def test_generate_task_id_with_custom_prefix():
    tid = generate_task_id(prefix="abc")
    assert tid.startswith("abc-")


def test_generate_task_id_with_custom_clock():
    tid = generate_task_id(clock=lambda: 1234567890.0)
    assert tid.startswith("task-1234567890-")


def test_generate_task_id_unique():
    ids = {generate_task_id() for _ in range(50)}
    assert len(ids) == 50


@pytest.mark.parametrize("bad", ["", "   ", "BadCase", "with space", "x" * 70])
def test_generate_task_id_rejects_bad_prefix(bad):
    with pytest.raises(ValueError):
        generate_task_id(prefix=bad)


@pytest.mark.parametrize("bad", [0, -1, True])
def test_generate_task_id_rejects_invalid_clock_value(bad):
    with pytest.raises(ValueError, match="invalid_clock_value"):
        generate_task_id(clock=lambda _bad=bad: _bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _format_event
# ---------------------------------------------------------------------------


def test_format_task_started():
    e = ProgressEvent(kind="task_started", timestamp=1.0, detail="hello")
    text = _format_event(e)
    assert "Старт" in text
    assert "hello" in text


def test_format_agent_started():
    e = ProgressEvent(kind="agent_started", timestamp=1.0, agent_role="architect_agent")
    assert "architect_agent" in _format_event(e)
    assert "начал" in _format_event(e)


def test_format_agent_finished_includes_duration():
    e = ProgressEvent(
        kind="agent_finished",
        timestamp=1.0,
        agent_role="writer_agent",
        duration_ms=1234,
    )
    assert "1234" in _format_event(e)


def test_format_agent_failed_truncates_long_detail():
    e = ProgressEvent(
        kind="agent_failed",
        timestamp=1.0,
        agent_role="writer_agent",
        detail="X" * 500,
    )
    out = _format_event(e)
    # We don't allow a single line to grow unboundedly.
    assert len(out) <= 200


def test_format_task_completed():
    e = ProgressEvent(kind="task_completed", timestamp=1.0, detail="branch=feature/x")
    assert "Готово" in _format_event(e)


def test_format_task_failed_with_no_detail():
    e = ProgressEvent(kind="task_failed", timestamp=1.0)
    assert "Провалена" in _format_event(e)


# ---------------------------------------------------------------------------
# make_real_task_handler — validation
# ---------------------------------------------------------------------------


def test_make_handler_rejects_non_runner(sandbox, tier_store):
    send, _ = _make_progress_capture()
    with pytest.raises(ValueError, match="invalid_runner"):
        make_real_task_handler(
            runner="not a runner",  # type: ignore[arg-type]
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send,
        )


def test_make_handler_rejects_non_sandbox(runner, tier_store):
    send, _ = _make_progress_capture()
    with pytest.raises(ValueError, match="invalid_sandbox"):
        make_real_task_handler(
            runner=runner,
            sandbox="not a sandbox",  # type: ignore[arg-type]
            tier_store=tier_store,
            send_progress=send,
        )


def test_make_handler_rejects_non_store(runner, sandbox):
    send, _ = _make_progress_capture()
    with pytest.raises(ValueError, match="invalid_tier_store"):
        make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store="not a store",  # type: ignore[arg-type]
            send_progress=send,
        )


def test_make_handler_rejects_non_callable_send(runner, sandbox, tier_store):
    with pytest.raises(ValueError, match="send_progress_not_callable"):
        make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress="not callable",  # type: ignore[arg-type]
        )


def test_make_handler_rejects_non_callable_send_progress_envelope(
    runner,
    sandbox,
    tier_store,
):
    send, _ = _make_progress_capture()
    with pytest.raises(ValueError, match="send_progress_envelope_not_callable"):
        make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send,
            send_progress_envelope="not callable",  # type: ignore[arg-type]
        )


def test_make_handler_rejects_non_callable_factory(runner, sandbox, tier_store):
    send, _ = _make_progress_capture()
    with pytest.raises(ValueError, match="agent_registry_factory_not_callable"):
        make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory="not callable",  # type: ignore[arg-type]
        )


def test_make_handler_rejects_non_config(runner, sandbox, tier_store):
    send, _ = _make_progress_capture()
    with pytest.raises(ValueError, match="invalid_config"):
        make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send,
            config="not a config",  # type: ignore[arg-type]
        )


def test_make_handler_rejects_non_observability(runner, sandbox, tier_store):
    send, _ = _make_progress_capture()
    with pytest.raises(ValueError, match="invalid_observability"):
        make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send,
            observability="not obs",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# _handle: short-circuit cases (synchronous, no submit)
# ---------------------------------------------------------------------------


def test_handle_no_tier_returns_pick_tier_reply(runner, sandbox, tier_store):
    send, _ = _make_progress_capture()
    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=send,
    )
    reply = handler("do something", _msg(chat_id=42))
    assert isinstance(reply, BridgeReply)
    assert "тариф" in reply.body.lower()
    assert "/tier" in reply.body
    # No work was submitted.
    assert runner.is_busy() is False


def test_handle_invalid_msg_returns_internal_error(runner, sandbox, tier_store):
    send, _ = _make_progress_capture()
    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=send,
    )
    reply = handler("hi", "not a message")  # type: ignore[arg-type]
    assert isinstance(reply, BridgeReply)
    assert "ошибка моста" in reply.body.lower()


def test_handle_busy_returns_wait_reply(runner, sandbox, tier_store):
    """If runner is already running another task, /tier-eligible chats get told."""
    tier_store.set_active(42, "STANDARD")
    send, _captured = _make_progress_capture()

    block = threading.Event()

    def slow_run(_token):
        block.wait(timeout=5)
        return "done"

    runner.submit(
        raw_task="first",
        run_fn=slow_run,
        on_complete=lambda *_: None,
    )
    try:
        handler = make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send,
        )
        reply = handler("second task", _msg(chat_id=42))
        assert isinstance(reply, BridgeReply)
        assert "уже работаю" in reply.body.lower()
        assert "first" in reply.body
    finally:
        block.set()
        _wait_until_idle(runner)


def test_handle_invalid_task_id_factory(runner, sandbox, tier_store):
    """A bogus task_id_factory must not crash the handler."""
    tier_store.set_active(42, "STANDARD")
    send, _ = _make_progress_capture()
    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=send,
        task_id_factory=lambda: "BadCASE!",
    )
    reply = handler("hi", _msg(chat_id=42))
    assert isinstance(reply, BridgeReply)
    assert "невалидный task_id" in reply.body.lower()


def test_handle_factory_raises(runner, sandbox, tier_store):
    tier_store.set_active(42, "STANDARD")
    send, _ = _make_progress_capture()

    def bad_factory():
        raise RuntimeError("kaboom")

    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=send,
        task_id_factory=bad_factory,
    )
    reply = handler("hi", _msg(chat_id=42))
    assert isinstance(reply, BridgeReply)
    assert "kaboom" in reply.body


def test_handle_returns_ack_when_submitted(runner, sandbox, tier_store):
    tier_store.set_active(42, "STANDARD")
    send, _captured = _make_progress_capture()

    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=send,
        agent_registry_factory=happy_agents,
    )
    reply = handler("write me a function", _msg(chat_id=42))
    assert isinstance(reply, BridgeReply)
    assert "принял" in reply.body.lower()
    assert "task-id" in reply.body.lower()
    assert "STANDARD" in reply.body
    _wait_until_idle(runner)


# ---------------------------------------------------------------------------
# Project-aware coordinator onboarding
# ---------------------------------------------------------------------------


def test_bound_project_chat_pipeline_gets_enriched_onboarding_prompt(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    from unittest.mock import MagicMock, patch

    tier_store.set_active(-100123, "STANDARD")
    send, _captured = _make_progress_capture()
    snapshot = _project_snapshot(
        fake_repo,
        chat_binding=_chat_binding(chat_id=-100123),
    )
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="bound-chat.db",
    )
    captured_prompts: list[str] = []
    owner_task_text = "Build the deployment checker."
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    with (
        patch("core.project_runtime_router._build_sandbox", return_value=sandbox),
        patch("core.real_task_handler.make_sandbox_hook", return_value=mock_hook_fn),
        patch.object(sandbox, "commit_in_worktree", return_value="abc123def456789"),
    ):
        handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=_capturing_happy_agents(captured_prompts),
            task_id_factory=lambda: "task-bound-onboarding-001",
        )
        reply = handler(
            owner_task_text,
            IncomingMessage(
                chat_id=-100123,
                user_id=777,
                message_id=1,
                text=owner_task_text,
                project_id="alpha_project",
                project_slug="alpha-project",
                project_context_source="bound_chat",
            ),
        )
        assert isinstance(reply, BridgeReply)
        assert owner_task_text in reply.body
        assert "Coordinator project captain onboarding" not in reply.body
        _wait_until_idle(runner)

    assert captured_prompts, "planning_agent must receive a pipeline task prompt"
    prompt = captured_prompts[0]
    assert prompt != owner_task_text
    assert "Coordinator role: project captain" in prompt
    assert "project_id: alpha_project" in prompt
    assert "slug: alpha-project" in prompt
    assert "source: explicit project chat" in prompt
    assert str(fake_repo.resolve()) in prompt
    assert owner_task_text in prompt


def test_owner_dm_single_project_pipeline_gets_enriched_onboarding_prompt(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    from unittest.mock import MagicMock, patch

    tier_store.set_active(101, "STANDARD")
    send, _captured = _make_progress_capture()
    snapshot = _project_snapshot(fake_repo)
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="owner-dm.db",
    )
    captured_prompts: list[str] = []
    owner_task_text = "Prepare the release branch."
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    with (
        patch("core.project_runtime_router._build_sandbox", return_value=sandbox),
        patch("core.real_task_handler.make_sandbox_hook", return_value=mock_hook_fn),
        patch.object(sandbox, "commit_in_worktree", return_value="abc123def456789"),
    ):
        handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=_capturing_happy_agents(captured_prompts),
            task_id_factory=lambda: "task-owner-dm-onboarding-001",
        )
        reply = handler(
            owner_task_text,
            IncomingMessage(
                chat_id=101,
                user_id=101,
                message_id=1,
                text=owner_task_text,
                project_id="alpha_project",
                project_slug="alpha-project",
                project_context_source="owner_dm_single_project",
            ),
        )
        assert isinstance(reply, BridgeReply)
        assert owner_task_text in reply.body
        assert "Coordinator project captain onboarding" not in reply.body
        _wait_until_idle(runner)

    assert captured_prompts, "planning_agent must receive a pipeline task prompt"
    prompt = captured_prompts[0]
    assert prompt != owner_task_text
    assert "Coordinator role: project captain" in prompt
    assert "source: owner DM fallback" in prompt
    assert owner_task_text in prompt


def test_bound_project_chat_seeds_coordinator_artifacts_before_planning(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    from unittest.mock import MagicMock, patch

    tier_store.set_active(-100123, "STANDARD")
    send, _captured = _make_progress_capture()
    snapshot = _project_snapshot(
        fake_repo,
        chat_binding=_chat_binding(chat_id=-100123),
    )
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="bound-brief.db",
    )
    task_id = "task-bound-brief-001"
    memory = PipelineMemory()
    seen: dict[str, str | None] = {}
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    def _planning_agent(task_prompt: str) -> str:
        seen["brief"] = memory.get_artifact(task_id, "project_brief")
        seen["proposal"] = memory.get_artifact(task_id, "team_proposal")
        seen["escalation"] = memory.get_artifact(task_id, "owner_escalation")
        seen["prompt"] = task_prompt
        return '{"plan": "ok"}'

    def _agents(_tier):
        agents = happy_agents(None)
        agents["planning_agent"] = _planning_agent
        return agents

    with (
        patch("core.project_runtime_router._build_sandbox", return_value=sandbox),
        patch("core.real_task_handler.make_sandbox_hook", return_value=mock_hook_fn),
        patch.object(sandbox, "commit_in_worktree", return_value="abc123def456789"),
    ):
        handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=_agents,
            memory_factory=lambda: memory,
            task_id_factory=lambda: task_id,
        )
        handler(
            "Build the deployment checker.",
            IncomingMessage(
                chat_id=-100123,
                user_id=777,
                message_id=1,
                text="Build the deployment checker.",
                project_id="alpha_project",
                project_slug="alpha-project",
                project_context_source="bound_chat",
            ),
        )
        _wait_until_idle(runner)

    assert seen["brief"] is not None
    assert "Coordinator project brief" in seen["brief"]
    assert "explicit project chat" in seen["brief"]
    assert "Build the deployment checker." in seen["brief"]
    assert memory.get_artifact(task_id, "project_brief") == seen["brief"]
    assert seen["proposal"] is not None
    assert "Coordinator team proposal" in seen["proposal"]
    assert "assembly_mode: baseline_internal_team" in seen["proposal"]
    assert "coordinator_agent" in seen["proposal"]
    assert "project captain" in seen["proposal"].lower()
    assert "Project specialists:" in seen["proposal"]
    assert "- none" in seen["proposal"]
    assert "Specialization hints:" in seen["proposal"]
    assert memory.get_artifact(task_id, "team_proposal") == seen["proposal"]


def test_owner_dm_single_project_seeds_coordinator_artifacts_before_planning(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    from unittest.mock import MagicMock, patch

    tier_store.set_active(101, "STANDARD")
    send, _captured = _make_progress_capture()
    snapshot = _project_snapshot(fake_repo)
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="owner-dm-brief.db",
    )
    task_id = "task-owner-dm-brief-001"
    memory = PipelineMemory()
    seen: dict[str, str | None] = {}
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    def _planning_agent(task_prompt: str) -> str:
        seen["brief"] = memory.get_artifact(task_id, "project_brief")
        seen["proposal"] = memory.get_artifact(task_id, "team_proposal")
        seen["escalation"] = memory.get_artifact(task_id, "owner_escalation")
        seen["prompt"] = task_prompt
        return '{"plan": "ok"}'

    def _agents(_tier):
        agents = happy_agents(None)
        agents["planning_agent"] = _planning_agent
        return agents

    with (
        patch("core.project_runtime_router._build_sandbox", return_value=sandbox),
        patch("core.real_task_handler.make_sandbox_hook", return_value=mock_hook_fn),
        patch.object(sandbox, "commit_in_worktree", return_value="abc123def456789"),
    ):
        handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=_agents,
            memory_factory=lambda: memory,
            task_id_factory=lambda: task_id,
        )
        handler(
            "Prepare the release branch.",
            IncomingMessage(
                chat_id=101,
                user_id=101,
                message_id=1,
                text="Prepare the release branch.",
                project_id="alpha_project",
                project_slug="alpha-project",
                project_context_source="owner_dm_single_project",
            ),
        )
        _wait_until_idle(runner)

    assert seen["brief"] is not None
    assert "Coordinator project brief" in seen["brief"]
    assert "owner DM fallback" in seen["brief"]
    assert "Prepare the release branch." in seen["brief"]
    assert seen["proposal"] is not None
    assert "Coordinator team proposal" in seen["proposal"]
    assert "assembly_mode: baseline_internal_team" in seen["proposal"]
    assert "owner DM fallback" in seen["proposal"]
    assert "Prepare the release branch." in seen["proposal"]
    assert "Project specialists:" in seen["proposal"]
    assert "- none" in seen["proposal"]
    assert "Specialization hints:" in seen["proposal"]


def test_project_aware_path_injects_current_project_specialists_into_proposal_surface(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    from unittest.mock import MagicMock, patch

    tier_store.set_active(-100123, "STANDARD")
    send, _captured = _make_progress_capture()
    snapshot = _project_snapshot(
        fake_repo,
        chat_binding=_chat_binding(chat_id=-100123),
    )
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="bound-roster-brief.db",
    )
    runtime_router.registry.add_project_specialist(
        "alpha_project",
        "security_agent",
    )
    runtime_router.registry.add_project_specialist(
        "alpha_project",
        "data_agent",
    )
    task_id = "task-bound-roster-brief-001"
    memory = PipelineMemory()
    seen: dict[str, str | None] = {}
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    def _planning_agent(task_prompt: str) -> str:
        seen["proposal"] = memory.get_artifact(task_id, "team_proposal")
        seen["prompt"] = task_prompt
        return '{"plan": "ok"}'

    def _agents(_tier):
        agents = happy_agents(None)
        agents["planning_agent"] = _planning_agent
        return agents

    with (
        patch("core.project_runtime_router._build_sandbox", return_value=sandbox),
        patch("core.real_task_handler.make_sandbox_hook", return_value=mock_hook_fn),
        patch.object(sandbox, "commit_in_worktree", return_value="abc123def456789"),
    ):
        handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=_agents,
            memory_factory=lambda: memory,
            task_id_factory=lambda: task_id,
        )
        handler(
            "Build the deployment checker.",
            IncomingMessage(
                chat_id=-100123,
                user_id=777,
                message_id=1,
                text="Build the deployment checker.",
                project_id="alpha_project",
                project_slug="alpha-project",
                project_context_source="bound_chat",
            ),
        )
        _wait_until_idle(runner)

    assert seen["proposal"] is not None
    assert "Project specialists:" in seen["proposal"]
    assert "- role_id: security_agent" in seen["proposal"]
    assert "- role_id: data_agent" in seen["proposal"]


def test_legacy_non_context_path_does_not_seed_fake_coordinator_artifacts(
    runner,
    sandbox,
    tier_store,
):
    from unittest.mock import MagicMock, patch

    tier_store.set_active(42, "STANDARD")
    send, _captured = _make_progress_capture()
    task_id = "task-no-brief-001"
    memory = PipelineMemory()
    seen: dict[str, str | None] = {}
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    def _planning_agent(task_prompt: str) -> str:
        seen["brief"] = memory.get_artifact(task_id, "project_brief")
        seen["proposal"] = memory.get_artifact(task_id, "team_proposal")
        seen["escalation"] = memory.get_artifact(task_id, "owner_escalation")
        seen["prompt"] = task_prompt
        return '{"plan": "ok"}'

    def _agents(_tier):
        agents = happy_agents(None)
        agents["planning_agent"] = _planning_agent
        return agents

    with (
        patch("core.real_task_handler.make_sandbox_hook", return_value=mock_hook_fn),
        patch.object(sandbox, "commit_in_worktree", return_value="abc123def456789"),
    ):
        handler = make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=_agents,
            memory_factory=lambda: memory,
            task_id_factory=lambda: task_id,
        )
        handler("Legacy build task.", _msg(chat_id=42, text="Legacy build task."))
        _wait_until_idle(runner)

    assert seen["brief"] is None
    assert seen["proposal"] is None
    assert seen["escalation"] is None
    assert memory.get_artifact(task_id, "project_brief") is None
    assert memory.get_artifact(task_id, "team_proposal") is None
    assert memory.get_artifact(task_id, "owner_escalation") is None
    assert seen["prompt"] == "Legacy build task."


def test_project_aware_fail_path_seeds_owner_escalation_and_uses_reply(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    from unittest.mock import patch

    tier_store.set_active(-100123, "STANDARD")
    send, captured = _make_progress_capture()
    snapshot = _project_snapshot(
        fake_repo,
        chat_binding=_chat_binding(chat_id=-100123),
    )
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="fail-escalation.db",
    )
    task_id = "task-fail-escalation-001"
    memory = PipelineMemory()

    def _reviewer_boom(*_args):
        raise RuntimeError("kaboom")

    def _agents(_tier):
        agents = happy_agents(None)
        agents["reviewer_agent"] = _reviewer_boom
        return agents

    with patch("core.project_runtime_router._build_sandbox", return_value=sandbox):
        handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=_agents,
            memory_factory=lambda: memory,
            task_id_factory=lambda: task_id,
        )
        handler(
            "Implement the release workflow.",
            IncomingMessage(
                chat_id=-100123,
                user_id=777,
                message_id=1,
                text="Implement the release workflow.",
                project_id="alpha_project",
                project_slug="alpha-project",
                project_context_source="bound_chat",
            ),
        )
        _wait_until_idle(runner)
        _wait_for_count(captured, lambda c: any("Не получилось" in t for _, t in c))

    escalation = memory.get_artifact(task_id, "owner_escalation")
    assert escalation is not None
    assert "escalation_type: system_failure" in escalation
    assert "agent_exception:RuntimeError:kaboom" in escalation
    assert "Alpha Project" in escalation
    failure_messages = [text for _, text in captured if "Не получилось" in text]
    assert failure_messages
    assert any("внутренним pipeline/system сбоем" in text for text in failure_messages)
    assert any("agent_exception:RuntimeError:kaboom" in text for text in failure_messages)


def test_project_aware_review_fix_failure_surfaces_actionable_diagnostics(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    from unittest.mock import patch

    from core.task_history import TaskHistory, split_failure_reason_detail

    tier_store.set_active(-100123, "ECONOMY")
    send, captured = _make_progress_capture()
    snapshot = _project_snapshot(
        fake_repo,
        chat_binding=_chat_binding(chat_id=-100123),
    )
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="review-fix-diagnostics.db",
    )
    task_id = "task-review-fix-diagnostics-001"
    history = TaskHistory()
    review_rejected = (
        '{"review_id":"r2","verdict":"REJECTED","files":[{"path":"src/example.py",'
        '"verdict":"REJECTED","issues":[{"severity":"major","issue":"missing square"}]}],'
        '"summary":{"total_issues":1,"critical":0,"major":1,"minor":0,'
        '"files_approved":0,"files_rejected":1},"for_fixer":[{"path":"src/example.py",'
        '"severity":"major","instruction":"restore square implementation"}]}'
    )

    def _agents(_tier):
        agents = happy_agents(None)
        agents["reviewer_agent"] = lambda *_args: review_rejected
        agents["fixer_agent"] = lambda *_args: "def f(): return 42"
        return agents

    with patch("core.project_runtime_router._build_sandbox", return_value=sandbox):
        handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=_agents,
            task_history=history,
            task_id_factory=lambda: task_id,
        )
        handler(
            "Implement the release workflow.",
            IncomingMessage(
                chat_id=-100123,
                user_id=777,
                message_id=1,
                text="Implement the release workflow.",
                project_id="alpha_project",
                project_slug="alpha-project",
                project_context_source="bound_chat",
            ),
        )
        _wait_until_idle(runner)
        _wait_for_count(captured, lambda c: any("Не получилось" in t for _, t in c))

    persisted = history.get(task_id)
    assert persisted is not None
    reason_code, detail = split_failure_reason_detail(persisted.failure_reason)
    assert reason_code == "review_fix_loop_exceeded"
    assert detail is not None
    assert "review=REJECTED" in detail
    assert "summary c=0 m=1 n=0" in detail
    assert "src/example.py" in detail
    assert "restore square implementation" in detail

    failure_messages = [text for _, text in captured if "Не получилось" in text]
    assert failure_messages
    assert any("reason  `review_fix_loop_exceeded`" in text for text in failure_messages)
    assert any("detail  review=REJECTED" in text for text in failure_messages)
    assert any("restore square implementation" in text for text in failure_messages)


def test_project_aware_blocked_path_seeds_owner_escalation(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    from unittest.mock import patch

    tier_store.set_active(-100123, "STANDARD")
    send, captured = _make_progress_capture()
    snapshot = _project_snapshot(
        fake_repo,
        chat_binding=_chat_binding(chat_id=-100123),
    )
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="blocked-escalation.db",
    )
    task_id = "task-blocked-escalation-001"
    memory = PipelineMemory()

    def _writer_blocked(*_args):
        return "BLOCKED: missing architecture"

    def _agents(_tier):
        agents = happy_agents(None)
        agents["writer_agent"] = _writer_blocked
        return agents

    with patch("core.project_runtime_router._build_sandbox", return_value=sandbox):
        handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=_agents,
            memory_factory=lambda: memory,
            task_id_factory=lambda: task_id,
        )
        handler(
            "Implement the release workflow.",
            IncomingMessage(
                chat_id=-100123,
                user_id=777,
                message_id=1,
                text="Implement the release workflow.",
                project_id="alpha_project",
                project_slug="alpha-project",
                project_context_source="bound_chat",
            ),
        )
        _wait_until_idle(runner)
        _wait_for_count(captured, lambda c: any("Не получилось" in t for _, t in c))

    escalation = memory.get_artifact(task_id, "owner_escalation")
    assert escalation is not None
    assert "escalation_type: project_blocked" in escalation
    assert "final_state: BLOCKED" in escalation
    assert "writer_blocked:BLOCKED: missing architecture" in escalation
    failure_messages = [text for _, text in captured if "Не получилось" in text]
    assert failure_messages
    assert any("заблокирована" in text for text in failure_messages)


def test_project_aware_publish_failure_seeds_owner_escalation(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    from unittest.mock import MagicMock, patch

    from core.sandbox_workspace import SandboxError

    tier_store.set_active(-100123, "PREMIUM")
    send, captured = _make_progress_capture()
    snapshot = _project_snapshot(
        fake_repo,
        chat_binding=_chat_binding(chat_id=-100123),
    )
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="publish-escalation.db",
    )
    task_id = "task-publish-escalation-001"
    memory = PipelineMemory()
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    with (
        patch("core.project_runtime_router._build_sandbox", return_value=sandbox),
        patch("core.real_task_handler.make_sandbox_hook", return_value=mock_hook_fn),
        patch.object(
            sandbox,
            "commit_in_worktree",
            side_effect=SandboxError("nothing_to_commit"),
        ),
    ):
        handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=happy_agents,
            memory_factory=lambda: memory,
            task_id_factory=lambda: task_id,
        )
        handler(
            "Prepare the release branch.",
            IncomingMessage(
                chat_id=-100123,
                user_id=777,
                message_id=1,
                text="Prepare the release branch.",
                project_id="alpha_project",
                project_slug="alpha-project",
                project_context_source="bound_chat",
            ),
        )
        _wait_until_idle(runner)
        _wait_for_count(captured, lambda c: any("Не получилось" in t for _, t in c))

    escalation = memory.get_artifact(task_id, "owner_escalation")
    assert escalation is not None
    assert "escalation_type: publish_failure" in escalation
    assert "commit_failed:SandboxError:nothing_to_commit" in escalation
    failure_messages = [text for _, text in captured if "Не получилось" in text]
    assert failure_messages
    assert any("publish step" in text for text in failure_messages)


def test_project_aware_success_path_does_not_seed_owner_escalation(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    from unittest.mock import MagicMock, patch

    tier_store.set_active(-100123, "PREMIUM")
    send, _captured = _make_progress_capture()
    snapshot = _project_snapshot(
        fake_repo,
        chat_binding=_chat_binding(chat_id=-100123),
    )
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="success-no-escalation.db",
    )
    task_id = "task-success-no-escalation-001"
    memory = PipelineMemory()
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    with (
        patch("core.project_runtime_router._build_sandbox", return_value=sandbox),
        patch("core.real_task_handler.make_sandbox_hook", return_value=mock_hook_fn),
        patch.object(sandbox, "commit_in_worktree", return_value="deadbeef12345678"),
    ):
        handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=happy_agents,
            memory_factory=lambda: memory,
            task_id_factory=lambda: task_id,
        )
        handler(
            "Ship the notification command.",
            IncomingMessage(
                chat_id=-100123,
                user_id=777,
                message_id=1,
                text="Ship the notification command.",
                project_id="alpha_project",
                project_slug="alpha-project",
                project_context_source="bound_chat",
            ),
        )
        _wait_until_idle(runner)

    assert memory.get_artifact(task_id, "owner_escalation") is None


def test_project_aware_cancelled_path_does_not_seed_owner_escalation(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    import threading
    from unittest.mock import patch

    tier_store.set_active(-100123, "STANDARD")
    send, captured = _make_progress_capture()
    snapshot = _project_snapshot(
        fake_repo,
        chat_binding=_chat_binding(chat_id=-100123),
    )
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="cancelled-no-escalation.db",
    )
    task_id = "task-cancelled-no-escalation-001"
    memory = PipelineMemory()
    agent_in_flight = threading.Event()
    release_agent = threading.Event()

    def _blocking_planning(*_args):
        agent_in_flight.set()
        release_agent.wait(timeout=5.0)
        return '{"plan": "ok"}'

    def _agents(_tier):
        agents = happy_agents(_tier)
        agents["planning_agent"] = _blocking_planning
        return agents

    with patch("core.project_runtime_router._build_sandbox", return_value=sandbox):
        handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=_agents,
            memory_factory=lambda: memory,
            task_id_factory=lambda: task_id,
        )
        handler(
            "Build the deployment checker.",
            IncomingMessage(
                chat_id=-100123,
                user_id=777,
                message_id=1,
                text="Build the deployment checker.",
                project_id="alpha_project",
                project_slug="alpha-project",
                project_context_source="bound_chat",
            ),
        )
        assert agent_in_flight.wait(timeout=5.0)
        runner.cancel()
        release_agent.set()
        _wait_until_idle(runner)
        _wait_for_count(captured, lambda c: any("Отменено" in t for _, t in c))

    assert memory.get_artifact(task_id, "owner_escalation") is None


def test_bound_project_chat_progress_posts_use_agent_and_coordinator_roles(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    from unittest.mock import MagicMock, patch

    tier_store.set_active(-100123, "STANDARD")
    send_envelope, captured = _make_progress_envelope_capture()
    snapshot = _project_snapshot(
        fake_repo,
        chat_binding=_chat_binding(chat_id=-100123),
        policy=_policy(require_owner_approval_for_hires=False),
    )
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="posting-bound-success.db",
    )
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    with (
        patch("core.project_runtime_router._build_sandbox", return_value=sandbox),
        patch("core.real_task_handler.make_sandbox_hook", return_value=mock_hook_fn),
        patch.object(sandbox, "commit_in_worktree", return_value="feedface12345678"),
    ):
        handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress_envelope=send_envelope,
            agent_registry_factory=happy_agents,
            task_id_factory=lambda: "task-posting-bound-success-001",
        )
        handler(
            "Ship the notification command.",
            IncomingMessage(
                chat_id=-100123,
                user_id=777,
                message_id=1,
                text="Ship the notification command.",
                project_id="alpha_project",
                project_slug="alpha-project",
                project_context_source="bound_chat",
            ),
        )
        _wait_until_idle(runner)

    assert any(
        env.sender_role == COORDINATOR_ROLE
        and env.message.text.startswith("🚀 Старт")
        and env.delivery_role is None
        for env in captured
    )
    assert any(
        env.sender_role == "architect_agent"
        and "architect_agent начал" in env.message.text
        and env.delivery_role is None
        for env in captured
    )
    assert any(
        env.sender_role == "writer_agent"
        and "writer_agent закончил" in env.message.text
        and env.delivery_role is None
        for env in captured
    )
    assert any(
        env.sender_role == COORDINATOR_ROLE
        and "🌳 worktree готов" in env.message.text
        for env in captured
    )
    assert any(
        env.sender_role == COORDINATOR_ROLE
        and "✅ Готово" in env.message.text
        for env in captured
    )


def test_bound_project_chat_agent_failed_post_uses_agent_role(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    from unittest.mock import patch

    tier_store.set_active(-100123, "STANDARD")
    send_envelope, captured = _make_progress_envelope_capture()
    snapshot = _project_snapshot(
        fake_repo,
        chat_binding=_chat_binding(chat_id=-100123),
        policy=_policy(require_owner_approval_for_hires=False),
    )
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="posting-bound-fail.db",
    )

    with patch("core.project_runtime_router._build_sandbox", return_value=sandbox):
        handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress_envelope=send_envelope,
            agent_registry_factory=exploding_agents,
            task_id_factory=lambda: "task-posting-bound-fail-001",
        )
        handler(
            "Break planning.",
            IncomingMessage(
                chat_id=-100123,
                user_id=777,
                message_id=1,
                text="Break planning.",
                project_id="alpha_project",
                project_slug="alpha-project",
                project_context_source="bound_chat",
            ),
        )
        _wait_until_idle(runner)
        _wait_for_count(captured, lambda c: any("❌ Не получилось" in env.message.text for env in c))

    assert any(
        env.sender_role == "planning_agent"
        and "planning_agent упал" in env.message.text
        and env.delivery_role is None
        for env in captured
    )
    assert any(
        env.sender_role == COORDINATOR_ROLE
        and "❌ Не получилось" in env.message.text
        and env.delivery_role is None
        for env in captured
    )


def test_owner_dm_fallback_progress_posts_keep_semantic_roles_but_route_back_to_same_bot(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    from unittest.mock import MagicMock, patch

    tier_store.set_active(101, "STANDARD")
    send_envelope, captured = _make_progress_envelope_capture()
    snapshot = _project_snapshot(fake_repo)
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="posting-owner-dm.db",
    )
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    with (
        patch("core.project_runtime_router._build_sandbox", return_value=sandbox),
        patch("core.real_task_handler.make_sandbox_hook", return_value=mock_hook_fn),
        patch.object(sandbox, "commit_in_worktree", return_value="0123456789abcdef"),
    ):
        handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress_envelope=send_envelope,
            agent_registry_factory=happy_agents,
            task_id_factory=lambda: "task-posting-owner-dm-001",
        )
        handler(
            "Prepare the release branch.",
            IncomingMessage(
                chat_id=101,
                user_id=101,
                message_id=1,
                text="Prepare the release branch.",
                project_id="alpha_project",
                project_slug="alpha-project",
                project_context_source="owner_dm_single_project",
                incoming_bot_role="writer_agent",
            ),
        )
        _wait_until_idle(runner)

    assert any("writer_agent начал" in env.message.text for env in captured)
    assert any(
        env.sender_role == "planning_agent"
        and env.delivery_role == "writer_agent"
        and "planning_agent начал" in env.message.text
        for env in captured
    )
    assert any(
        env.sender_role == "writer_agent"
        and env.delivery_role == "writer_agent"
        and "writer_agent закончил" in env.message.text
        for env in captured
    )
    assert any(
        env.sender_role == COORDINATOR_ROLE
        and env.delivery_role == "writer_agent"
        and env.message.text.startswith("🚀 Старт")
        for env in captured
    )
    assert any(
        env.sender_role == COORDINATOR_ROLE
        and env.delivery_role == "writer_agent"
        and "🌳 worktree готов" in env.message.text
        for env in captured
    )
    assert any(
        env.sender_role == COORDINATOR_ROLE
        and env.delivery_role == "writer_agent"
        and "✅ Готово" in env.message.text
        for env in captured
    )


def test_legacy_progress_posts_remain_coordinator_owned_and_sender_failure_is_swallowed(
    runner,
    sandbox,
    tier_store,
):
    tier_store.set_active(42, "STANDARD")
    captured: list[OutgoingEnvelope] = []

    def _flaky_send(envelope: OutgoingEnvelope) -> None:
        captured.append(envelope)
        raise RuntimeError("telegram down")

    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress_envelope=_flaky_send,
        agent_registry_factory=happy_agents,
        task_id_factory=lambda: "task-posting-legacy-001",
    )
    reply = handler("build me a thing", _msg(chat_id=42))
    assert isinstance(reply, BridgeReply)
    _wait_until_idle(runner)

    assert captured
    assert all(env.sender_role == COORDINATOR_ROLE for env in captured)
    assert all(env.delivery_role is None for env in captured)


def test_busy_message_keeps_original_owner_task_text_for_project_aware_tasks(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    import threading
    from unittest.mock import patch

    tier_store.set_active(-100123, "STANDARD")
    send, _captured = _make_progress_capture()
    snapshot = _project_snapshot(
        fake_repo,
        chat_binding=_chat_binding(chat_id=-100123),
    )
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="busy-project-aware.db",
    )
    block = threading.Event()

    def slow_run(_token):
        block.wait(timeout=5)
        return "done"

    runner.submit(
        raw_task="Original owner task text",
        run_fn=slow_run,
        on_complete=lambda *_: None,
    )
    try:
        with patch(
            "core.project_runtime_router._build_sandbox",
            return_value=sandbox,
        ):
            handler = make_real_task_handler(
                runner=runner,
                runtime_router=runtime_router,
                tier_store=tier_store,
                send_progress=send,
            )
            reply = handler(
                "Second task text",
                IncomingMessage(
                    chat_id=-100123,
                    user_id=777,
                    message_id=1,
                    text="Second task text",
                    project_id="alpha_project",
                    project_slug="alpha-project",
                    project_context_source="bound_chat",
                ),
            )
        assert isinstance(reply, BridgeReply)
        assert "Original owner task text" in reply.body
        assert "Coordinator project captain onboarding" not in reply.body
    finally:
        block.set()
        _wait_until_idle(runner)


def test_commit_message_keeps_original_owner_task_text_for_project_aware_tasks(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    from unittest.mock import MagicMock, patch

    tier_store.set_active(-100123, "PREMIUM")
    send, _captured = _make_progress_capture()
    snapshot = _project_snapshot(
        fake_repo,
        chat_binding=_chat_binding(chat_id=-100123),
    )
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="project-aware-commit.db",
    )
    owner_task_text = "Ship the notification command."
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    with (
        patch("core.project_runtime_router._build_sandbox", return_value=sandbox),
        patch("core.real_task_handler.make_sandbox_hook", return_value=mock_hook_fn),
        patch.object(sandbox, "commit_in_worktree", return_value="feedface12345678") as mock_commit,
    ):
        handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=happy_agents,
            task_id_factory=lambda: "task-project-aware-commit-001",
        )
        handler(
            owner_task_text,
            IncomingMessage(
                chat_id=-100123,
                user_id=777,
                message_id=1,
                text=owner_task_text,
                project_id="alpha_project",
                project_slug="alpha-project",
                project_context_source="bound_chat",
            ),
        )
        _wait_until_idle(runner)

    commit_message = mock_commit.call_args.kwargs["message"]
    assert owner_task_text in commit_message
    assert "Coordinator project captain onboarding" not in commit_message


# ---------------------------------------------------------------------------
# Full pipeline through the worker thread (happy + sad paths)
# ---------------------------------------------------------------------------


def test_full_run_happy_path_streams_progress_and_releases_worktree(
    runner, sandbox, tier_store, fake_repo, tmp_path,
):
    from unittest.mock import MagicMock, patch

    tier_store.set_active(42, "STANDARD")
    send, captured = _make_progress_capture()

    # mock make_sandbox_hook so the pipeline reaches SUCCESS despite
    # happy_agents writer returning non-JSON (no real ruff/pytest run).
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)
    mock_make_sandbox_hook = MagicMock(return_value=mock_hook_fn)

    with (
        patch("core.real_task_handler.make_sandbox_hook", mock_make_sandbox_hook),
        patch.object(sandbox, "commit_in_worktree", return_value="abc123def456789"),
    ):
        handler = make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=happy_agents,
            task_id_factory=lambda: "task-happy-001",
        )

        handler("build a counter function", _msg(chat_id=42))
        _wait_until_idle(runner)
        # Wait briefly for on_complete after worker finishes
        _wait_for_count(captured, lambda c: any("Готово" in t for _, t in c))

    sent_chat_ids = {cid for cid, _ in captured}
    sent_texts = [t for _, t in captured]

    # All progress messages targeted the same chat
    assert sent_chat_ids == {42}
    # We see the start, agent transitions, and final 'Готово' (либо в ивенте, либо в финале)
    assert any("Старт" in t for t in sent_texts)
    assert any("worktree" in t.lower() for t in sent_texts)
    assert any("Готово" in t for t in sent_texts)
    # All 8 happy-path roles called at least once.
    for role in (
        "planning_agent", "pm_agent", "architect_agent", "writer_agent",
        "reviewer_agent", "tester_agent", "qa_agent",
    ):
        assert any(role in t for t in sent_texts), f"missing {role} in stream"

    # Worktree was released — directory shouldn't exist anymore
    worktree_path = sandbox.config.worktree_root / "task-happy-001"
    assert not worktree_path.exists()


def test_full_run_failing_pipeline_emits_failed_event(
    runner, sandbox, tier_store, tmp_path,
):
    tier_store.set_active(42, "ECONOMY")
    send, captured = _make_progress_capture()

    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=send,
        agent_registry_factory=noop_agents,
        task_id_factory=lambda: "task-noop-001",
    )

    handler("anything", _msg(chat_id=42))
    _wait_until_idle(runner)
    _wait_for_count(
        captured,
        lambda c: any("Не получилось" in t or "Провалена" in t for _, t in c),
    )

    sent_texts = [t for _, t in captured]
    assert any("Не получилось" in t or "Провалена" in t for t in sent_texts)


def test_full_run_agent_exception_terminates_pipeline(
    runner, sandbox, tier_store,
):
    tier_store.set_active(42, "ECONOMY")
    send, captured = _make_progress_capture()

    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=send,
        agent_registry_factory=exploding_agents,
        task_id_factory=lambda: "task-boom-001",
    )

    handler("anything", _msg(chat_id=42))
    _wait_until_idle(runner)
    _wait_for_count(
        captured,
        lambda c: any("Не получилось" in t or "Провалена" in t for _, t in c),
    )

    sent_texts = [t for _, t in captured]
    # Either the agent_failed event arrived or the on_complete final message
    assert (
        any("planning_agent" in t and "упал" in t for t in sent_texts)
        or any("Не получилось" in t for t in sent_texts)
    )


def test_send_progress_failure_does_not_break_worker(
    runner, sandbox, tier_store,
):
    """If send_progress raises, the worker thread must keep running cleanly."""
    tier_store.set_active(42, "STANDARD")
    call_count = {"n": 0}

    def flaky_send(_chat_id, _text):
        call_count["n"] += 1
        if call_count["n"] % 2 == 0:
            raise RuntimeError("transport down")

    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=flaky_send,
        agent_registry_factory=happy_agents,
    )
    handler("ok", _msg(chat_id=42))
    _wait_until_idle(runner)
    # Some attempted sends happened; importantly no exception escaped.
    assert call_count["n"] > 0
    assert runner.is_busy() is False


# ---------------------------------------------------------------------------
# on_complete final message
# ---------------------------------------------------------------------------


def test_on_complete_emits_success_summary_for_happy_path(
    runner, sandbox, tier_store,
):
    from unittest.mock import MagicMock, patch

    tier_store.set_active(42, "PREMIUM")
    send, captured = _make_progress_capture()

    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)
    mock_make_sandbox_hook = MagicMock(return_value=mock_hook_fn)

    with (
        patch("core.real_task_handler.make_sandbox_hook", mock_make_sandbox_hook),
        patch.object(sandbox, "commit_in_worktree", return_value="abc123def456789"),
    ):
        handler = make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=happy_agents,
            task_id_factory=lambda: "task-final-001",
        )
        handler("ok", _msg(chat_id=42))
        _wait_until_idle(runner)
    _wait_for_count(captured, lambda c: any("Готово" in t for _, t in c))

    final_msgs = [t for _, t in captured if "Готово" in t]
    assert final_msgs, "no success message"
    assert any("PREMIUM" in t for t in final_msgs)
    assert any("task-final-001" in t for t in final_msgs)


def test_on_complete_emits_failure_summary_for_noop(
    runner, sandbox, tier_store,
):
    tier_store.set_active(42, "ECONOMY")
    send, captured = _make_progress_capture()
    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=send,
        agent_registry_factory=noop_agents,
        task_id_factory=lambda: "task-fail-001",
    )
    handler("ok", _msg(chat_id=42))
    _wait_until_idle(runner)
    _wait_for_count(captured, lambda c: any("Не получилось" in t for _, t in c))

    final_msgs = [t for _, t in captured if "Не получилось" in t]
    assert final_msgs
    assert any("ECONOMY" in t for t in final_msgs)


# ---------------------------------------------------------------------------
# Edge: tier disappears between set_active and submit
# ---------------------------------------------------------------------------


class _DroppingStore(TierSessionStore):
    """active_tier_name returns a name that is NOT in the registry."""

    def active_tier_name(self, chat_id: int) -> str | None:
        return "GHOST"


def test_handle_stale_tier_returns_repick_reply(runner, sandbox):
    store = _DroppingStore(default_tier_registry())
    send, _ = _make_progress_capture()
    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=store,
        send_progress=send,
    )
    reply = handler("hi", _msg(chat_id=1))
    assert isinstance(reply, BridgeReply)
    assert "GHOST" in reply.body
    assert "/tier set" in reply.body


# ---------------------------------------------------------------------------
# Observability injection (smoke test only)
# ---------------------------------------------------------------------------


def test_observability_injected_succeeds(runner, sandbox, tier_store):
    tier_store.set_active(42, "STANDARD")
    send, _ = _make_progress_capture()
    obs = Observability()
    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=send,
        observability=obs,
        agent_registry_factory=happy_agents,
    )
    reply = handler("hi", _msg(chat_id=42))
    assert isinstance(reply, BridgeReply)
    _wait_until_idle(runner)


def test_cost_budget_exceeded_surfaces_in_real_handler(
    runner, sandbox, tier_store,
):
    tier_store.set_active(42, "STANDARD")
    send, captured = _make_progress_capture()
    obs = Observability()

    class _RegistryWithEstimator(dict):
        pass

    def _factory(_tier):
        registry = _RegistryWithEstimator(happy_agents(_tier))
        registry.cost_estimator = lambda _agent, _args, _output: (10, 5, 0.01)
        return registry

    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=send,
        observability=obs,
        agent_registry_factory=_factory,
        config=RealTaskHandlerConfig(cost_budget_usd=0.005),
        task_id_factory=lambda: "task-budget-001",
    )

    handler("hi", _msg(chat_id=42))
    _wait_until_idle(runner)
    _wait_for_count(
        captured,
        lambda c: any("cost_budget_exceeded" in t for _, t in c),
    )

    final_msgs = [t for _, t in captured if "Не получилось" in t]
    assert final_msgs
    assert any("cost_budget_exceeded" in t for t in final_msgs)


# ---------------------------------------------------------------------------
# 14b-9: commit_in_worktree wired on SUCCESS / not called on FAIL
# ---------------------------------------------------------------------------


def _ok_validation_report():
    from core.quality_gates import CheckResult
    from core.runtime_validator import ValidationReport, ValidationStrategy

    return ValidationReport(
        ok=True,
        strategy=ValidationStrategy.INPLACE,
        checks=(
            CheckResult(
                name="lint", ok=True, summary="ok", raw_output="", duration_ms=0
            ),
        ),
        duration_ms=1,
    )


def test_commit_in_worktree_called_on_success(runner, sandbox, tier_store, tmp_path):
    """When pipeline reaches SUCCESS, commit_in_worktree must be called once."""
    from unittest.mock import MagicMock, patch

    tier_store.set_active(42, "PREMIUM")
    send, _captured = _make_progress_capture()

    fake_sha = "abc123def456789"
    mock_report = _ok_validation_report()
    # hook builder: returns a callable that returns mock_report
    mock_hook_fn = MagicMock(return_value=mock_report)
    mock_make_sandbox_hook = MagicMock(return_value=mock_hook_fn)

    with (
        patch("core.real_task_handler.make_sandbox_hook", mock_make_sandbox_hook),
        patch.object(sandbox, "commit_in_worktree", return_value=fake_sha) as mock_commit,
    ):
        handler = make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=happy_agents,
            task_id_factory=lambda: "task-commit-001",
        )
        handler("build a CLI tool", _msg(chat_id=42))
        _wait_until_idle(runner)

    mock_commit.assert_called_once()
    # Commit message should contain first 60 chars of task text
    commit_msg = mock_commit.call_args.kwargs["message"]
    assert "build a CLI tool" in commit_msg


def test_commit_in_worktree_not_called_on_fail(runner, sandbox, tier_store):
    """When pipeline does NOT reach SUCCESS, commit_in_worktree must not be called."""
    from unittest.mock import patch

    tier_store.set_active(42, "ECONOMY")
    send, _ = _make_progress_capture()

    with patch.object(sandbox, "commit_in_worktree") as mock_commit:
        handler = make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=noop_agents,
            task_id_factory=lambda: "task-noop-001",
        )
        handler("build something", _msg(chat_id=42))
        _wait_until_idle(runner)

    mock_commit.assert_not_called()


def test_commit_sha_appears_in_success_message(runner, sandbox, tier_store):
    """commit_sha[:8] must appear in the final ✅ Готово message."""
    from unittest.mock import MagicMock, patch

    tier_store.set_active(42, "PREMIUM")
    send, captured = _make_progress_capture()

    fake_sha = "deadbeef12345678"
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)
    mock_make_sandbox_hook = MagicMock(return_value=mock_hook_fn)

    with (
        patch("core.real_task_handler.make_sandbox_hook", mock_make_sandbox_hook),
        patch.object(sandbox, "commit_in_worktree", return_value=fake_sha),
    ):
        handler = make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=happy_agents,
            task_id_factory=lambda: "task-sha-001",
        )
        handler("make something", _msg(chat_id=42))
        _wait_until_idle(runner)
        _wait_for_count(captured, lambda c: any("Готово" in t for _, t in c))

    success_msgs = [t for _, t in captured if "Готово" in t]
    assert success_msgs, "no success message received"
    assert any(fake_sha[:8] in t for t in success_msgs), (
        f"commit sha {fake_sha[:8]!r} not found in: {success_msgs}"
    )


def test_commit_error_surfaces_as_task_failed_not_success(runner, sandbox, tier_store):
    """If commit_in_worktree raises after pipeline SUCCESS, the worker must:
      - NOT emit ✅ Готово (that would be a lie — branch has no commit)
      - emit ❌ Не получилось with commit_failed reason
      - not crash (on_complete still runs)
    """
    from unittest.mock import MagicMock, patch

    from core.sandbox_workspace import SandboxError

    tier_store.set_active(42, "PREMIUM")
    send, captured = _make_progress_capture()

    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)
    mock_make_sandbox_hook = MagicMock(return_value=mock_hook_fn)

    with (
        patch("core.real_task_handler.make_sandbox_hook", mock_make_sandbox_hook),
        patch.object(
            sandbox,
            "commit_in_worktree",
            side_effect=SandboxError("nothing_to_commit"),
        ),
    ):
        handler = make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=happy_agents,
            task_id_factory=lambda: "task-err-commit-001",
        )
        handler("build x", _msg(chat_id=42))
        _wait_until_idle(runner)

    # commit_failed must be reported honestly — NOT as Готово
    _wait_for_count(
        captured,
        lambda c: any("Не получилось" in t or "commit_failed" in t for _, t in c),
    )
    all_texts = [t for _, t in captured]
    assert any("commit_failed" in t or "Не получилось" in t for t in all_texts), (
        f"expected commit_failed/Не получилось in messages, got: {all_texts}"
    )
    assert not any("Готово" in t for t in all_texts), (
        "worker must NOT emit Готово when commit failed"
    )


def test_cancellation_overrides_success_to_cancelled(
    runner, sandbox, tier_store, fake_repo, tmp_path,
):
    """14c-fix critical bug: if /stop pressed while pipeline runs, the handler
    must NOT commit and must send ⏹ Отменено — not ✅ Готово.

    Determinism: planning_agent blocks on `agent_in_flight` until the test
    explicitly calls `runner.cancel()` and then `release_agent.set()`. This
    guarantees the cancel token is observed by `_run` after `orch.run()`,
    eliminating the timing race where happy_agents could finish before the
    main thread calls cancel().
    """
    import threading
    from unittest.mock import MagicMock, patch

    from core.task_history import TaskHistory

    tier_store.set_active(42, "STANDARD")
    send, captured = _make_progress_capture()
    history = TaskHistory()

    # Synchronisation: planning_agent waits on `release_agent`, but signals
    # `agent_in_flight` first so the main thread knows the worker has reached
    # the pipeline and it's safe to cancel.
    agent_in_flight = threading.Event()
    release_agent = threading.Event()

    def _blocking_planning(*_a) -> str:
        agent_in_flight.set()
        # Wait until the test releases us (after issuing cancel).
        release_agent.wait(timeout=5.0)
        return '{"plan": "ok"}'

    def blocking_agents(_tier):
        agents = happy_agents(_tier)
        agents["planning_agent"] = _blocking_planning
        return agents

    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)
    mock_make_sandbox_hook = MagicMock(return_value=mock_hook_fn)
    mock_commit = MagicMock(return_value="deadbeef12345678")

    with (
        patch("core.real_task_handler.make_sandbox_hook", mock_make_sandbox_hook),
        patch.object(sandbox, "commit_in_worktree", mock_commit),
    ):
        handler = make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=blocking_agents,
            task_id_factory=lambda: "task-cancel-001",
            task_history=history,
        )

        handler("build something", _msg(chat_id=42))
        # 1. Wait for the worker to enter planning_agent so we KNOW the
        #    pipeline has started before we cancel.
        assert agent_in_flight.wait(timeout=5.0), (
            "planning_agent never started — fixture wiring bug"
        )
        # 2. Cancel — this sets the token on the runner.
        runner.cancel()
        # 3. Release the agent so the rest of the pipeline can finish.
        #    Now the post-orch.run() check `cancelled = token.is_set()`
        #    will deterministically observe True.
        release_agent.set()
        _wait_until_idle(runner)

    _wait_for_count(
        captured,
        lambda c: any("Отменено" in t for _, t in c),
        timeout=5.0,
    )

    all_texts = [t for _, t in captured]

    # Must send cancellation notice
    assert any("Отменено" in t for t in all_texts), (
        f"expected ⏹ Отменено in messages, got: {all_texts}"
    )
    # Must NOT claim success
    assert not any("Готово" in t for t in all_texts), (
        "must NOT send ✅ Готово after cancellation"
    )
    # commit_in_worktree must NOT be called after cancellation
    mock_commit.assert_not_called()

    # TaskHistory record is written in _build_on_complete BEFORE the
    # ⏹ message is sent, so by the time _wait_for_count returns the
    # record is guaranteed to be present.
    summary = history.get("task-cancel-001")
    assert summary is not None, "TaskHistory must record cancelled task"
    assert summary.final_state == "CANCELLED"
    assert summary.commit_sha is None


# ---------------------------------------------------------------------------
# A4-fix: runtime validator uses "ruff check ." + run_tests=False
# ---------------------------------------------------------------------------


def test_adapter_factory_uses_dot_lint_target(runner, sandbox, tier_store):
    """_adapter_factory must supply a custom lint command targeting '.'
    so that ruff lints the whole worktree instead of the hardcoded
    ["core", "tests"] paths that don't exist in generated projects."""
    import sys
    from unittest.mock import MagicMock, patch

    from core.adapter import ProjectCommand

    captured_adapters: list = []
    captured_validators: list = []

    def _spy_make_hook(handle, adapter_factory, validator):
        # Materialise an adapter by calling the factory with a dummy path
        from pathlib import Path
        captured_adapters.append(adapter_factory(Path("/tmp")))
        captured_validators.append(validator)
        # Return a hook that immediately says ok=True so pipeline completes
        report = _ok_validation_report()
        return MagicMock(return_value=report)

    send, _captured = _make_progress_capture()
    tier_store.set_active(1, "ECONOMY")

    with (
        patch("core.real_task_handler.make_sandbox_hook", side_effect=_spy_make_hook),
        patch.object(sandbox, "commit_in_worktree", return_value="sha123"),
    ):
        handler = make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=happy_agents,
            task_id_factory=lambda: "task-lint-dot-001",
        )
        handler("write a sum function", _msg(chat_id=1))
        _wait_until_idle(runner)

    assert captured_adapters, "adapter_factory was never called"
    adapter = captured_adapters[0]
    assert "lint" in adapter.commands, "adapter must have a 'lint' command"
    lint_cmd = adapter.commands["lint"]
    assert isinstance(lint_cmd, ProjectCommand)
    # Must target "." — NOT ["core", "tests"]
    assert "." in lint_cmd.cmd, f"lint cmd must include '.', got: {lint_cmd.cmd}"
    assert sys.executable in lint_cmd.cmd, "lint cmd must use sys.executable"


def test_runtime_validator_run_tests_is_false(runner, sandbox, tier_store):
    """RuntimeValidator must be built with run_tests=False because tester
    output is not written to the worktree — only writer output is."""
    from unittest.mock import MagicMock, patch

    from core.runtime_validator import RuntimeValidator

    captured_validators: list = []

    def _spy_make_hook(handle, adapter_factory, validator):
        captured_validators.append(validator)
        report = _ok_validation_report()
        return MagicMock(return_value=report)

    send, _captured = _make_progress_capture()
    tier_store.set_active(2, "ECONOMY")

    with (
        patch("core.real_task_handler.make_sandbox_hook", side_effect=_spy_make_hook),
        patch.object(sandbox, "commit_in_worktree", return_value="sha456"),
    ):
        handler = make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=happy_agents,
            task_id_factory=lambda: "task-no-tests-001",
        )
        handler("write a sum function", _msg(chat_id=2))
        _wait_until_idle(runner)

    assert captured_validators, "make_sandbox_hook was never called"
    validator = captured_validators[0]
    assert isinstance(validator, RuntimeValidator)
    assert validator._run_tests is False, (
        "run_tests must be False — tester files are not in the worktree"
    )
    assert validator._run_lint is True, "run_lint must remain True"


def test_project_aware_pipeline_uses_collaboration_bus_and_public_projection(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    from unittest.mock import MagicMock, patch

    tier_store.set_active(-100123, "STANDARD")
    send_envelope, captured = _make_progress_envelope_capture()
    snapshot = _project_snapshot(
        fake_repo,
        chat_binding=_chat_binding(chat_id=-100123),
        policy=_policy(require_owner_approval_for_hires=False),
    )
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="collaboration-runtime.db",
    )
    dispatcher = _make_dispatcher()
    factory = build_dispatcher_agent_registry_factory(dispatcher)
    planning_calls = 0

    def _dispatch(req, _tier):
        nonlocal planning_calls
        if req.agent_role == "planning_agent":
            planning_calls += 1
            if planning_calls == 1:
                return _make_dispatch_response(
                    '{"action":"ask_another_agent","recipient_role":"reviewer_agent","question":"Нужен риск-анализ по API"}'
                )
            assert "INTERNAL CONSULTATION TRANSCRIPT" in req.messages[1]["content"]
            return _make_dispatch_response('{"plan":"ok"}')
        if req.agent_role == "pm_agent":
            return _make_dispatch_response(
                '{"tasks":[],"specialization_hints":[]}'
            )
        if req.agent_role == "architect_agent":
            return _make_dispatch_response('{"arch":"spec"}')
        if req.agent_role == "writer_agent":
            return _make_dispatch_response("def f(): return 42")
        if req.agent_role == "reviewer_agent":
            if "INTERNAL CONSULTATION MODE" in req.messages[0]["content"]:
                return _make_dispatch_response(
                    "Главный риск — silent regression в contract validation."
                )
            return _make_dispatch_response('{"verdict":"APPROVED"}')
        if req.agent_role == "tester_agent":
            return _make_dispatch_response("tests pass")
        if req.agent_role == "qa_agent":
            return _make_dispatch_response('{"verdict":"PASS"}')
        if req.agent_role == "fixer_agent":
            return _make_dispatch_response("def f(): return 42")
        raise AssertionError(f"unexpected role {req.agent_role}")

    dispatcher.dispatch = MagicMock(side_effect=_dispatch)  # type: ignore[method-assign]
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    with (
        patch("core.project_runtime_router._build_sandbox", return_value=sandbox),
        patch("core.real_task_handler.make_sandbox_hook", return_value=mock_hook_fn),
        patch.object(sandbox, "commit_in_worktree", return_value="abc123def456789"),
    ):
        handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress_envelope=send_envelope,
            agent_registry_factory=factory,
            task_id_factory=lambda: "task-42",
        )
        reply = handler(
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
        assert isinstance(reply, BridgeReply)
        _wait_until_idle(runner)
        _wait_for_count(
            captured,
            lambda envelopes: any("Готово" in env.message.text for env in envelopes),
        )

    backend_bus = StateBackedAgentBus(runtime_router.registry.state_db)
    thread = backend_bus.get_task_thread("alpha_project", "task-42")
    assert thread is not None
    messages = backend_bus.list_thread_messages("alpha_project", thread.thread_id)
    assert tuple(message.message_kind for message in messages) == ("request", "reply")
    assert messages[0].sender_role == "planning_agent"
    assert messages[0].recipient_role == "reviewer_agent"
    assert messages[1].sender_role == "reviewer_agent"
    assert messages[1].in_reply_to is not None
    assert messages[1].in_reply_to.message_id == messages[0].message_id

    projection_texts = [env.message.text for env in captured]
    assert any("Маршрут: planning_agent -> reviewer_agent" in text for text in projection_texts)
    assert any("Маршрут: reviewer_agent -> planning_agent" in text for text in projection_texts)
    assert any(env.sender_role == "planning_agent" and env.delivery_role is None for env in captured)
    assert any(env.sender_role == "reviewer_agent" and env.delivery_role is None for env in captured)
    assert any("Готово" in text for text in projection_texts)
    assert runtime_router.registry.get_project_specialist_roster(
        "alpha_project"
    ).specialist_roles == ()
    assert not any("Логический hire" in text for text in projection_texts)


def test_project_aware_runtime_propagates_pm_specialization_hints_to_specialist_consult_prompt(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    from unittest.mock import MagicMock, patch

    tier_store.set_active(-100123, "STANDARD")
    send_envelope, captured = _make_progress_envelope_capture()
    snapshot = _project_snapshot(
        fake_repo,
        chat_binding=_chat_binding(chat_id=-100123),
        policy=_policy(require_owner_approval_for_hires=False),
    )
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="specialist-hints-runtime.db",
    )
    dispatcher = _make_dispatcher()
    factory = build_dispatcher_agent_registry_factory(dispatcher)
    architect_calls = 0

    def _dispatch(req, _tier):
        nonlocal architect_calls
        if req.agent_role == "planning_agent":
            return _make_dispatch_response('{"plan":"ok"}')
        if req.agent_role == "pm_agent":
            return _make_dispatch_response(
                '{"tasks":[],"specialization_hints":[{"specialist_role":"security_agent","reason":"Задача затрагивает auth, secrets и trust boundaries."}]}'
            )
        if req.agent_role == "architect_agent":
            architect_calls += 1
            if architect_calls == 1:
                return _make_dispatch_response(
                    '{"action":"ask_another_agent","recipient_role":"security_agent","question":"Нужен security review по API"}'
                )
            assert "INTERNAL CONSULTATION TRANSCRIPT" in req.messages[1]["content"]
            return _make_dispatch_response('{"arch":"spec"}')
        if req.agent_role == "security_agent":
            user_content = req.messages[1]["content"]
            assert "Specialization context" in user_content
            assert "role: security_agent" in user_content
            assert (
                "task_specific_hint: Задача затрагивает auth, secrets и trust boundaries."
                in user_content
            )
            return _make_dispatch_response(
                "Проверь authz, secret handling и trust boundaries."
            )
        if req.agent_role == "writer_agent":
            return _make_dispatch_response("def f(): return 42")
        if req.agent_role == "reviewer_agent":
            return _make_dispatch_response('{"verdict":"APPROVED"}')
        if req.agent_role == "tester_agent":
            return _make_dispatch_response("tests pass")
        if req.agent_role == "qa_agent":
            return _make_dispatch_response('{"verdict":"PASS"}')
        if req.agent_role == "fixer_agent":
            return _make_dispatch_response("def f(): return 42")
        raise AssertionError(f"unexpected role {req.agent_role}")

    dispatcher.dispatch = MagicMock(side_effect=_dispatch)  # type: ignore[method-assign]
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    with (
        patch("core.project_runtime_router._build_sandbox", return_value=sandbox),
        patch("core.real_task_handler.make_sandbox_hook", return_value=mock_hook_fn),
        patch.object(sandbox, "commit_in_worktree", return_value="abc123def456789"),
    ):
        handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress_envelope=send_envelope,
            agent_registry_factory=factory,
            task_id_factory=lambda: "task-42",
        )
        reply = handler(
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
        assert isinstance(reply, BridgeReply)
        _wait_until_idle(runner)
        _wait_for_count(
            captured,
            lambda envelopes: any("Готово" in env.message.text for env in envelopes),
        )

    backend_bus = StateBackedAgentBus(runtime_router.registry.state_db)
    thread = backend_bus.get_task_thread("alpha_project", "task-42")
    assert thread is not None
    messages = backend_bus.list_thread_messages("alpha_project", thread.thread_id)
    assert tuple(message.message_kind for message in messages) == ("request", "reply")
    assert messages[0].sender_role == "architect_agent"
    assert messages[0].recipient_role == "security_agent"
    assert messages[1].sender_role == "security_agent"
    assert messages[1].in_reply_to is not None
    assert messages[1].in_reply_to.message_id == messages[0].message_id

    projection_texts = [env.message.text for env in captured]
    assert any("Маршрут: architect_agent -> security_agent" in text for text in projection_texts)
    assert any("Маршрут: security_agent -> architect_agent" in text for text in projection_texts)
    assert any("Логический hire выполнен" in text for text in projection_texts)
    assert any("security_agent" in text for text in projection_texts)
    assert any("Готово" in text for text in projection_texts)
    assert runtime_router.registry.get_project_specialist_roster(
        "alpha_project"
    ).specialist_roles == ("security_agent",)


def test_project_aware_runtime_creates_pending_hire_request_when_owner_approval_is_required(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    from unittest.mock import MagicMock, patch

    tier_store.set_active(-100123, "STANDARD")
    send_envelope, captured = _make_progress_envelope_capture()
    snapshot = _project_snapshot(
        fake_repo,
        chat_binding=_chat_binding(chat_id=-100123),
        policy=_policy(require_owner_approval_for_hires=True),
    )
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="logical-hiring-pending-approval.db",
    )
    dispatcher = _make_dispatcher()
    factory = build_dispatcher_agent_registry_factory(dispatcher)

    def _dispatch(req, _tier):
        payloads = {
            "planning_agent": '{"plan":"ok"}',
            "pm_agent": (
                '{"tasks":[],"specialization_hints":'
                '[{"specialist_role":"security_agent","reason":"Auth и secrets в scope."}]}'
            ),
            "architect_agent": '{"arch":"spec"}',
            "writer_agent": "def f(): return 42",
            "reviewer_agent": '{"verdict":"APPROVED"}',
            "tester_agent": "tests pass",
            "qa_agent": '{"verdict":"PASS"}',
            "fixer_agent": "def f(): return 42",
        }
        return _make_dispatch_response(payloads[req.agent_role])

    dispatcher.dispatch = MagicMock(side_effect=_dispatch)  # type: ignore[method-assign]
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    with (
        patch("core.project_runtime_router._build_sandbox", return_value=sandbox),
        patch("core.real_task_handler.make_sandbox_hook", return_value=mock_hook_fn),
        patch.object(sandbox, "commit_in_worktree", return_value="abc123def456789"),
    ):
        handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress_envelope=send_envelope,
            agent_registry_factory=factory,
            task_id_factory=lambda: "task-42",
        )
        reply = handler(
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
        assert isinstance(reply, BridgeReply)
        _wait_until_idle(runner)
        _wait_for_count(
            captured,
            lambda envelopes: any("Готово" in env.message.text for env in envelopes),
        )

    projection_texts = [env.message.text for env in captured]
    assert any("owner approval" in text for text in projection_texts)
    assert any("roster пока не изменён" in text for text in projection_texts)
    assert runtime_router.registry.get_project_specialist_roster(
        "alpha_project"
    ).specialist_roles == ()
    pending = runtime_router.registry.list_pending_hire_requests("alpha_project")
    assert len(pending) == 1
    assert pending[0].specialist_role == "security_agent"


def test_project_aware_runtime_blocks_logical_hire_when_policy_disallows_it(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    from unittest.mock import MagicMock, patch

    tier_store.set_active(-100123, "STANDARD")
    send_envelope, captured = _make_progress_envelope_capture()
    snapshot = _project_snapshot(
        fake_repo,
        chat_binding=_chat_binding(chat_id=-100123),
        policy=_policy(allow_hiring=False),
    )
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="logical-hiring-blocked.db",
    )
    dispatcher = _make_dispatcher()
    factory = build_dispatcher_agent_registry_factory(dispatcher)

    def _dispatch(req, _tier):
        payloads = {
            "planning_agent": '{"plan":"ok"}',
            "pm_agent": (
                '{"tasks":[],"specialization_hints":'
                '[{"specialist_role":"security_agent","reason":"Auth и secrets в scope."}]}'
            ),
            "architect_agent": '{"arch":"spec"}',
            "writer_agent": "def f(): return 42",
            "reviewer_agent": '{"verdict":"APPROVED"}',
            "tester_agent": "tests pass",
            "qa_agent": '{"verdict":"PASS"}',
            "fixer_agent": "def f(): return 42",
        }
        return _make_dispatch_response(payloads[req.agent_role])

    dispatcher.dispatch = MagicMock(side_effect=_dispatch)  # type: ignore[method-assign]
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    with (
        patch("core.project_runtime_router._build_sandbox", return_value=sandbox),
        patch("core.real_task_handler.make_sandbox_hook", return_value=mock_hook_fn),
        patch.object(sandbox, "commit_in_worktree", return_value="abc123def456789"),
    ):
        handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress_envelope=send_envelope,
            agent_registry_factory=factory,
            task_id_factory=lambda: "task-42",
        )
        reply = handler(
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
        assert isinstance(reply, BridgeReply)
        _wait_until_idle(runner)
        _wait_for_count(
            captured,
            lambda envelopes: any("Готово" in env.message.text for env in envelopes),
        )

    projection_texts = [env.message.text for env in captured]
    assert any("Логический hire заблокирован" in text for text in projection_texts)
    assert runtime_router.registry.get_project_specialist_roster(
        "alpha_project"
    ).specialist_roles == ()


def test_project_aware_runtime_reloads_persisted_policy_before_logical_hire(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    from unittest.mock import MagicMock, patch

    tier_store.set_active(-100123, "STANDARD")
    send_envelope, captured = _make_progress_envelope_capture()
    snapshot = _project_snapshot(
        fake_repo,
        chat_binding=_chat_binding(chat_id=-100123),
        policy=_policy(allow_hiring=True),
    )
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="logical-hiring-stale-policy.db",
    )
    dispatcher = _make_dispatcher()
    factory = build_dispatcher_agent_registry_factory(dispatcher)

    def _dispatch(req, _tier):
        if req.agent_role == "pm_agent":
            runtime_router.registry.set_project_policy(_policy(allow_hiring=False))
            return _make_dispatch_response(
                '{"tasks":[],"specialization_hints":'
                '[{"specialist_role":"security_agent","reason":"Auth и secrets в scope."}]}'
            )
        payloads = {
            "planning_agent": '{"plan":"ok"}',
            "architect_agent": '{"arch":"spec"}',
            "writer_agent": "def f(): return 42",
            "reviewer_agent": '{"verdict":"APPROVED"}',
            "tester_agent": "tests pass",
            "qa_agent": '{"verdict":"PASS"}',
            "fixer_agent": "def f(): return 42",
        }
        return _make_dispatch_response(payloads[req.agent_role])

    dispatcher.dispatch = MagicMock(side_effect=_dispatch)  # type: ignore[method-assign]
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    with (
        patch("core.project_runtime_router._build_sandbox", return_value=sandbox),
        patch("core.real_task_handler.make_sandbox_hook", return_value=mock_hook_fn),
        patch.object(sandbox, "commit_in_worktree", return_value="abc123def456789"),
    ):
        handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress_envelope=send_envelope,
            agent_registry_factory=factory,
            task_id_factory=lambda: "task-42",
        )
        reply = handler(
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
        assert isinstance(reply, BridgeReply)
        _wait_until_idle(runner)
        _wait_for_count(
            captured,
            lambda envelopes: any("Готово" in env.message.text for env in envelopes),
        )

    projection_texts = [env.message.text for env in captured]
    assert any("Логический hire заблокирован" in text for text in projection_texts)
    assert runtime_router.registry.get_project_specialist_roster(
        "alpha_project"
    ).specialist_roles == ()


def test_project_aware_runtime_does_not_duplicate_already_hired_specialist(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    from unittest.mock import MagicMock, patch

    tier_store.set_active(-100123, "STANDARD")
    send_envelope, captured = _make_progress_envelope_capture()
    snapshot = _project_snapshot(
        fake_repo,
        chat_binding=_chat_binding(chat_id=-100123),
    )
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="logical-hiring-already.db",
    )
    runtime_router.registry.add_project_specialist(
        "alpha_project",
        "security_agent",
    )
    dispatcher = _make_dispatcher()
    factory = build_dispatcher_agent_registry_factory(dispatcher)

    def _dispatch(req, _tier):
        payloads = {
            "planning_agent": '{"plan":"ok"}',
            "pm_agent": (
                '{"tasks":[],"specialization_hints":'
                '[{"specialist_role":"security_agent","reason":"Auth и secrets в scope."}]}'
            ),
            "architect_agent": '{"arch":"spec"}',
            "writer_agent": "def f(): return 42",
            "reviewer_agent": '{"verdict":"APPROVED"}',
            "tester_agent": "tests pass",
            "qa_agent": '{"verdict":"PASS"}',
            "fixer_agent": "def f(): return 42",
        }
        return _make_dispatch_response(payloads[req.agent_role])

    dispatcher.dispatch = MagicMock(side_effect=_dispatch)  # type: ignore[method-assign]
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    with (
        patch("core.project_runtime_router._build_sandbox", return_value=sandbox),
        patch("core.real_task_handler.make_sandbox_hook", return_value=mock_hook_fn),
        patch.object(sandbox, "commit_in_worktree", return_value="abc123def456789"),
    ):
        handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress_envelope=send_envelope,
            agent_registry_factory=factory,
            task_id_factory=lambda: "task-42",
        )
        reply = handler(
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
        assert isinstance(reply, BridgeReply)
        _wait_until_idle(runner)
        _wait_for_count(
            captured,
            lambda envelopes: any("Готово" in env.message.text for env in envelopes),
        )

    projection_texts = [env.message.text for env in captured]
    assert any("Логический hire не потребовался" in text for text in projection_texts)
    assert runtime_router.registry.get_project_specialist_roster(
        "alpha_project"
    ).specialist_roles == ("security_agent",)


def test_project_aware_runtime_converges_when_owner_adds_specialist_before_apply(
    runner,
    sandbox,
    tier_store,
    fake_repo,
    tmp_path,
):
    from unittest.mock import MagicMock, patch

    tier_store.set_active(-100123, "STANDARD")
    send_envelope, captured = _make_progress_envelope_capture()
    snapshot = _project_snapshot(
        fake_repo,
        chat_binding=_chat_binding(chat_id=-100123),
    )
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="logical-hiring-owner-adds-first.db",
    )
    dispatcher = _make_dispatcher()
    factory = build_dispatcher_agent_registry_factory(dispatcher)

    def _dispatch(req, _tier):
        if req.agent_role == "pm_agent":
            runtime_router.registry.add_project_specialist(
                "alpha_project",
                "security_agent",
            )
            return _make_dispatch_response(
                '{"tasks":[],"specialization_hints":'
                '[{"specialist_role":"security_agent","reason":"Auth и secrets в scope."}]}'
            )
        payloads = {
            "planning_agent": '{"plan":"ok"}',
            "architect_agent": '{"arch":"spec"}',
            "writer_agent": "def f(): return 42",
            "reviewer_agent": '{"verdict":"APPROVED"}',
            "tester_agent": "tests pass",
            "qa_agent": '{"verdict":"PASS"}',
            "fixer_agent": "def f(): return 42",
        }
        return _make_dispatch_response(payloads[req.agent_role])

    dispatcher.dispatch = MagicMock(side_effect=_dispatch)  # type: ignore[method-assign]
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    with (
        patch("core.project_runtime_router._build_sandbox", return_value=sandbox),
        patch("core.real_task_handler.make_sandbox_hook", return_value=mock_hook_fn),
        patch.object(sandbox, "commit_in_worktree", return_value="abc123def456789"),
    ):
        handler = make_real_task_handler(
            runner=runner,
            runtime_router=runtime_router,
            tier_store=tier_store,
            send_progress_envelope=send_envelope,
            agent_registry_factory=factory,
            task_id_factory=lambda: "task-42",
        )
        reply = handler(
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
        assert isinstance(reply, BridgeReply)
        _wait_until_idle(runner)
        _wait_for_count(
            captured,
            lambda envelopes: any("Готово" in env.message.text for env in envelopes),
        )

    projection_texts = [env.message.text for env in captured]
    assert any("Логический hire не потребовался" in text for text in projection_texts)
    assert not any("Логический hire не удалось обработать" in text for text in projection_texts)
    assert runtime_router.registry.get_project_specialist_roster(
        "alpha_project"
    ).specialist_roles == ("security_agent",)


def test_non_project_dispatcher_path_stays_legacy_without_collaboration(
    runner,
    sandbox,
    tier_store,
):
    from unittest.mock import MagicMock, patch

    tier_store.set_active(42, "STANDARD")
    send, captured = _make_progress_capture()
    dispatcher = _make_dispatcher()
    factory = build_dispatcher_agent_registry_factory(dispatcher)

    def _dispatch(req, _tier):
        payloads = {
            "planning_agent": '{"plan":"ok"}',
            "pm_agent": '{"tasks":[],"specialization_hints":[]}',
            "architect_agent": '{"arch":"spec"}',
            "writer_agent": "def f(): return 42",
            "reviewer_agent": '{"verdict":"APPROVED"}',
            "tester_agent": "tests pass",
            "qa_agent": '{"verdict":"PASS"}',
            "fixer_agent": "def f(): return 42",
        }
        return _make_dispatch_response(payloads[req.agent_role])

    dispatcher.dispatch = MagicMock(side_effect=_dispatch)  # type: ignore[method-assign]
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    with (
        patch("core.real_task_handler.make_sandbox_hook", return_value=mock_hook_fn),
        patch.object(sandbox, "commit_in_worktree", return_value="abc123def456789"),
    ):
        handler = make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory=factory,
            task_id_factory=lambda: "task-plain-42",
        )
        reply = handler("Сделай CLI tool.", _msg(chat_id=42))
        assert isinstance(reply, BridgeReply)
        _wait_until_idle(runner)
        _wait_for_count(
            captured,
            lambda rows: any("Готово" in text for _, text in rows),
        )

    assert not any("Маршрут:" in text for _, text in captured)
