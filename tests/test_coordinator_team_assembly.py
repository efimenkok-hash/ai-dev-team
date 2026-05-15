from pathlib import Path

import pytest

from core.agent_personas import DEFAULT_PERSONAS, PersonaRegistry, default_registry
from core.agent_role_catalog import SPECIALIST_ROLE_ORDER
from core.coordinator_role import COORDINATOR_ROLE
from core.coordinator_team_assembly import (
    BASELINE_INTERNAL_TEAM_ROLE_ORDER,
    AssembledTeamMember,
    CoordinatorTeamAssembly,
    CoordinatorTeamAssemblyContext,
    CoordinatorTeamAssemblyService,
)
from core.project_models import Project, ProjectChatBinding, ProjectPolicy
from core.project_registry import ProjectSnapshot
from core.project_runtime import ProjectRuntimeBinding
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


def _registry_without(role: str) -> PersonaRegistry:
    return PersonaRegistry(
        tuple(persona for persona in DEFAULT_PERSONAS if persona.agent_role != role)
    )


def _member(role: str, *, captain: bool = False) -> AssembledTeamMember:
    persona = default_registry().for_role(role)
    return AssembledTeamMember(
        persona=persona,
        mandate=f"Mandate for {role}",
        is_captain=captain,
    )


def _assembly(repo_path: Path, **overrides) -> CoordinatorTeamAssembly:
    members = tuple(
        _member(role, captain=(role == COORDINATOR_ROLE))
        for role in BASELINE_INTERNAL_TEAM_ROLE_ORDER
    )
    data = {
        "snapshot": _snapshot(repo_path, chat_binding=_chat_binding()),
        "context_source": "bound_chat",
        "owner_task_text": "Implement the release workflow.",
        "assembly_mode": "baseline_internal_team",
        "captain_role": COORDINATOR_ROLE,
        "members": members,
    }
    data.update(overrides)
    return CoordinatorTeamAssembly(**data)


def _hints() -> SpecializationHints:
    return SpecializationHints(
        (
            SpecializationHint(
                "data_agent",
                "Task touches schema and analytics shape.",
            ),
            SpecializationHint(
                "security_agent",
                "Task touches auth and secrets.",
            ),
        )
    )


def test_context_happy_path_for_bound_chat(tmp_path):
    repo = _git_repo(tmp_path)
    context = CoordinatorTeamAssemblyContext(
        snapshot=_snapshot(repo, chat_binding=_chat_binding()),
        owner_task_text="Implement the release workflow.",
        context_source="bound_chat",
        personas=default_registry(),
    )

    assert context.context_source == "bound_chat"


def test_context_happy_path_for_owner_dm_single_project(tmp_path):
    repo = _git_repo(tmp_path)
    context = CoordinatorTeamAssemblyContext(
        snapshot=_snapshot(repo),
        owner_task_text="Prepare the release branch.",
        context_source="owner_dm_single_project",
        personas=default_registry(),
    )

    assert context.context_source == "owner_dm_single_project"


def test_context_rejects_bad_snapshot():
    with pytest.raises(ValueError, match="invalid_project_snapshot_type"):
        CoordinatorTeamAssemblyContext(
            snapshot="bad",  # type: ignore[arg-type]
            owner_task_text="Task",
            context_source="bound_chat",
            personas=default_registry(),
        )


def test_context_rejects_snapshot_without_runtime_binding(tmp_path):
    with pytest.raises(ValueError, match="snapshot_missing_runtime_binding"):
        CoordinatorTeamAssemblyContext(
            snapshot=_snapshot(None),
            owner_task_text="Task",
            context_source="bound_chat",
            personas=default_registry(),
        )


@pytest.mark.parametrize("bad", ["", "   "])
def test_context_rejects_empty_owner_task_text(tmp_path, bad):
    repo = _git_repo(tmp_path)
    with pytest.raises(ValueError, match="empty_owner_task_text"):
        CoordinatorTeamAssemblyContext(
            snapshot=_snapshot(repo),
            owner_task_text=bad,
            context_source="bound_chat",
            personas=default_registry(),
        )


@pytest.mark.parametrize("bad", ["none", "registry", "", "  "])
def test_context_rejects_bad_context_source(tmp_path, bad):
    repo = _git_repo(tmp_path)
    with pytest.raises(ValueError, match="invalid_context_source"):
        CoordinatorTeamAssemblyContext(
            snapshot=_snapshot(repo),
            owner_task_text="Task",
            context_source=bad,
            personas=default_registry(),
        )


def test_context_rejects_bad_personas_type(tmp_path):
    repo = _git_repo(tmp_path)
    with pytest.raises(ValueError, match="invalid_persona_registry_type"):
        CoordinatorTeamAssemblyContext(
            snapshot=_snapshot(repo),
            owner_task_text="Task",
            context_source="bound_chat",
            personas="bad",  # type: ignore[arg-type]
        )


