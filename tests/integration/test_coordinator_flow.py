from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.agent_personas import default_registry
from core.background_runner import BackgroundTaskRunner
from core.bot_runner import build_bridge_from_env, build_command_registry
from core.coordinator_role import COORDINATOR_ROLE
from core.coordinator_team_assembly import BASELINE_INTERNAL_TEAM_ROLE_ORDER
from core.memory import PipelineMemory
from core.model_tier import default_registry as default_tier_registry
from core.project_chat_binding_service import ProjectChatBindingService
from core.project_context import ProjectContextResolver
from core.project_migration_service import ProjectMigrationService
from core.project_models import Project, ProjectChatBinding, ProjectPolicy
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.project_runtime import ProjectRuntimeBinding
from core.project_runtime_router import ProjectRuntimeRouter
from core.project_summary_service import ProjectSummaryService
from core.quality_gates import CheckResult
from core.real_task_handler import make_real_task_handler
from core.runtime_validator import ValidationReport, ValidationStrategy
from core.sandbox_workspace import (
    SandboxConfig,
    SandboxError,
    SandboxWorkspace,
    _RunResult,
    _SubprocessRunner,
)
from core.state_db import StateDB
from core.telegram_bridge import BridgeReply, IncomingMessage, OutgoingMessage, TelegramBridge
from core.tier_session import TierSessionStore

OWNER_ID = 777


class FakeGitRunner(_SubprocessRunner):
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, cmd, cwd, env, timeout):
        self.calls.append({"cmd": cmd, "cwd": cwd, "timeout": timeout})
        return _RunResult(returncode=0, stdout="", stderr="")


@pytest.fixture
def runner():
    background_runner = BackgroundTaskRunner()
    yield background_runner
    background_runner.shutdown()


@pytest.fixture
def sandbox(tmp_path: Path) -> SandboxWorkspace:
    repo = tmp_path / "sandbox-main-repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return SandboxWorkspace(
        SandboxConfig(
            main_repo_path=repo,
            worktree_root=tmp_path / "sandbox-worktrees",
        ),
        runner=FakeGitRunner(),
    )


def _make_db(tmp_path: Path, name: str = "state.db") -> StateDB:
    return StateDB(tmp_path / name)


def _git_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    return repo


def _project(**overrides: object) -> Project:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "slug": "alpha-project",
        "name": "Alpha Project",
        "description": "Primary AI Office project.",
        "owner_user_id": OWNER_ID,
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


def _runtime_binding(repo_path: Path, **overrides: object) -> ProjectRuntimeBinding:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "adapter_name": "alpha_adapter",
        "repo_path": repo_path,
        "worktree_root": repo_path.parent / f"{repo_path.name}-worktrees",
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


def _chat_binding(**overrides: object) -> ProjectChatBinding:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "chat_provider": "telegram",
        "chat_id": -100123450001,
    }
    data.update(overrides)
    return ProjectChatBinding(**data)


def _project_snapshot(
    repo_path: Path,
    *,
    project_id: str = "alpha_project",
    slug: str = "alpha-project",
    name: str = "Alpha Project",
    owner_user_id: int = OWNER_ID,
    chat_binding: ProjectChatBinding | None = None,
) -> ProjectSnapshot:
    return ProjectSnapshot(
        project=_project(
            project_id=project_id,
            slug=slug,
            name=name,
            owner_user_id=owner_user_id,
        ),
        policy=_policy(project_id=project_id),
        chat_binding=chat_binding,
        runtime_binding=_runtime_binding(
            repo_path,
            project_id=project_id,
            adapter_name=f"{project_id}_adapter",
        ),
    )


def _message(
    *,
    chat_id: int,
    user_id: int,
    message_id: int,
    text: str,
) -> IncomingMessage:
    return IncomingMessage(
        chat_id=chat_id,
        user_id=user_id,
        message_id=message_id,
        text=text,
    )


