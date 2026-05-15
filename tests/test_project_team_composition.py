from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.agent_personas import default_registry
from core.background_runner import BackgroundTaskRunner
from core.bot_runner import build_command_registry
from core.coordinator_team_assembly import (
    CoordinatorTeamAssemblyContext,
    CoordinatorTeamAssemblyService,
)
from core.coordinator_team_proposal import CoordinatorTeamProposalService
from core.hire_approval import (
    HireApprovalDecision,
    HireApprovalService,
)
from core.logical_hiring import LogicalHiringService
from core.model_tier import default_registry as default_tier_registry
from core.project_context import ProjectContextResolver
from core.project_models import Project, ProjectChatBinding, ProjectPolicy
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.project_runtime import ProjectRuntimeBinding
from core.project_runtime_router import ProjectRuntimeRouter
from core.project_team_commands import (
    ProjectTeamCommand,
    ProjectTeamCommandContext,
    ProjectTeamCommandService,
)
from core.quality_gates import CheckResult
from core.real_task_handler import make_real_task_handler
from core.runtime_validator import ValidationReport, ValidationStrategy
from core.sandbox_workspace import (
    SandboxConfig,
    SandboxWorkspace,
    _RunResult,
    _SubprocessRunner,
)
from core.specialization_hints import SpecializationHint, SpecializationHints
from core.state_db import StateDB
from core.telegram_bridge import (
    BridgeReply,
    IncomingMessage,
    OutgoingEnvelope,
    TelegramBridge,
)
from core.tier_session import TierSessionStore


class FakeGitRunner(_SubprocessRunner):
    def run(self, cmd, cwd, env, timeout):
        return _RunResult(returncode=0, stdout="", stderr="")


@pytest.fixture
def runner():
    instance = BackgroundTaskRunner()
    yield instance
    instance.shutdown()


@pytest.fixture
def tier_store() -> TierSessionStore:
    return TierSessionStore(default_tier_registry())


def _git_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


def _make_sandbox(tmp_path: Path, repo_path: Path) -> SandboxWorkspace:
    return SandboxWorkspace(
        SandboxConfig(
            main_repo_path=repo_path,
            worktree_root=tmp_path / f"{repo_path.name}-worktrees",
        ),
        runner=FakeGitRunner(),
    )


def _project(
    project_id: str,
    slug: str,
    *,
    owner_user_id: int = 777,
    name: str | None = None,
) -> Project:
    return Project(
        project_id=project_id,
        slug=slug,
        name=name or slug.replace("-", " ").title(),
        description=f"Project {slug}.",
        owner_user_id=owner_user_id,
        status="active",
    )


def _policy(
    project_id: str,
    *,
    allow_hiring: bool = True,
    require_owner_approval_for_hires: bool = True,
) -> ProjectPolicy:
    return ProjectPolicy(
        project_id=project_id,
        allow_hiring=allow_hiring,
        allow_agent_dm=False,
        require_owner_approval_for_hires=require_owner_approval_for_hires,
    )


def _chat_binding(project_id: str, chat_id: int) -> ProjectChatBinding:
    return ProjectChatBinding(
        project_id=project_id,
        chat_provider="telegram",
        chat_id=chat_id,
    )


def _runtime_binding(project_id: str, repo_path: Path) -> ProjectRuntimeBinding:
    return ProjectRuntimeBinding(
        project_id=project_id,
        adapter_name=f"{project_id}_adapter",
        repo_path=repo_path,
        worktree_root=repo_path.parent / f"{project_id}-project-worktrees",
        base_branch="main",
        branch_prefix="feature/",
        language="python",
        rules=(),
        commands=(),
        forbidden_paths=(),
        forbidden_tokens=(),
    )


def _snapshot(
    project_id: str,
    slug: str,
    repo_path: Path,
    *,
    owner_user_id: int = 777,
    chat_id: int | None = None,
    allow_hiring: bool = True,
    require_owner_approval_for_hires: bool = True,
) -> ProjectSnapshot:
    return ProjectSnapshot(
        project=_project(
            project_id,
            slug,
            owner_user_id=owner_user_id,
        ),
        policy=_policy(
            project_id,
            allow_hiring=allow_hiring,
            require_owner_approval_for_hires=require_owner_approval_for_hires,
        ),
        chat_binding=(
            _chat_binding(project_id, chat_id)
            if chat_id is not None
            else None
        ),
        runtime_binding=_runtime_binding(project_id, repo_path),
    )