def test_context_rejects_missing_required_roles(tmp_path):
    repo = _git_repo(tmp_path)
    with pytest.raises(ValueError, match="missing_required_persona_roles"):
        CoordinatorTeamAssemblyContext(
            snapshot=_snapshot(repo),
            owner_task_text="Task",
            context_source="bound_chat",
            personas=_registry_without("fixer_agent"),
        )


def test_member_happy_path_for_captain():
    member = _member(COORDINATOR_ROLE, captain=True)
    assert member.is_captain is True


def test_member_happy_path_for_non_captain():
    member = _member("planning_agent")
    assert member.is_captain is False


def test_member_rejects_bad_persona():
    with pytest.raises(ValueError, match="invalid_agent_persona_type"):
        AssembledTeamMember(  # type: ignore[arg-type]
            persona="bad",
            mandate="Task mandate",
        )


@pytest.mark.parametrize("bad", ["", "   "])
def test_member_rejects_empty_mandate(bad):
    with pytest.raises(ValueError, match="empty_member_mandate"):
        AssembledTeamMember(
            persona=default_registry().for_role("planning_agent"),
            mandate=bad,
        )


@pytest.mark.parametrize("field_name", ["is_captain", "is_internal", "is_active"])
def test_member_rejects_non_bool_flags(field_name):
    kwargs = {
        "persona": default_registry().for_role("planning_agent"),
        "mandate": "Task mandate",
    }
    kwargs[field_name] = "bad"  # type: ignore[index]
    with pytest.raises(ValueError, match=f"invalid_{field_name}_type"):
        AssembledTeamMember(**kwargs)  # type: ignore[arg-type]


def test_member_rejects_non_coordinator_captain():
    with pytest.raises(ValueError, match="captain_must_be_coordinator"):
        AssembledTeamMember(
            persona=default_registry().for_role("planning_agent"),
            mandate="Task mandate",
            is_captain=True,
        )


def test_assembly_happy_path(tmp_path):
    repo = _git_repo(tmp_path)
    assembly = _assembly(repo)
    assert assembly.captain_role == COORDINATOR_ROLE


def test_assembly_rejects_bad_snapshot(tmp_path):
    repo = _git_repo(tmp_path)
    with pytest.raises(ValueError, match="invalid_project_snapshot_type"):
        _assembly(repo, snapshot="bad")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["none", "registry", "", "  "])
def test_assembly_rejects_bad_context_source(tmp_path, bad):
    repo = _git_repo(tmp_path)
    with pytest.raises(ValueError, match="invalid_context_source"):
        _assembly(repo, context_source=bad)


def test_assembly_rejects_bad_assembly_mode(tmp_path):
    repo = _git_repo(tmp_path)
    with pytest.raises(ValueError, match="invalid_assembly_mode"):
        _assembly(repo, assembly_mode="dynamic")


def test_assembly_rejects_bad_captain_role(tmp_path):
    repo = _git_repo(tmp_path)
    with pytest.raises(ValueError, match="invalid_captain_role"):
        _assembly(repo, captain_role="pm_agent")


def test_assembly_rejects_empty_members(tmp_path):
    repo = _git_repo(tmp_path)
    with pytest.raises(ValueError, match="members_must_be_non_empty_tuple"):
        _assembly(repo, members=())


def test_assembly_rejects_duplicate_roles(tmp_path):
    repo = _git_repo(tmp_path)
    duplicate_members = (
        _member(COORDINATOR_ROLE, captain=True),
        _member("planning_agent"),
        _member("planning_agent"),
        *(
            _member(role)
            for role in BASELINE_INTERNAL_TEAM_ROLE_ORDER[3:]
        ),
    )
    with pytest.raises(ValueError, match="duplicate_assembled_team_roles"):
        _assembly(repo, members=duplicate_members)


def test_assembly_rejects_missing_captain(tmp_path):
    repo = _git_repo(tmp_path)
    members = tuple(_member(role) for role in BASELINE_INTERNAL_TEAM_ROLE_ORDER)
    with pytest.raises(
        ValueError,
        match="assembled_team_requires_exactly_one_captain",
    ):
        _assembly(repo, members=members)


def test_assembly_rejects_captain_not_first(tmp_path):
    repo = _git_repo(tmp_path)
    members = (
        _member("planning_agent"),
        _member(COORDINATOR_ROLE, captain=True),
        *(_member(role) for role in BASELINE_INTERNAL_TEAM_ROLE_ORDER[2:]),
    )
    with pytest.raises(ValueError, match="assembled_team_captain_must_be_first"):
        _assembly(repo, members=members)


def test_assembly_rejects_non_exact_baseline_team_set(tmp_path):
    repo = _git_repo(tmp_path)
    members = tuple(
        _member(role, captain=(role == COORDINATOR_ROLE))
        for role in BASELINE_INTERNAL_TEAM_ROLE_ORDER[:-1]
    )
    with pytest.raises(
        ValueError,
        match="assembled_team_must_match_baseline_internal_team",
    ):
        _assembly(repo, members=members)