def _captured_send() -> tuple[Callable[[OutgoingMessage], None], list[OutgoingMessage]]:
    captured: list[OutgoingMessage] = []

    def _send(msg: OutgoingMessage) -> None:
        captured.append(msg)

    return _send, captured


def _make_progress_capture() -> tuple[Callable[[int, str], None], list[tuple[int, str]]]:
    captured: list[tuple[int, str]] = []
    lock = threading.Lock()

    def _send(chat_id: int, text: str) -> None:
        with lock:
            captured.append((chat_id, text))

    return _send, captured


def _wait_until_idle(background_runner: BackgroundTaskRunner, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while background_runner.is_busy() and time.time() < deadline:
        time.sleep(0.02)


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timeout_waiting_for_predicate")


def _ok_validation_report() -> ValidationReport:
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


def _happy_agents() -> dict[str, Callable[..., str]]:
    return {
        "planning_agent": lambda *_a: '{"plan": "ok"}',
        "pm_agent": lambda *_a: '{"tasks": []}',
        "architect_agent": lambda *_a: '{"arch": "spec"}',
        "writer_agent": lambda *_a: "def f(): return 42",
        "reviewer_agent": lambda *_a: '{"verdict": "APPROVED"}',
        "tester_agent": lambda *_a: "tests pass",
        "qa_agent": lambda *_a: '{"verdict": "PASS"}',
        "fixer_agent": lambda *_a: "def f(): return 42",
    }


def _assert_role_order(text: str) -> None:
    positions = [text.index(f"role_id: {role}") for role in BASELINE_INTERNAL_TEAM_ROLE_ORDER]
    assert positions == sorted(positions)


def _build_runtime_router(registry: ProjectRegistry) -> ProjectRuntimeRouter:
    return ProjectRuntimeRouter(registry, None)


@dataclass
class _CommandHarness:
    bridge: TelegramBridge
    registry: ProjectRegistry
    runtime_router: ProjectRuntimeRouter
    captured: list[OutgoingMessage]
    task_calls: list[dict[str, object]]


def _build_command_harness(
    registry: ProjectRegistry,
    *,
    owner_user_ids: tuple[int, ...] = (OWNER_ID,),
) -> _CommandHarness:
    resolver = ProjectContextResolver(registry, owner_user_ids)
    binding_service = ProjectChatBindingService(registry, owner_user_ids)
    migration_service = ProjectMigrationService(
        registry,
        binding_service,
        owner_user_ids,
    )
    summary_service = ProjectSummaryService(
        registry,
        resolver,
        migration_service=migration_service,
    )
    runtime_router = _build_runtime_router(registry)
    commands = build_command_registry(
        default_registry(),
        runtime_router=runtime_router,
        project_chat_binding_service=binding_service,
        project_migration_service=migration_service,
        project_context_resolver=resolver,
        project_summary_service=summary_service,
    )
    send, captured = _captured_send()
    task_calls: list[dict[str, object]] = []

    def _task_handler(text: str, msg: IncomingMessage) -> BridgeReply:
        resolved = runtime_router.resolve_message_runtime(msg)
        task_calls.append(
            {
                "text": text,
                "project_id": msg.project_id,
                "project_context_source": msg.project_context_source,
                "resolved_project_id": resolved.snapshot.project.project_id,
            }
        )
        return BridgeReply(
            persona_role=COORDINATOR_ROLE,
            body=f"coordinator routed {resolved.snapshot.project.project_id}",
        )

    bridge = TelegramBridge(
        owner_chat_ids=frozenset(owner_user_ids),
        send=send,
        commands=commands,
        task_handler=_task_handler,
        project_context_resolver=resolver,
    )
    return _CommandHarness(
        bridge=bridge,
        registry=registry,
        runtime_router=runtime_router,
        captured=captured,
        task_calls=task_calls,
    )


@dataclass
class _RealHarness:
    bridge: TelegramBridge
    registry: ProjectRegistry
    runtime_router: ProjectRuntimeRouter
    memory: PipelineMemory
    bridge_captured: list[OutgoingMessage]
    progress_captured: list[tuple[int, str]]
    task_calls: list[dict[str, object]]
    task_id: str


def _build_real_harness(
    *,
    registry: ProjectRegistry,
    runner: BackgroundTaskRunner,
    sandbox: SandboxWorkspace,
    task_id: str,
    memory: PipelineMemory,
    agent_registry_factory: Callable[[object], dict[str, Callable[..., str]]],
    active_chat_ids: tuple[int, ...],
    owner_user_ids: tuple[int, ...] = (OWNER_ID,),
) -> _RealHarness:
    resolver = ProjectContextResolver(registry, owner_user_ids)
    binding_service = ProjectChatBindingService(registry, owner_user_ids)
    migration_service = ProjectMigrationService(
        registry,
        binding_service,
        owner_user_ids,
    )
    summary_service = ProjectSummaryService(
        registry,
        resolver,
        migration_service=migration_service,
    )
    runtime_router = _build_runtime_router(registry)
    tier_store = TierSessionStore(default_tier_registry())
    for chat_id in active_chat_ids:
        tier_store.set_active(chat_id, "STANDARD")
    send, bridge_captured = _captured_send()
    send_progress, progress_captured = _make_progress_capture()
    commands = build_command_registry(
        default_registry(),
        tier_store=tier_store,
        runner=runner,
        runtime_router=runtime_router,
        project_chat_binding_service=binding_service,
        project_migration_service=migration_service,
        project_context_resolver=resolver,
        project_summary_service=summary_service,
    )
    real_task_handler = make_real_task_handler(
        runner=runner,
        runtime_router=runtime_router,
        tier_store=tier_store,
        send_progress=send_progress,
        agent_registry_factory=agent_registry_factory,
        memory_factory=lambda: memory,
        task_id_factory=lambda: task_id,
    )
    task_calls: list[dict[str, object]] = []

    def _recording_task_handler(text: str, msg: IncomingMessage) -> BridgeReply | None:
        task_calls.append(
            {
                "text": text,
                "project_id": msg.project_id,
                "project_context_source": msg.project_context_source,
            }
        )
        return real_task_handler(text, msg)

    bridge = TelegramBridge(
        owner_chat_ids=frozenset(owner_user_ids),
        send=send,
        commands=commands,
        task_handler=_recording_task_handler,
        project_context_resolver=resolver,
    )
    return _RealHarness(
        bridge=bridge,
        registry=registry,
        runtime_router=runtime_router,
        memory=memory,
        bridge_captured=bridge_captured,
        progress_captured=progress_captured,
        task_calls=task_calls,
        task_id=task_id,
    )


def test_coordinator_is_canonical_control_plane_lead(tmp_path: Path):
    env = {
        "TELEGRAM_OWNER_CHAT_ID": str(OWNER_ID),
        "STATE_DB_PATH": str(tmp_path / "coordinator-bridge.db"),
    }
    send, captured = _captured_send()
    bridge = build_bridge_from_env(env, send_callable=send)

    command_result = bridge.handle(
        _message(
            chat_id=OWNER_ID,
            user_id=OWNER_ID,
            message_id=1,
            text="/help",
        )
    )
    task_result = bridge.handle(
        _message(
            chat_id=OWNER_ID,
            user_id=OWNER_ID,
            message_id=2,
            text="coordinator default path",
        )
    )

    assert bridge.coordinator_role == COORDINATOR_ROLE
    assert bridge.coordinator_persona.agent_role == COORDINATOR_ROLE
    assert command_result.handled is True
    assert command_result.reason == "command"
    assert captured[0].text.startswith("Координатор:")
    assert task_result.handled is True
    assert task_result.reason == "task"
    assert captured[1].text.startswith("Координатор:")

    alias_send, alias_captured = _captured_send()
    alias_bridge = TelegramBridge(
        owner_chat_ids=frozenset({OWNER_ID}),
        send=alias_send,
        task_handler=lambda text, msg: BridgeReply(
            persona_role=COORDINATOR_ROLE,
            body=f"echo {text} via {msg.chat_id}",
        ),
        manager_role="pm_agent",
    )
    alias_bridge.handle(
        _message(
            chat_id=OWNER_ID,
            user_id=OWNER_ID,
            message_id=3,
            text="legacy manager alias",
        )
    )

    assert alias_bridge.coordinator_role == COORDINATOR_ROLE
    assert alias_bridge.manager_persona is alias_bridge.coordinator_persona
    assert alias_captured[0].text.startswith("Координатор:")


def test_bound_project_chat_enters_pipeline_through_coordinator_onboarding(
    tmp_path: Path,
    runner: BackgroundTaskRunner,
    sandbox: SandboxWorkspace,
):
    registry = ProjectRegistry(_make_db(tmp_path, "bound.db"))
    alpha_repo = _git_repo(tmp_path, "alpha-bound")
    registry.register_project(
        _project_snapshot(
            alpha_repo,
            chat_binding=_chat_binding(chat_id=-100123450901),
        )
    )
    memory = PipelineMemory()
    planning_prompts: list[str] = []

    def _planning_agent(task_prompt: str) -> str:
        planning_prompts.append(task_prompt)
        assert memory.get_artifact(task_id, "project_brief") is not None
        assert memory.get_artifact(task_id, "team_proposal") is not None
        assert memory.get_artifact(task_id, "owner_escalation") is None
        return '{"plan": "ok"}'

    def _agents(_tier):
        agents = _happy_agents()
        agents["planning_agent"] = _planning_agent
        return agents

    task_id = "task-coordinator-bound-001"
    harness = _build_real_harness(
        registry=registry,
        runner=runner,
        sandbox=sandbox,
        task_id=task_id,
        memory=memory,
        agent_registry_factory=_agents,
        active_chat_ids=(-100123450901,),
    )
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    with (
        patch("core.project_runtime_router._build_sandbox", return_value=sandbox),
        patch("core.real_task_handler.make_sandbox_hook", return_value=mock_hook_fn),
        patch.object(sandbox, "commit_in_worktree", return_value="abc123def456789"),
    ):
        result = harness.bridge.handle(
            _message(
                chat_id=-100123450901,
                user_id=999,
                message_id=1,
                text="Ship the deployment checker.",
            )
        )
        _wait_until_idle(runner)

    assert result.handled is True
    assert result.reason == "task"
    assert harness.task_calls == [
        {
            "text": "Ship the deployment checker.",
            "project_id": "alpha_project",
            "project_context_source": "bound_chat",
        }
    ]
    assert harness.bridge_captured
    assert harness.bridge_captured[0].text.startswith("Координатор:")
    assert "Ship the deployment checker." in harness.bridge_captured[0].text
    assert "Coordinator project captain onboarding" not in harness.bridge_captured[0].text

    assert planning_prompts
    assert "Coordinator project captain onboarding" in planning_prompts[0]
    assert "source: explicit project chat" in planning_prompts[0]
    assert "project_id: alpha_project" in planning_prompts[0]
    assert "Ship the deployment checker." in planning_prompts[0]

    project_brief = memory.get_artifact(task_id, "project_brief")
    team_proposal = memory.get_artifact(task_id, "team_proposal")
    assert project_brief is not None
    assert "Coordinator project brief" in project_brief
    assert "explicit project chat" in project_brief
    assert "alpha-project" in project_brief
    assert team_proposal is not None
    assert "Coordinator team proposal" in team_proposal
    assert "captain_role: coordinator_agent" in team_proposal
    assert "assembly_mode: baseline_internal_team" in team_proposal
    _assert_role_order(team_proposal)
    assert memory.get_artifact(task_id, "owner_escalation") is None


def test_owner_dm_single_project_fallback_gets_full_coordinator_contour(
    tmp_path: Path,
    runner: BackgroundTaskRunner,
    sandbox: SandboxWorkspace,
):
    registry = ProjectRegistry(_make_db(tmp_path, "owner-dm.db"))
    alpha_repo = _git_repo(tmp_path, "alpha-owner-dm")
    registry.register_project(_project_snapshot(alpha_repo))
    memory = PipelineMemory()
    planning_prompts: list[str] = []
    task_id = "task-coordinator-owner-dm-001"

    def _planning_agent(task_prompt: str) -> str:
        planning_prompts.append(task_prompt)
        return '{"plan": "ok"}'

    def _agents(_tier):
        agents = _happy_agents()
        agents["planning_agent"] = _planning_agent
        return agents

    harness = _build_real_harness(
        registry=registry,
        runner=runner,
        sandbox=sandbox,
        task_id=task_id,
        memory=memory,
        agent_registry_factory=_agents,
        active_chat_ids=(OWNER_ID,),
    )
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    with (
        patch("core.project_runtime_router._build_sandbox", return_value=sandbox),
        patch("core.real_task_handler.make_sandbox_hook", return_value=mock_hook_fn),
        patch.object(sandbox, "commit_in_worktree", return_value="abc123def456789"),
    ):
        result = harness.bridge.handle(
            _message(
                chat_id=OWNER_ID,
                user_id=OWNER_ID,
                message_id=1,
                text="Prepare the release branch.",
            )
        )
        _wait_until_idle(runner)

    assert result.handled is True
    assert result.reason == "task"
    assert harness.task_calls == [
        {
            "text": "Prepare the release branch.",
            "project_id": "alpha_project",
            "project_context_source": "owner_dm_single_project",
        }
    ]
    assert planning_prompts
    assert "Coordinator project captain onboarding" in planning_prompts[0]
    assert "source: owner DM fallback" in planning_prompts[0]
    assert "Prepare the release branch." in planning_prompts[0]

    project_brief = memory.get_artifact(task_id, "project_brief")
    team_proposal = memory.get_artifact(task_id, "team_proposal")
    assert project_brief is not None
    assert "owner DM fallback" in project_brief
    assert team_proposal is not None
    assert "owner DM fallback" in team_proposal
    assert "captain_role: coordinator_agent" in team_proposal
    assert memory.get_artifact(task_id, "owner_escalation") is None


def test_project_aware_fail_path_produces_owner_escalation(
    tmp_path: Path,
    runner: BackgroundTaskRunner,
    sandbox: SandboxWorkspace,
):
    registry = ProjectRegistry(_make_db(tmp_path, "fail.db"))
    alpha_repo = _git_repo(tmp_path, "alpha-fail")
    registry.register_project(
        _project_snapshot(
            alpha_repo,
            chat_binding=_chat_binding(chat_id=-100123450902),
        )
    )
    memory = PipelineMemory()
    task_id = "task-coordinator-fail-001"

    def _reviewer_boom(*_args):
        raise RuntimeError("kaboom")

    def _agents(_tier):
        agents = _happy_agents()
        agents["reviewer_agent"] = _reviewer_boom
        return agents

    harness = _build_real_harness(
        registry=registry,
        runner=runner,
        sandbox=sandbox,
        task_id=task_id,
        memory=memory,
        agent_registry_factory=_agents,
        active_chat_ids=(-100123450902,),
    )

    with patch("core.project_runtime_router._build_sandbox", return_value=sandbox):
        result = harness.bridge.handle(
            _message(
                chat_id=-100123450902,
                user_id=999,
                message_id=1,
                text="Implement the release workflow.",
            )
        )
        _wait_until_idle(runner)
        _wait_for(
            lambda: any("Не получилось" in text for _, text in harness.progress_captured)
        )

    assert result.handled is True
    assert result.reason == "task"
    escalation = memory.get_artifact(task_id, "owner_escalation")
    assert escalation is not None
    assert "escalation_type: system_failure" in escalation
    assert "final_state: FAIL" in escalation
    assert "agent_exception:RuntimeError:kaboom" in escalation
    final_text = next(
        text for _, text in harness.progress_captured if "Не получилось" in text
    )
    assert "Координатор: задача по проекту `alpha-project` завершилась" in final_text
    assert "внутренним pipeline/system сбоем" in final_text
    assert "reason  `agent_exception:RuntimeError:kaboom`" in final_text


def test_publish_failure_escalates_without_false_success(
    tmp_path: Path,
    runner: BackgroundTaskRunner,
    sandbox: SandboxWorkspace,
):
    registry = ProjectRegistry(_make_db(tmp_path, "publish-fail.db"))
    alpha_repo = _git_repo(tmp_path, "alpha-publish-fail")
    registry.register_project(
        _project_snapshot(
            alpha_repo,
            chat_binding=_chat_binding(chat_id=-100123450903),
        )
    )
    memory = PipelineMemory()
    task_id = "task-coordinator-publish-fail-001"
    harness = _build_real_harness(
        registry=registry,
        runner=runner,
        sandbox=sandbox,
        task_id=task_id,
        memory=memory,
        agent_registry_factory=lambda _tier: _happy_agents(),
        active_chat_ids=(-100123450903,),
    )
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
        result = harness.bridge.handle(
            _message(
                chat_id=-100123450903,
                user_id=999,
                message_id=1,
                text="Prepare the publish step.",
            )
        )
        _wait_until_idle(runner)
        _wait_for(
            lambda: any("Не получилось" in text for _, text in harness.progress_captured)
        )

    assert result.handled is True
    assert result.reason == "task"
    escalation = memory.get_artifact(task_id, "owner_escalation")
    assert escalation is not None
    assert "escalation_type: publish_failure" in escalation
    assert "commit_failed:SandboxError:nothing_to_commit" in escalation
    final_text = next(
        text for _, text in harness.progress_captured if "Не получилось" in text
    )
    all_progress_texts = [text for _, text in harness.progress_captured]
    assert "publish step" in final_text
    assert "commit_failed:SandboxError:nothing_to_commit" in final_text
    assert not any("✅ Готово" in text for text in all_progress_texts)


def test_agents_command_explains_current_assembled_team_truthfully(tmp_path: Path):
    bound_db = _make_db(tmp_path, "agents-bound.db")
    bound_registry = ProjectRegistry(bound_db)
    bound_repo = _git_repo(tmp_path, "agents-bound-alpha")
    bound_registry.register_project(
        _project_snapshot(
            bound_repo,
            chat_binding=_chat_binding(chat_id=-100123450904),
        )
    )
    bound_harness = _build_command_harness(bound_registry)

    dm_db = _make_db(tmp_path, "agents-dm.db")
    dm_registry = ProjectRegistry(dm_db)
    dm_repo = _git_repo(tmp_path, "agents-dm-alpha")
    dm_registry.register_project(_project_snapshot(dm_repo))
    dm_harness = _build_command_harness(dm_registry)

    unbound_db = _make_db(tmp_path, "agents-unbound.db")
    unbound_registry = ProjectRegistry(unbound_db)
    unbound_repo = _git_repo(tmp_path, "agents-unbound-alpha")
    unbound_registry.register_project(_project_snapshot(unbound_repo))
    unbound_harness = _build_command_harness(unbound_registry)

    multi_db = _make_db(tmp_path, "agents-multi.db")
    multi_registry = ProjectRegistry(multi_db)
    multi_alpha_repo = _git_repo(tmp_path, "agents-multi-alpha")
    multi_beta_repo = _git_repo(tmp_path, "agents-multi-beta")
    multi_registry.register_project(_project_snapshot(multi_alpha_repo))
    multi_registry.register_project(
        _project_snapshot(
            multi_beta_repo,
            project_id="beta_project",
            slug="beta-project",
            name="Beta Project",
        )
    )
    multi_harness = _build_command_harness(multi_registry)

    bound_harness.bridge.handle(
        _message(
            chat_id=-100123450904,
            user_id=999,
            message_id=1,
            text="/agents",
        )
    )
    dm_harness.bridge.handle(
        _message(
            chat_id=OWNER_ID,
            user_id=OWNER_ID,
            message_id=1,
            text="/agents",
        )
    )
    unbound_harness.bridge.handle(
        _message(
            chat_id=-100123450905,
            user_id=OWNER_ID,
            message_id=1,
            text="/agents",
        )
    )
    multi_harness.bridge.handle(
        _message(
            chat_id=OWNER_ID,
            user_id=OWNER_ID,
            message_id=1,
            text="/agents",
        )
    )

    bound_text = bound_harness.captured[0].text
    assert "Текущая assembled team" in bound_text
    assert "context_source: explicit project chat" in bound_text
    assert "captain_role: coordinator_agent" in bound_text
    _assert_role_order(bound_text)

    dm_text = dm_harness.captured[0].text
    assert "Текущая assembled team" in dm_text
    assert "context_source: owner DM fallback" in dm_text
    assert "captain_role: coordinator_agent" in dm_text

    unbound_text = unbound_harness.captured[0].text
    assert "не определена" in unbound_text.lower()
    assert "Baseline internal team template" in unbound_text
    assert "reference template" in unbound_text
    assert "/projects bind <project_id_or_slug>" in unbound_text

    multi_text = multi_harness.captured[0].text
    assert "не определена" in multi_text.lower()
    assert "explicit project chat" in multi_text
    assert "Baseline internal team template" in multi_text
    assert "Текущая assembled team" not in multi_text


def test_agents_command_does_not_change_routing_or_create_hidden_state(
    tmp_path: Path,
):
    bound_db = _make_db(tmp_path, "agents-routing-bound.db")
    bound_registry = ProjectRegistry(bound_db)
    bound_repo = _git_repo(tmp_path, "agents-routing-alpha")
    bound_registry.register_project(
        _project_snapshot(
            bound_repo,
            chat_binding=_chat_binding(chat_id=-100123450906),
        )
    )
    bound_harness = _build_command_harness(bound_registry)

    before_task = bound_harness.bridge.handle(
        _message(
            chat_id=-100123450906,
            user_id=999,
            message_id=1,
            text="run alpha task after agents",
        )
    )
    agents = bound_harness.bridge.handle(
        _message(
            chat_id=-100123450906,
            user_id=999,
            message_id=2,
            text="/agents",
        )
    )
    after_task = bound_harness.bridge.handle(
        _message(
            chat_id=-100123450906,
            user_id=999,
            message_id=3,
            text="run alpha task after agents again",
        )
    )

    multi_db = _make_db(tmp_path, "agents-routing-multi.db")
    multi_registry = ProjectRegistry(multi_db)
    multi_alpha_repo = _git_repo(tmp_path, "agents-routing-multi-alpha")
    multi_beta_repo = _git_repo(tmp_path, "agents-routing-multi-beta")
    multi_registry.register_project(_project_snapshot(multi_alpha_repo))
    multi_registry.register_project(
        _project_snapshot(
            multi_beta_repo,
            project_id="beta_project",
            slug="beta-project",
            name="Beta Project",
        )
    )
    multi_harness = _build_command_harness(multi_registry)
    blocked_before = multi_harness.bridge.handle(
        _message(
            chat_id=OWNER_ID,
            user_id=OWNER_ID,
            message_id=1,
            text="should block before agents",
        )
    )
    unresolved_agents = multi_harness.bridge.handle(
        _message(
            chat_id=OWNER_ID,
            user_id=OWNER_ID,
            message_id=2,
            text="/agents",
        )
    )
    blocked_after = multi_harness.bridge.handle(
        _message(
            chat_id=OWNER_ID,
            user_id=OWNER_ID,
            message_id=3,
            text="should still block after agents",
        )
    )

    assert before_task.handled is True
    assert before_task.reason == "task"
    assert agents.handled is True
    assert agents.reason == "command"
    assert after_task.handled is True
    assert after_task.reason == "task"
    assert bound_harness.task_calls == [
        {
            "text": "run alpha task after agents",
            "project_id": "alpha_project",
            "project_context_source": "bound_chat",
            "resolved_project_id": "alpha_project",
        },
        {
            "text": "run alpha task after agents again",
            "project_id": "alpha_project",
            "project_context_source": "bound_chat",
            "resolved_project_id": "alpha_project",
        },
    ]

    assert blocked_before.handled is False
    assert blocked_before.reason == "project_context_missing"
    assert unresolved_agents.handled is True
    assert unresolved_agents.reason == "command"
    assert blocked_after.handled is False
    assert blocked_after.reason == "project_context_missing"
    assert multi_harness.task_calls == []
    assert "explicit project chat" in multi_harness.captured[1].text
    assert "нужен явный" in multi_harness.captured[0].text.lower()
    assert "проектный чат" in multi_harness.captured[0].text.lower()
    assert "нужен явный" in multi_harness.captured[2].text.lower()
    assert "проектный чат" in multi_harness.captured[2].text.lower()


def test_coordinator_artifacts_remain_project_consistent(
    tmp_path: Path,
    runner: BackgroundTaskRunner,
    sandbox: SandboxWorkspace,
):
    registry = ProjectRegistry(_make_db(tmp_path, "consistency.db"))
    alpha_repo = _git_repo(tmp_path, "alpha-consistency")
    registry.register_project(
        _project_snapshot(
            alpha_repo,
            chat_binding=_chat_binding(chat_id=-100123450907),
        )
    )
    memory = PipelineMemory()
    task_id = "task-coordinator-consistency-001"
    planning_prompts: list[str] = []

    def _planning_agent(task_prompt: str) -> str:
        planning_prompts.append(task_prompt)
        return '{"plan": "ok"}'

    def _reviewer_boom(*_args):
        raise RuntimeError("kaboom")

    def _agents(_tier):
        agents = _happy_agents()
        agents["planning_agent"] = _planning_agent
        agents["reviewer_agent"] = _reviewer_boom
        return agents

    harness = _build_real_harness(
        registry=registry,
        runner=runner,
        sandbox=sandbox,
        task_id=task_id,
        memory=memory,
        agent_registry_factory=_agents,
        active_chat_ids=(-100123450907,),
    )

    with patch("core.project_runtime_router._build_sandbox", return_value=sandbox):
        harness.bridge.handle(
            _message(
                chat_id=-100123450907,
                user_id=999,
                message_id=1,
                text="Prepare consistent coordinator artifacts.",
            )
        )
        _wait_until_idle(runner)

    prompt = planning_prompts[0]
    project_brief = memory.get_artifact(task_id, "project_brief")
    team_proposal = memory.get_artifact(task_id, "team_proposal")
    owner_escalation = memory.get_artifact(task_id, "owner_escalation")
    assert project_brief is not None
    assert team_proposal is not None
    assert owner_escalation is not None

    for text in (prompt, project_brief, team_proposal, owner_escalation):
        assert "alpha_project" in text
        assert "alpha-project" in text
        assert "explicit project chat" in text

    assert "Coordinator role: project captain" in prompt
    assert "Coordinator mandate:" in project_brief
    assert "captain_role: coordinator_agent" in team_proposal
    assert "Coordinator owner escalation" in owner_escalation
