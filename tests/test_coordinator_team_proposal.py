from pathlib import Path

from core.agent_personas import default_registry
from core.coordinator_team_assembly import (
    BASELINE_INTERNAL_TEAM_ROLE_ORDER,
    CoordinatorTeamAssemblyContext,
    CoordinatorTeamAssemblyService,
)
from core.coordinator_team_proposal import CoordinatorTeamProposalService
from core.project_models import Project, ProjectChatBinding, ProjectPolicy
from core.project_registry import ProjectSnapshot
from core.project_runtime import ProjectRuntimeBinding
from core.project_team_state import ProjectSpecialistRoster
from core.specialization_hints import SpecializationHint, SpecializationHints


def _git_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


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
        "worktree_root": repo_path.parent / "worktrees",
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


def _snapshot(
    repo_path: Path | None,
    *,
    chat_binding: ProjectChatBinding | None = None,
    **overrides,
) -> ProjectSnapshot:
    data = {
        "project": _project(),
        "policy": _policy(),
        "chat_binding": chat_binding,
    }
    if repo_path is not None:
        data["runtime_binding"] = _runtime_binding(repo_path)
    data.update(overrides)
    return ProjectSnapshot(**data)


def _hints() -> SpecializationHints:
    return SpecializationHints(
        (
            SpecializationHint(
                "devops_agent",
                "Task depends on deploy and rollback safety.",
            ),
            SpecializationHint(
                "security_agent",
                "Task touches auth and secrets.",
            ),
        )
    )


def _roster(*roles: str) -> ProjectSpecialistRoster:
    return ProjectSpecialistRoster(
        project_id="alpha_project",
        specialist_roles=tuple(roles),
    )


def _assembly(
    tmp_path: Path,
    *,
    context_source: str,
    bound: bool,
    project_specialist_roster: ProjectSpecialistRoster | None = None,
    specialization_hints: SpecializationHints | None = None,
):
    repo = _git_repo(tmp_path)
    snapshot = _snapshot(repo, chat_binding=_chat_binding() if bound else None)
    return CoordinatorTeamAssemblyService().assemble_team(
        CoordinatorTeamAssemblyContext(
            snapshot=snapshot,
            owner_task_text="Implement the release workflow.",
            context_source=context_source,
            personas=default_registry(),
            project_specialist_roster=(
                project_specialist_roster
                if project_specialist_roster is not None
                else _roster()
            ),
            specialization_hints=(
                specialization_hints
                if specialization_hints is not None
                else SpecializationHints.empty()
            ),
        )
    )


def test_team_proposal_artifact_includes_project_anchor_context_and_captain(
    tmp_path,
):
    assembly = _assembly(tmp_path, context_source="bound_chat", bound=True)

    artifact = CoordinatorTeamProposalService().build_team_proposal_artifact(
        assembly
    )

    assert "Coordinator team proposal" in artifact
    assert "alpha_project" in artifact
    assert "alpha-project" in artifact
    assert "Alpha Project" in artifact
    assert "explicit project chat" in artifact
    assert "project captain" in artifact.lower()
    assert "control-plane lead" in artifact.lower()
    assert "Implement the release workflow." in artifact
    assert "Hiring and external roles are not auto-activated" in artifact
    assert "Project specialists:" in artifact
    assert "- none" in artifact
    assert "Specialization hints:" in artifact


def test_team_proposal_builds_from_assembled_team_in_stable_order(tmp_path):
    assembly = _assembly(
        tmp_path,
        context_source="owner_dm_single_project",
        bound=False,
    )

    artifact = CoordinatorTeamProposalService().build_team_proposal_artifact(
        assembly
    )

    assert "assembly_mode: baseline_internal_team" in artifact
    last_index = -1
    member_roles = tuple(member.persona.agent_role for member in assembly.members)
    assert member_roles == BASELINE_INTERNAL_TEAM_ROLE_ORDER
    for member in assembly.members:
        role_index = artifact.index(f"- role_id: {member.persona.agent_role}")
        assert role_index > last_index
        last_index = role_index
        assert f"  human_name: {member.persona.human_name}" in artifact
        assert f"  title: {member.persona.title}" in artifact
        assert f"  seniority: {member.persona.seniority}" in artifact
        assert f"  mandate: {member.mandate}" in artifact


def test_team_proposal_is_deterministic(tmp_path):
    assembly = _assembly(
        tmp_path,
        context_source="owner_dm_single_project",
        bound=False,
    )
    service = CoordinatorTeamProposalService()

    first = service.build_team_proposal_artifact(assembly)
    second = service.build_team_proposal_artifact(assembly)

    assert first == second


def test_team_proposal_renders_non_empty_hints_in_stable_order(tmp_path):
    assembly = _assembly(
        tmp_path,
        context_source="bound_chat",
        bound=True,
        specialization_hints=_hints(),
    )

    artifact = CoordinatorTeamProposalService().build_team_proposal_artifact(
        assembly
    )

    assert "Specialization hints:" in artifact
    security_index = artifact.index("- specialist_role: security_agent")
    devops_index = artifact.index("- specialist_role: devops_agent")
    assert security_index < devops_index
    assert "Task touches auth and secrets." in artifact
    assert "Task depends on deploy and rollback safety." in artifact


def test_team_proposal_renders_non_empty_project_specialists_in_stable_order(
    tmp_path,
):
    assembly = _assembly(
        tmp_path,
        context_source="bound_chat",
        bound=True,
        project_specialist_roster=_roster("data_agent", "security_agent"),
    )

    artifact = CoordinatorTeamProposalService().build_team_proposal_artifact(
        assembly
    )

    assert "Project specialists:" in artifact
    security_index = artifact.index("- role_id: security_agent")
    data_index = artifact.index("- role_id: data_agent")
    hints_index = artifact.index("Specialization hints:")
    assert security_index < data_index < hints_index