def _hints(*items: tuple[str, str]) -> SpecializationHints:
    return SpecializationHints(
        tuple(
            SpecializationHint(
                specialist_role=role,
                reason=reason,
            )
            for role, reason in items
        )
    )


def _proposal_for(
    registry: ProjectRegistry,
    project_id: str,
    *,
    owner_task_text: str = "Implement the current project task.",
) -> str:
    snapshot = registry.get_project_snapshot(project_id)
    assert snapshot is not None
    assembly = CoordinatorTeamAssemblyService().assemble_team(
        CoordinatorTeamAssemblyContext(
            snapshot=snapshot,
            owner_task_text=owner_task_text,
            context_source=(
                "bound_chat"
                if snapshot.chat_binding is not None
                else "owner_dm_single_project"
            ),
            personas=default_registry(),
            project_specialist_roster=registry.get_project_specialist_roster(
                project_id
            ),
            specialization_hints=SpecializationHints.empty(),
        )
    )
    return CoordinatorTeamProposalService().build_team_proposal_artifact(
        assembly
    )


def _captured_send():
    captured = []

    def _send(out):
        captured.append(out)

    return _send, captured


def _make_bridge(
    registry: ProjectRegistry,
    *,
    owner_user_id: int = 777,
) -> tuple[TelegramBridge, list]:
    resolver = ProjectContextResolver(registry, (owner_user_id,))
    commands = build_command_registry(
        default_registry(),
        project_context_resolver=resolver,
    )
    send, captured = _captured_send()
    bridge = TelegramBridge(
        owner_chat_ids=frozenset({owner_user_id}),
        send=send,
        commands=commands,
        task_handler=lambda text, msg: BridgeReply(
            persona_role="architect_agent",
            body="task ok",
        ),
        project_context_resolver=resolver,
    )
    return bridge, captured


