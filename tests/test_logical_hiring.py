from __future__ import annotations

from pathlib import Path

import pytest

from core.logical_hiring import (
    LogicalHireCandidate,
    LogicalHiringPlan,
    LogicalHiringResult,
    LogicalHiringService,
)
from core.project_models import Project, ProjectPolicy
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.project_team_state import ProjectSpecialistRoster
from core.specialization_hints import SpecializationHint, SpecializationHints
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


def _registry(tmp_path: Path, *, snapshot: ProjectSnapshot | None = None) -> ProjectRegistry:
    registry = ProjectRegistry(_make_db(tmp_path))
    registry.register_project(_snapshot() if snapshot is None else snapshot)
    return registry


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


def test_run_from_hints_with_empty_hints_returns_no_candidates(tmp_path: Path):
    registry = _registry(tmp_path)
    service = LogicalHiringService(registry)

    result = service.run_from_hints(_snapshot(), SpecializationHints.empty())

    assert result.status == "no_candidates"
    assert result.initial_roster == result.final_roster
    assert result.hired_roles == ()


def test_run_from_hints_hires_absent_specialist(tmp_path: Path):
    immediate_snapshot = _snapshot(
        policy=_policy(require_owner_approval_for_hires=False),
    )
    registry = _registry(tmp_path, snapshot=immediate_snapshot)
    service = LogicalHiringService(registry)

    result = service.run_from_hints(
        immediate_snapshot,
        _hints(("security_agent", "Auth and secrets are in scope.")),
    )

    assert result.status == "hired"
    assert result.hired_roles == ("security_agent",)
    assert result.final_roster.specialist_roles == ("security_agent",)
    assert registry.get_project_specialist_roster("alpha_project").specialist_roles == (
        "security_agent",
    )


