from pathlib import Path

import pytest

from core.agent_personas import DEFAULT_PERSONAS, PersonaRegistry, default_registry
from core.coordinator_team_proposal import (
    BASELINE_TEAM_ROLE_ORDER,
    CoordinatorTeamProposalContext,
    CoordinatorTeamProposalService,
)
from core.project_models import Project, ProjectChatBinding, ProjectPolicy
from core.project_registry import ProjectSnapshot
from core.project_runtime import ProjectRuntimeBinding


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


def _registry_without(role: str) -> PersonaRegistry:
    return PersonaRegistry(
        tuple(persona for persona in DEFAULT_PERSONAS if persona.agent_role != role)
    )


def test_context_happy_path_for_bound_chat(tmp_path):
    repo = _git_repo(tmp_path)
    context = CoordinatorTeamProposalContext(
        snapshot=_snapshot(repo, chat_binding=_chat_binding()),
        owner_task_text="Implement the release workflow.",
        context_source="bound_chat",
        personas=default_registry(),
    )

    assert context.context_source == "bound_chat"


def test_context_happy_path_for_owner_dm_fallback(tmp_path):
    repo = _git_repo(tmp_path)
    context = CoordinatorTeamProposalContext(
        snapshot=_snapshot(repo),
        owner_task_text="Prepare the release branch.",
        context_source="owner_dm_single_project",
        personas=default_registry(),
    )

    assert context.context_source == "owner_dm_single_project"


def test_context_rejects_bad_snapshot():
    with pytest.raises(ValueError, match="invalid_project_snapshot_type"):
        CoordinatorTeamProposalContext(
            snapshot="bad",  # type: ignore[arg-type]
            owner_task_text="Task",
            context_source="bound_chat",
            personas=default_registry(),
        )


def test_context_rejects_snapshot_without_runtime_binding(tmp_path):
    with pytest.raises(ValueError, match="snapshot_missing_runtime_binding"):
        CoordinatorTeamProposalContext(
            snapshot=_snapshot(None),
            owner_task_text="Task",
            context_source="bound_chat",
            personas=default_registry(),
        )


@pytest.mark.parametrize("bad", ["", "   "])
def test_context_rejects_empty_owner_task_text(tmp_path, bad):
    repo = _git_repo(tmp_path)

    with pytest.raises(ValueError, match="empty_owner_task_text"):
        CoordinatorTeamProposalContext(
            snapshot=_snapshot(repo),
            owner_task_text=bad,
            context_source="bound_chat",
            personas=default_registry(),
        )


@pytest.mark.parametrize("bad", ["none", "registry", "", "  "])
def test_context_rejects_bad_context_source(tmp_path, bad):
    repo = _git_repo(tmp_path)

    with pytest.raises(ValueError, match="invalid_context_source"):
        CoordinatorTeamProposalContext(
            snapshot=_snapshot(repo),
            owner_task_text="Task",
            context_source=bad,
            personas=default_registry(),
        )


def test_context_rejects_bad_personas_type(tmp_path):
    repo = _git_repo(tmp_path)

    with pytest.raises(ValueError, match="invalid_persona_registry_type"):
        CoordinatorTeamProposalContext(
            snapshot=_snapshot(repo),
            owner_task_text="Task",
            context_source="bound_chat",
            personas="bad",  # type: ignore[arg-type]
        )


def test_context_rejects_missing_required_roles(tmp_path):
    repo = _git_repo(tmp_path)

    with pytest.raises(ValueError, match="missing_required_persona_roles"):
        CoordinatorTeamProposalContext(
            snapshot=_snapshot(repo),
            owner_task_text="Task",
            context_source="bound_chat",
            personas=_registry_without("fixer_agent"),
        )


def test_team_proposal_artifact_includes_project_anchor_context_and_captain(
    tmp_path,
):
    repo = _git_repo(tmp_path)
    context = CoordinatorTeamProposalContext(
        snapshot=_snapshot(repo, chat_binding=_chat_binding()),
        owner_task_text="Implement the release workflow.",
        context_source="bound_chat",
        personas=default_registry(),
    )

    artifact = CoordinatorTeamProposalService().build_team_proposal_artifact(
        context
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


def test_team_proposal_artifact_includes_all_baseline_roles_in_stable_order(
    tmp_path,
):
    repo = _git_repo(tmp_path)
    context = CoordinatorTeamProposalContext(
        snapshot=_snapshot(repo),
        owner_task_text="Prepare the release branch.",
        context_source="owner_dm_single_project",
        personas=default_registry(),
    )

    artifact = CoordinatorTeamProposalService().build_team_proposal_artifact(
        context
    )

    last_index = -1
    for role in BASELINE_TEAM_ROLE_ORDER:
        persona = context.personas.for_role(role)
        role_index = artifact.index(f"- role_id: {role}")
        assert role_index > last_index
        last_index = role_index
        assert f"  human_name: {persona.human_name}" in artifact
        assert f"  title: {persona.title}" in artifact
        assert f"  seniority: {persona.seniority}" in artifact
        assert "  mandate: " in artifact


def test_team_proposal_artifact_is_deterministic(tmp_path):
    repo = _git_repo(tmp_path)
    context = CoordinatorTeamProposalContext(
        snapshot=_snapshot(repo),
        owner_task_text="Prepare the release branch.",
        context_source="owner_dm_single_project",
        personas=default_registry(),
    )
    service = CoordinatorTeamProposalService()

    first = service.build_team_proposal_artifact(context)
    second = service.build_team_proposal_artifact(context)

    assert first == second
