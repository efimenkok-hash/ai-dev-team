from __future__ import annotations

from pathlib import Path

import pytest

from core.bot_commands import parse_command
from core.project_models import Project, ProjectPolicy
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.project_team_commands import (
    ProjectTeamCommand,
    ProjectTeamCommandContext,
    ProjectTeamCommandService,
    describe_project_team_command_error,
    parse_project_team_command,
)
from core.state_db import StateDB


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


def _policy(**overrides: object) -> ProjectPolicy:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "allow_hiring": True,
        "allow_agent_dm": False,
        "require_owner_approval_for_hires": True,
    }
    data.update(overrides)
    return ProjectPolicy(**data)


def _snapshot(**overrides: object) -> ProjectSnapshot:
    data: dict[str, object] = {
        "project": _project(),
        "policy": _policy(),
    }
    data.update(overrides)
    return ProjectSnapshot(**data)


def _register_project(
    tmp_path: Path,
    *,
    snapshot: ProjectSnapshot | None = None,
) -> ProjectRegistry:
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    registry.register_project(_snapshot() if snapshot is None else snapshot)
    return registry


def _context(
    *,
    snapshot: ProjectSnapshot | None = None,
    actor_user_id: int = 101,
    context_source: str = "bound_chat",
) -> ProjectTeamCommandContext:
    return ProjectTeamCommandContext(
        snapshot=_snapshot() if snapshot is None else snapshot,
        actor_user_id=actor_user_id,
        context_source=context_source,
    )


def test_parse_team_without_args_maps_to_list():
    parsed = parse_project_team_command(parse_command("/team"))
    assert parsed == ProjectTeamCommand(action="list")


def test_parse_team_list_maps_to_list():
    parsed = parse_project_team_command(parse_command("/team list"))
    assert parsed == ProjectTeamCommand(action="list")


def test_parse_team_add_accepts_specialist_role():
    parsed = parse_project_team_command(parse_command("/team add security_agent"))
    assert parsed == ProjectTeamCommand(
        action="add",
        specialist_role="security_agent",
    )


def test_parse_team_remove_accepts_specialist_role():
    parsed = parse_project_team_command(parse_command("/team remove devops_agent"))
    assert parsed == ProjectTeamCommand(
        action="remove",
        specialist_role="devops_agent",
    )


def test_parse_team_rejects_invalid_subcommand():
    with pytest.raises(ValueError, match="project_team_invalid_subcommand"):
        parse_project_team_command(parse_command("/team hire security_agent"))


def test_parse_team_rejects_extra_args():
    with pytest.raises(ValueError, match="project_team_extra_args"):
        parse_project_team_command(parse_command("/team add security_agent extra"))


def test_parse_team_rejects_baseline_role():
    with pytest.raises(ValueError, match="unknown_specialist_role:writer_agent"):
        parse_project_team_command(parse_command("/team add writer_agent"))


def test_parse_team_rejects_unknown_role():
    with pytest.raises(ValueError, match="unknown_specialist_role:ghost_agent"):
        parse_project_team_command(parse_command("/team remove ghost_agent"))


def test_project_team_list_renders_none_for_empty_roster(tmp_path: Path):
    registry = _register_project(tmp_path)
    service = ProjectTeamCommandService(registry)

    result = service.handle(
        ProjectTeamCommand(action="list"),
        _context(),
    )

    assert result.roster.is_empty is True
    assert "Project specialists:" in result.message_text
    assert "- none" in result.message_text


def test_project_team_add_updates_persisted_roster(tmp_path: Path):
    registry = _register_project(tmp_path)
    service = ProjectTeamCommandService(registry)

    result = service.handle(
        ProjectTeamCommand(action="add", specialist_role="security_agent"),
        _context(),
    )

    assert result.roster.specialist_roles == ("security_agent",)
    assert registry.get_project_specialist_roster("alpha_project").specialist_roles == (
        "security_agent",
    )
    assert "добавлен" in result.message_text.lower()


def test_project_team_remove_updates_persisted_roster(tmp_path: Path):
    registry = _register_project(tmp_path)
    registry.add_project_specialist("alpha_project", "security_agent")
    service = ProjectTeamCommandService(registry)

    result = service.handle(
        ProjectTeamCommand(action="remove", specialist_role="security_agent"),
        _context(),
    )

    assert result.roster.specialist_roles == ()
    assert registry.get_project_specialist_roster("alpha_project").specialist_roles == ()
    assert "удалён" in result.message_text.lower()


def test_project_team_duplicate_add_is_rejected_truthfully(tmp_path: Path):
    registry = _register_project(tmp_path)
    service = ProjectTeamCommandService(registry)
    context = _context()
    command = ProjectTeamCommand(action="add", specialist_role="security_agent")

    service.handle(command, context)

    with pytest.raises(
        ValueError,
        match="duplicate_project_specialist:alpha_project:security_agent",
    ):
        service.handle(command, context)


def test_project_team_remove_absent_role_is_rejected_truthfully(tmp_path: Path):
    registry = _register_project(tmp_path)
    service = ProjectTeamCommandService(registry)

    with pytest.raises(
        ValueError,
        match="unknown_project_specialist:alpha_project:security_agent",
    ):
        service.handle(
            ProjectTeamCommand(action="remove", specialist_role="security_agent"),
            _context(),
        )


def test_project_team_non_owner_cannot_mutate(tmp_path: Path):
    registry = _register_project(tmp_path)
    service = ProjectTeamCommandService(registry)

    with pytest.raises(ValueError, match="project_team_mutation_requires_owner"):
        service.handle(
            ProjectTeamCommand(action="add", specialist_role="security_agent"),
            _context(actor_user_id=999),
        )


def test_project_team_allow_hiring_false_blocks_mutation(tmp_path: Path):
    registry = _register_project(
        tmp_path,
        snapshot=_snapshot(policy=_policy(allow_hiring=False)),
    )
    service = ProjectTeamCommandService(registry)

    with pytest.raises(
        ValueError,
        match="project_team_mutation_disallowed_by_policy",
    ):
        service.handle(
            ProjectTeamCommand(action="add", specialist_role="security_agent"),
            _context(snapshot=_snapshot(policy=_policy(allow_hiring=False))),
        )


def test_project_team_context_rejects_invalid_context_source():
    with pytest.raises(ValueError, match="invalid_project_team_context_source"):
        ProjectTeamCommandContext(
            snapshot=_snapshot(),
            actor_user_id=101,
            context_source="none",
        )


def test_project_team_error_description_includes_usage_for_bad_syntax():
    text = describe_project_team_command_error("project_team_invalid_subcommand")
    assert "Использование:" in text
    assert "/team add" in text