def test_run_from_hints_returns_already_satisfied_for_existing_role(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.add_project_specialist("alpha_project", "security_agent")
    service = LogicalHiringService(registry)

    result = service.run_from_hints(
        _snapshot(),
        _hints(("security_agent", "Auth and secrets are in scope.")),
    )

    assert result.status == "already_satisfied"
    assert result.hired_roles == ()
    assert result.final_roster.specialist_roles == ("security_agent",)


def test_run_from_hints_hires_multiple_roles_in_deterministic_order(tmp_path: Path):
    immediate_snapshot = _snapshot(
        policy=_policy(require_owner_approval_for_hires=False),
    )
    registry = _registry(tmp_path, snapshot=immediate_snapshot)
    service = LogicalHiringService(registry)

    result = service.run_from_hints(
        immediate_snapshot,
        _hints(
            ("data_agent", "Schema and migrations are risky."),
            ("security_agent", "Secrets and trust boundaries are risky."),
            ("devops_agent", "Deployability and rollback are risky."),
        ),
    )

    assert result.status == "hired"
    assert result.hired_roles == (
        "security_agent",
        "devops_agent",
        "data_agent",
    )
    assert result.final_roster.specialist_roles == (
        "security_agent",
        "devops_agent",
        "data_agent",
    )


def test_run_from_hints_blocked_by_allow_hiring_false(tmp_path: Path):
    blocked_snapshot = _snapshot(policy=_policy(allow_hiring=False))
    registry = _registry(tmp_path, snapshot=blocked_snapshot)
    service = LogicalHiringService(registry)

    result = service.run_from_hints(
        blocked_snapshot,
        _hints(("security_agent", "Auth and secrets are in scope.")),
    )

    assert result.status == "blocked_by_policy"
    assert result.hired_roles == ()
    assert result.final_roster == result.initial_roster
    assert registry.get_project_specialist_roster("alpha_project").specialist_roles == ()


def test_run_from_hints_reloads_current_persisted_policy_before_hiring(
    tmp_path: Path,
):
    stale_snapshot = _snapshot(policy=_policy(allow_hiring=True))
    registry = _registry(tmp_path, snapshot=stale_snapshot)
    registry.set_project_policy(_policy(allow_hiring=False))
    service = LogicalHiringService(registry)

    result = service.run_from_hints(
        stale_snapshot,
        _hints(("security_agent", "Auth and secrets are in scope.")),
    )

    assert result.status == "blocked_by_policy"
    assert result.hired_roles == ()
    assert result.final_roster == result.initial_roster
    assert registry.get_project_specialist_roster("alpha_project").specialist_roles == ()


def test_apply_plan_reloads_current_persisted_roster_before_hiring(
    tmp_path: Path,
):
    snapshot = _snapshot()
    registry = _registry(tmp_path, snapshot=snapshot)
    service = LogicalHiringService(registry)

    plan = service.plan_from_hints(
        snapshot,
        _hints(("security_agent", "Auth and secrets are in scope.")),
    )
    registry.add_project_specialist("alpha_project", "security_agent")

    result = service.apply_plan(snapshot, plan)

    assert result.status == "already_satisfied"
    assert result.hired_roles == ()
    assert result.initial_roster.specialist_roles == ("security_agent",)
    assert result.final_roster.specialist_roles == ("security_agent",)
    assert "не потребовался" in result.message_text


def test_require_owner_approval_for_hires_creates_pending_request(tmp_path: Path):
    approval_snapshot = _snapshot(policy=_policy(require_owner_approval_for_hires=True))
    registry = _registry(tmp_path, snapshot=approval_snapshot)
    service = LogicalHiringService(registry)

    result = service.run_from_hints(
        approval_snapshot,
        _hints(("security_agent", "Auth and secrets are in scope.")),
    )

    assert result.status == "pending_owner_approval"
    assert result.hired_roles == ()
    assert registry.get_project_specialist_roster("alpha_project").specialist_roles == ()
    pending = registry.list_pending_hire_requests("alpha_project")
    assert len(pending) == 1
    assert pending[0].specialist_role == "security_agent"
    assert pending[0].source == "logical_hiring_pm_hint"
    assert pending[0].request_id in result.message_text


def test_run_from_hints_reuses_existing_pending_owner_approval_request(
    tmp_path: Path,
):
    approval_snapshot = _snapshot(policy=_policy(require_owner_approval_for_hires=True))
    registry = _registry(tmp_path, snapshot=approval_snapshot)
    service = LogicalHiringService(registry)

    first = service.run_from_hints(
        approval_snapshot,
        _hints(("security_agent", "Auth and secrets are in scope.")),
    )
    second = service.run_from_hints(
        approval_snapshot,
        _hints(("security_agent", "Auth and secrets are in scope.")),
    )

    assert first.status == "pending_owner_approval"
    assert second.status == "pending_owner_approval"
    pending = registry.list_pending_hire_requests("alpha_project")
    assert len(pending) == 1
    assert pending[0].request_id in first.message_text
    assert pending[0].request_id in second.message_text


def test_logical_hiring_plan_rejects_duplicate_candidates():
    with pytest.raises(ValueError, match="duplicate_logical_hire_candidate:security_agent"):
        LogicalHiringPlan(
            project_id="alpha_project",
            current_roster=ProjectSpecialistRoster(
                project_id="alpha_project",
                specialist_roles=(),
            ),
            candidates=(
                LogicalHireCandidate(
                    specialist_role="security_agent",
                    reason="one",
                ),
                LogicalHireCandidate(
                    specialist_role="security_agent",
                    reason="two",
                ),
            ),
        )


def test_apply_plan_rejects_project_id_mismatch(tmp_path: Path):
    registry = _registry(tmp_path)
    service = LogicalHiringService(registry)
    plan = LogicalHiringPlan(
        project_id="beta_project",
        current_roster=ProjectSpecialistRoster(
            project_id="beta_project",
            specialist_roles=(),
        ),
        candidates=(),
    )

    with pytest.raises(ValueError, match="logical_hiring_plan_project_id_mismatch"):
        service.apply_plan(_snapshot(), plan)


def test_logical_hire_candidate_rejects_unknown_role():
    with pytest.raises(ValueError, match="unknown_specialist_role:writer_agent"):
        LogicalHireCandidate(
            specialist_role="writer_agent",
            reason="not allowed",
        )


def test_logical_hiring_result_non_hired_status_forbids_roster_change():
    initial_roster = ProjectSpecialistRoster(
        project_id="alpha_project",
        specialist_roles=(),
    )
    final_roster = ProjectSpecialistRoster(
        project_id="alpha_project",
        specialist_roles=("security_agent",),
    )

    with pytest.raises(
        ValueError,
        match="logical_hiring_non_hired_status_forbids_roster_change",
    ):
        LogicalHiringResult(
            project_id="alpha_project",
            status="already_satisfied",
            initial_roster=initial_roster,
            final_roster=final_roster,
            hired_roles=(),
            message_text="bad",
        )