def _team_message(
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


def _make_progress_envelope_capture():
    captured: list[OutgoingEnvelope] = []
    lock = threading.Lock()

    def _send(envelope: OutgoingEnvelope) -> None:
        with lock:
            captured.append(envelope)

    return _send, captured


def _wait_until_idle(runner: BackgroundTaskRunner, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while runner.is_busy() and time.time() < deadline:
        time.sleep(0.02)


def _wait_for_envelope(captured, predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate(captured):
            return
        time.sleep(0.02)
    raise AssertionError("timed_out_waiting_for_envelope")


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


def test_project_team_composition_isolated_pending_approved_proposal_and_restart(
    tmp_path: Path,
):
    db_path = tmp_path / "team-composition-isolation.db"
    registry = ProjectRegistry(StateDB(db_path))
    alpha_repo = _git_repo(tmp_path, "alpha-repo")
    beta_repo = _git_repo(tmp_path, "beta-repo")
    alpha_snapshot = _snapshot(
        "alpha_project",
        "alpha-project",
        alpha_repo,
        chat_id=-1002001,
        require_owner_approval_for_hires=True,
    )
    beta_snapshot = _snapshot(
        "beta_project",
        "beta-project",
        beta_repo,
        chat_id=-1002002,
        require_owner_approval_for_hires=True,
    )
    registry.register_project(alpha_snapshot)
    registry.register_project(beta_snapshot)
    approval_service = HireApprovalService(registry)

    pending_result = approval_service.request_sensitive_hire(
        alpha_snapshot,
        "security_agent",
        "Auth and secrets are in scope.",
        "logical_hiring_pm_hint",
    )

    assert pending_result.status == "pending_created"
    assert registry.get_project_specialist_roster("alpha_project").specialist_roles == ()
    assert registry.get_project_specialist_roster("beta_project").specialist_roles == ()
    assert len(registry.list_pending_hire_requests("alpha_project")) == 1
    assert registry.list_pending_hire_requests("beta_project") == ()

    cross_project_result = approval_service.apply_decision(
        beta_snapshot,
        HireApprovalDecision(
            request_id=pending_result.request_id,
            decision="approve",
            actor_user_id=777,
        ),
    )
    assert cross_project_result.status == "not_found"
    assert registry.get_project_specialist_roster("beta_project").specialist_roles == ()
    assert len(registry.list_pending_hire_requests("alpha_project")) == 1

    alpha_pending_proposal = _proposal_for(registry, "alpha_project")
    beta_pending_proposal = _proposal_for(registry, "beta_project")
    assert "Project specialists:" in alpha_pending_proposal
    assert "Project specialists:" in beta_pending_proposal
    assert pending_result.request_id not in alpha_pending_proposal
    assert "- role_id: security_agent" not in alpha_pending_proposal
    assert "- role_id: security_agent" not in beta_pending_proposal

    approved_result = approval_service.apply_decision(
        alpha_snapshot,
        HireApprovalDecision(
            request_id=pending_result.request_id,
            decision="approve",
            actor_user_id=777,
        ),
    )

    assert approved_result.status == "approved"
    assert registry.get_project_specialist_roster("alpha_project").specialist_roles == (
        "security_agent",
    )
    assert registry.list_pending_hire_requests("alpha_project") == ()
    assert registry.get_project_specialist_roster("beta_project").specialist_roles == ()

    alpha_approved_proposal = _proposal_for(registry, "alpha_project")
    beta_approved_proposal = _proposal_for(registry, "beta_project")
    assert "- role_id: security_agent" in alpha_approved_proposal
    assert "- role_id: security_agent" not in beta_approved_proposal

    reopened = ProjectRegistry(StateDB(db_path))
    assert reopened.get_project_specialist_roster("alpha_project").specialist_roles == (
        "security_agent",
    )
    assert reopened.list_pending_hire_requests("alpha_project") == ()
    assert reopened.get_project_specialist_roster("beta_project").specialist_roles == ()
    assert "- role_id: security_agent" in _proposal_for(reopened, "alpha_project")
    assert "- role_id: security_agent" not in _proposal_for(reopened, "beta_project")


def test_project_team_composition_command_surface_reconciles_pending_and_remove_clears(
    tmp_path: Path,
):
    registry = ProjectRegistry(StateDB(tmp_path / "team-composition-commands.db"))
    repo = _git_repo(tmp_path, "alpha-command-repo")
    snapshot = _snapshot(
        "alpha_project",
        "alpha-project",
        repo,
        chat_id=-1002101,
        require_owner_approval_for_hires=True,
    )
    registry.register_project(snapshot)
    approval_service = HireApprovalService(registry)

    first_pending = approval_service.request_sensitive_hire(
        snapshot,
        "security_agent",
        "Auth and secrets are in scope.",
        "logical_hiring_pm_hint",
    )
    second_pending = approval_service.request_sensitive_hire(
        snapshot,
        "security_agent",
        "Auth and secrets are in scope.",
        "logical_hiring_pm_hint",
    )
    assert first_pending.request_id == second_pending.request_id
    assert second_pending.status == "pending_exists"
    assert len(registry.list_pending_hire_requests("alpha_project")) == 1

    bridge, captured = _make_bridge(registry)

    pending_result = bridge.handle(
        _team_message(
            chat_id=-1002101,
            user_id=777,
            message_id=1,
            text="/team pending",
        )
    )
    assert pending_result.reason == "command"
    assert first_pending.request_id in captured[-1].text

    add_result = bridge.handle(
        _team_message(
            chat_id=-1002101,
            user_id=777,
            message_id=2,
            text="/team add security_agent",
        )
    )
    assert add_result.reason == "command"
    assert registry.get_project_specialist_roster("alpha_project").specialist_roles == (
        "security_agent",
    )
    assert registry.list_pending_hire_requests("alpha_project") == ()
    assert "marked approved" in captured[-1].text

    list_result = bridge.handle(
        _team_message(
            chat_id=-1002101,
            user_id=777,
            message_id=3,
            text="/team list",
        )
    )
    assert list_result.reason == "command"
    assert "security_agent" in captured[-1].text

    pending_after_result = bridge.handle(
        _team_message(
            chat_id=-1002101,
            user_id=777,
            message_id=4,
            text="/team pending",
        )
    )
    assert pending_after_result.reason == "command"
    assert "Pending hire requests:" in captured[-1].text
    assert captured[-1].text.rstrip().endswith("- none")

    remove_result = bridge.handle(
        _team_message(
            chat_id=-1002101,
            user_id=777,
            message_id=5,
            text="/team remove security_agent",
        )
    )
    assert remove_result.reason == "command"
    assert registry.get_project_specialist_roster("alpha_project").specialist_roles == ()

    final_list_result = bridge.handle(
        _team_message(
            chat_id=-1002101,
            user_id=777,
            message_id=6,
            text="/team list",
        )
    )
    assert final_list_result.reason == "command"
    assert "Project specialists:" in captured[-1].text
    assert captured[-1].text.rstrip().endswith("- none")

    rejected_pending = approval_service.request_sensitive_hire(
        snapshot,
        "devops_agent",
        "Deployability is in scope.",
        "logical_hiring_pm_hint",
    )
    reject_result = bridge.handle(
        _team_message(
            chat_id=-1002101,
            user_id=777,
            message_id=7,
            text=f"/team reject {rejected_pending.request_id}",
        )
    )
    assert reject_result.reason == "command"
    assert registry.get_project_specialist_roster("alpha_project").specialist_roles == ()
    assert registry.list_pending_hire_requests("alpha_project") == ()
    assert "отклон" in captured[-1].text.lower()


def test_project_team_composition_persisted_policy_blocks_stale_logical_and_direct_mutations(
    tmp_path: Path,
):
    registry = ProjectRegistry(StateDB(tmp_path / "team-composition-policy.db"))
    alpha_repo = _git_repo(tmp_path, "alpha-policy-repo")
    beta_repo = _git_repo(tmp_path, "beta-policy-repo")
    alpha_snapshot = _snapshot(
        "alpha_project",
        "alpha-project",
        alpha_repo,
        chat_id=-1002201,
        allow_hiring=True,
        require_owner_approval_for_hires=False,
    )
    beta_snapshot = _snapshot(
        "beta_project",
        "beta-project",
        beta_repo,
        chat_id=-1002202,
        allow_hiring=True,
        require_owner_approval_for_hires=False,
    )
    registry.register_project(alpha_snapshot)
    registry.register_project(beta_snapshot)
    registry.add_project_specialist("beta_project", "security_agent")
    registry.set_project_policy(
        _policy(
            "alpha_project",
            allow_hiring=False,
            require_owner_approval_for_hires=False,
        )
    )
    registry.set_project_policy(
        _policy(
            "beta_project",
            allow_hiring=False,
            require_owner_approval_for_hires=False,
        )
    )

    logical_result = LogicalHiringService(registry).run_from_hints(
        alpha_snapshot,
        _hints(("security_agent", "Auth and secrets are in scope.")),
    )
    assert logical_result.status == "blocked_by_policy"
    assert registry.get_project_specialist_roster("alpha_project").specialist_roles == ()
    assert registry.list_pending_hire_requests("alpha_project") == ()

    command_service = ProjectTeamCommandService(registry)
    with pytest.raises(
        ValueError,
        match="project_team_mutation_disallowed_by_policy",
    ):
        command_service.handle(
            ProjectTeamCommand(action="add", specialist_role="security_agent"),
            ProjectTeamCommandContext(
                snapshot=alpha_snapshot,
                actor_user_id=777,
                context_source="bound_chat",
            ),
        )
    assert registry.get_project_specialist_roster("alpha_project").specialist_roles == ()

    with pytest.raises(
        ValueError,
        match="project_team_mutation_disallowed_by_policy",
    ):
        command_service.handle(
            ProjectTeamCommand(action="remove", specialist_role="security_agent"),
            ProjectTeamCommandContext(
                snapshot=beta_snapshot,
                actor_user_id=777,
                context_source="bound_chat",
            ),
        )
    assert registry.get_project_specialist_roster("beta_project").specialist_roles == (
        "security_agent",
    )


def test_project_team_composition_runtime_pending_then_approve_after_restart(
    tmp_path: Path,
    runner: BackgroundTaskRunner,
    tier_store: TierSessionStore,
):
    repo = _git_repo(tmp_path, "runtime-alpha-repo")
    sandbox = _make_sandbox(tmp_path, repo)
    tier_store.set_active(-1002301, "STANDARD")
    snapshot = _snapshot(
        "alpha_project",
        "alpha-project",
        repo,
        chat_id=-1002301,
        require_owner_approval_for_hires=True,
    )
    runtime_router = _runtime_router_for_snapshot(
        tmp_path,
        snapshot,
        db_name="team-composition-runtime.db",
    )
    send_envelope, captured = _make_progress_envelope_capture()
    mock_report = _ok_validation_report()
    mock_hook_fn = MagicMock(return_value=mock_report)

    def _agents(_tier):
        return {
            "planning_agent": lambda *_a: '{"plan":"ok"}',
            "pm_agent": lambda *_a: (
                '{"tasks":[],"specialization_hints":'
                '[{"specialist_role":"security_agent","reason":"Auth и secrets в scope."}]}'
            ),
            "architect_agent": lambda *_a: '{"arch":"spec"}',
            "writer_agent": lambda *_a: "def f(): return 42",
            "reviewer_agent": lambda *_a: '{"verdict":"APPROVED"}',
            "tester_agent": lambda *_a: "tests pass",
            "qa_agent": lambda *_a: '{"verdict":"PASS"}',
            "fixer_agent": lambda *_a: "def f(): return 42",
        }

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
            agent_registry_factory=_agents,
            task_id_factory=lambda: "task-42",
        )
        reply = handler(
            "Собери безопасный API для billing.",
            IncomingMessage(
                chat_id=-1002301,
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
        _wait_for_envelope(
            captured,
            lambda envelopes: any("Готово" in env.message.text for env in envelopes),
        )

    projection_texts = [env.message.text for env in captured]
    assert any("owner approval" in text for text in projection_texts)
    assert any("roster пока не изменён" in text for text in projection_texts)
    assert runtime_router.registry.get_project_specialist_roster(
        "alpha_project"
    ).specialist_roles == ()

    reopened = ProjectRegistry(StateDB(runtime_router.registry.state_db.path))
    pending = reopened.list_pending_hire_requests("alpha_project")
    assert len(pending) == 1
    request_id = pending[0].request_id

    bridge, command_captured = _make_bridge(reopened)
    pending_result = bridge.handle(
        _team_message(
            chat_id=-1002301,
            user_id=777,
            message_id=2,
            text="/team pending",
        )
    )
    assert pending_result.reason == "command"
    assert request_id in command_captured[-1].text

    approve_result = bridge.handle(
        _team_message(
            chat_id=-1002301,
            user_id=777,
            message_id=3,
            text=f"/team approve {request_id}",
        )
    )
    assert approve_result.reason == "command"
    assert reopened.get_project_specialist_roster("alpha_project").specialist_roles == (
        "security_agent",
    )

    list_result = bridge.handle(
        _team_message(
            chat_id=-1002301,
            user_id=777,
            message_id=4,
            text="/team list",
        )
    )
    assert list_result.reason == "command"
    assert "security_agent" in command_captured[-1].text

    reopened_again = ProjectRegistry(StateDB(runtime_router.registry.state_db.path))
    bridge_after_restart, restart_captured = _make_bridge(reopened_again)
    restart_list_result = bridge_after_restart.handle(
        _team_message(
            chat_id=-1002301,
            user_id=777,
            message_id=5,
            text="/team list",
        )
    )
    assert restart_list_result.reason == "command"
    assert reopened_again.get_project_specialist_roster(
        "alpha_project"
    ).specialist_roles == ("security_agent",)
    assert reopened_again.list_pending_hire_requests("alpha_project") == ()
    assert "security_agent" in restart_captured[-1].text


def test_project_team_composition_command_surface_does_not_guess_project_context(
    tmp_path: Path,
):
    registry = ProjectRegistry(StateDB(tmp_path / "team-composition-context.db"))
    alpha_repo = _git_repo(tmp_path, "alpha-context-repo")
    beta_repo = _git_repo(tmp_path, "beta-context-repo")
    registry.register_project(
        _snapshot(
            "alpha_project",
            "alpha-project",
            alpha_repo,
            chat_id=-1002401,
        )
    )
    registry.register_project(
        _snapshot(
            "beta_project",
            "beta-project",
            beta_repo,
            chat_id=-1002402,
        )
    )

    bridge, captured = _make_bridge(registry)

    owner_dm_result = bridge.handle(
        _team_message(
            chat_id=777,
            user_id=777,
            message_id=1,
            text="/team list",
        )
    )
    assert owner_dm_result.reason == "command"
    assert "не выбирает проект автоматически" in captured[-1].text.lower()

    unbound_chat_result = bridge.handle(
        _team_message(
            chat_id=-1002499,
            user_id=777,
            message_id=2,
            text="/team list",
        )
    )
    assert unbound_chat_result.reason == "command"
    assert "не привязан" in captured[-1].text.lower()