def test_service_assembles_baseline_team_in_stable_order(tmp_path):
    repo = _git_repo(tmp_path)
    context = CoordinatorTeamAssemblyContext(
        snapshot=_snapshot(repo, chat_binding=_chat_binding()),
        owner_task_text="Implement the release workflow.",
        context_source="bound_chat",
        personas=default_registry(),
    )

    assembly = CoordinatorTeamAssemblyService().assemble_team(context)

    assert assembly.assembly_mode == "baseline_internal_team"
    assert assembly.captain_role == COORDINATOR_ROLE
    assert tuple(member.persona.agent_role for member in assembly.members) == (
        BASELINE_INTERNAL_TEAM_ROLE_ORDER
    )
    assert assembly.members[0].is_captain is True
    assert all(member.is_internal for member in assembly.members)
    assert all(member.is_active for member in assembly.members)
    assert not any(
        member.persona.agent_role in SPECIALIST_ROLE_ORDER
        for member in assembly.members
    )


def test_service_ignores_extra_specialist_personas_and_keeps_baseline_shape(
    tmp_path,
):
    repo = _git_repo(tmp_path)
    assembly = CoordinatorTeamAssemblyService().assemble_team(
        CoordinatorTeamAssemblyContext(
            snapshot=_snapshot(repo, chat_binding=_chat_binding()),
            owner_task_text="Implement the release workflow.",
            context_source="bound_chat",
            personas=PersonaRegistry(DEFAULT_PERSONAS),
        )
    )

    assert len(assembly.members) == len(BASELINE_INTERNAL_TEAM_ROLE_ORDER)
    assert tuple(member.persona.agent_role for member in assembly.members) == (
        BASELINE_INTERNAL_TEAM_ROLE_ORDER
    )
    assert all(
        role not in SPECIALIST_ROLE_ORDER
        for role in (member.persona.agent_role for member in assembly.members)
    )


def test_assembly_carries_specialization_hints_without_changing_roster(tmp_path):
    repo = _git_repo(tmp_path)
    assembly = CoordinatorTeamAssemblyService().assemble_team(
        CoordinatorTeamAssemblyContext(
            snapshot=_snapshot(repo, chat_binding=_chat_binding()),
            owner_task_text="Implement the release workflow.",
            context_source="bound_chat",
            personas=default_registry(),
            specialization_hints=_hints(),
        )
    )

    assert tuple(member.persona.agent_role for member in assembly.members) == (
        BASELINE_INTERNAL_TEAM_ROLE_ORDER
    )
    assert tuple(
        hint.specialist_role for hint in assembly.specialization_hints.items
    ) == ("security_agent", "data_agent")


def test_format_team_assembly_includes_project_context_and_stable_roster(tmp_path):
    repo = _git_repo(tmp_path)
    assembly = CoordinatorTeamAssemblyService().assemble_team(
        CoordinatorTeamAssemblyContext(
            snapshot=_snapshot(repo),
            owner_task_text="Prepare the release branch.",
            context_source="owner_dm_single_project",
            personas=default_registry(),
        )
    )
    service = CoordinatorTeamAssemblyService()

    rendered = service.format_team_assembly(assembly)

    assert "alpha_project" in rendered
    assert "alpha-project" in rendered
    assert "Alpha Project" in rendered
    assert "owner DM fallback" in rendered
    assert "captain_role: coordinator_agent" in rendered
    assert "Specialization hints:" in rendered
    assert "- none" in rendered
    last_index = -1
    for role in BASELINE_INTERNAL_TEAM_ROLE_ORDER:
        idx = rendered.index(f"- role_id: {role}")
        assert idx > last_index
        last_index = idx


def test_format_team_assembly_renders_non_empty_hints_in_stable_order(tmp_path):
    repo = _git_repo(tmp_path)
    assembly = CoordinatorTeamAssemblyService().assemble_team(
        CoordinatorTeamAssemblyContext(
            snapshot=_snapshot(repo),
            owner_task_text="Prepare the release branch.",
            context_source="owner_dm_single_project",
            personas=default_registry(),
            specialization_hints=_hints(),
        )
    )
    rendered = CoordinatorTeamAssemblyService().format_team_assembly(assembly)

    assert "Specialization hints:" in rendered
    security_index = rendered.index("- specialist_role: security_agent")
    data_index = rendered.index("- specialist_role: data_agent")
    assert security_index < data_index
    assert "Task touches auth and secrets." in rendered
    assert "Task touches schema and analytics shape." in rendered


def test_formatting_is_deterministic(tmp_path):
    repo = _git_repo(tmp_path)
    service = CoordinatorTeamAssemblyService()
    assembly = service.assemble_team(
        CoordinatorTeamAssemblyContext(
            snapshot=_snapshot(repo, chat_binding=_chat_binding()),
            owner_task_text="Implement the release workflow.",
            context_source="bound_chat",
            personas=default_registry(),
        )
    )

    assert service.format_team_assembly(assembly) == service.format_team_assembly(assembly)
