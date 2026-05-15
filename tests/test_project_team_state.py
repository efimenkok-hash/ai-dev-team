from __future__ import annotations

import pytest

from core.agent_role_catalog import BASELINE_INTERNAL_TEAM_ROLE_ORDER
from core.project_team_state import (
    ProjectSpecialistAssignment,
    ProjectSpecialistRoster,
)


def test_empty_roster_is_valid():
    roster = ProjectSpecialistRoster(
        project_id="alpha_project",
        specialist_roles=(),
    )

    assert roster.is_empty is True
    assert roster.specialist_roles == ()


def test_roster_orders_specialists_deterministically():
    roster = ProjectSpecialistRoster(
        project_id="alpha_project",
        specialist_roles=(
            "data_agent",
            "security_agent",
            "devops_agent",
        ),
    )

    assert roster.specialist_roles == (
        "security_agent",
        "devops_agent",
        "data_agent",
    )


def test_roster_contains_helper_works():
    roster = ProjectSpecialistRoster(
        project_id="alpha_project",
        specialist_roles=("security_agent",),
    )

    assert roster.contains("security_agent") is True
    assert roster.contains("devops_agent") is False


def test_resolved_team_roles_returns_baseline_plus_specialists():
    roster = ProjectSpecialistRoster(
        project_id="alpha_project",
        specialist_roles=("data_agent", "security_agent"),
    )

    assert roster.resolved_team_roles() == (
        BASELINE_INTERNAL_TEAM_ROLE_ORDER
        + ("security_agent", "data_agent")
    )


def test_empty_roster_resolves_to_baseline_only_team():
    roster = ProjectSpecialistRoster(
        project_id="alpha_project",
        specialist_roles=(),
    )

    assert roster.resolved_team_roles() == BASELINE_INTERNAL_TEAM_ROLE_ORDER


def test_assignment_accepts_valid_specialist():
    assignment = ProjectSpecialistAssignment(
        project_id="alpha_project",
        specialist_role="security_agent",
    )

    assert assignment.project_id == "alpha_project"
    assert assignment.specialist_role == "security_agent"


@pytest.mark.parametrize(
    "role",
    ("writer_agent", "planning_agent", "coordinator_agent", "ghost_agent"),
)
def test_assignment_rejects_non_specialist_roles(role: str):
    with pytest.raises(ValueError, match=fr"unknown_specialist_role:{role}"):
        ProjectSpecialistAssignment(
            project_id="alpha_project",
            specialist_role=role,
        )


def test_roster_rejects_duplicate_specialist_role():
    with pytest.raises(ValueError, match="duplicate_specialist_role:security_agent"):
        ProjectSpecialistRoster(
            project_id="alpha_project",
            specialist_roles=("security_agent", "security_agent"),
        )


def test_roster_rejects_non_tuple_roles():
    with pytest.raises(ValueError, match="specialist_roles_must_be_tuple"):
        ProjectSpecialistRoster(
            project_id="alpha_project",
            specialist_roles=["security_agent"],  # type: ignore[arg-type]
        )
