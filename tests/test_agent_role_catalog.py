"""Tests for canonical agent role catalog."""

from core.agent_role_catalog import (
    BASELINE_INTERNAL_TEAM_ROLE_ORDER,
    KNOWN_AGENT_ROLES,
    SPECIALIST_ROLE_ORDER,
    is_baseline_internal_team_role,
    is_known_agent_role,
    is_specialist_role,
)


def test_baseline_internal_team_role_order_is_deterministic():
    assert BASELINE_INTERNAL_TEAM_ROLE_ORDER == (
        "coordinator_agent",
        "planning_agent",
        "pm_agent",
        "architect_agent",
        "writer_agent",
        "reviewer_agent",
        "tester_agent",
        "qa_agent",
        "fixer_agent",
    )


def test_specialist_role_order_is_deterministic():
    assert SPECIALIST_ROLE_ORDER == (
        "security_agent",
        "devops_agent",
        "data_agent",
    )


def test_known_agent_roles_contains_baseline_and_specialists():
    assert KNOWN_AGENT_ROLES == frozenset(
        BASELINE_INTERNAL_TEAM_ROLE_ORDER + SPECIALIST_ROLE_ORDER
    )


def test_specialists_are_not_in_baseline_internal_team_order():
    assert not any(
        role in BASELINE_INTERNAL_TEAM_ROLE_ORDER
        for role in SPECIALIST_ROLE_ORDER
    )


def test_role_catalog_has_no_duplicates_across_sets():
    combined = BASELINE_INTERNAL_TEAM_ROLE_ORDER + SPECIALIST_ROLE_ORDER
    assert len(set(combined)) == len(combined)
    assert len(KNOWN_AGENT_ROLES) == len(combined)


def test_role_classification_helpers_work_for_baseline_and_specialists():
    assert is_known_agent_role("coordinator_agent") is True
    assert is_baseline_internal_team_role("coordinator_agent") is True
    assert is_specialist_role("coordinator_agent") is False

    assert is_known_agent_role("security_agent") is True
    assert is_baseline_internal_team_role("security_agent") is False
    assert is_specialist_role("security_agent") is True


def test_unknown_role_is_not_treated_as_known_or_specialist():
    assert is_known_agent_role("ghost_agent") is False
    assert is_baseline_internal_team_role("ghost_agent") is False
    assert is_specialist_role("ghost_agent") is False
    assert is_known_agent_role("") is False
    assert is_known_agent_role(None) is False  # type: ignore[arg-type]

