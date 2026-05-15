from __future__ import annotations

from pathlib import Path

import pytest

from core.hire_approval import (
    HireApprovalDecision,
    HireApprovalResult,
    HireApprovalService,
    PendingHireRequest,
)
from core.project_models import Project, ProjectPolicy
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.project_team_state import ProjectSpecialistRoster
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


def _registry(
    tmp_path: Path,
    *,
    snapshot: ProjectSnapshot | None = None,
) -> ProjectRegistry:
    registry = ProjectRegistry(_make_db(tmp_path))
    registry.register_project(_snapshot() if snapshot is None else snapshot)
    return registry


def test_request_sensitive_hire_creates_pending_request(tmp_path: Path):
    snapshot = _snapshot()
    registry = _registry(tmp_path, snapshot=snapshot)
    service = HireApprovalService(registry)

    result = service.request_sensitive_hire(
        snapshot,
        "security_agent",
        "Auth and secrets are in scope.",
        "logical_hiring_pm_hint",
    )

    assert result.status == "pending_created"
    assert result.request_id is not None
    assert result.roster_before == result.roster_after
    pending = service.list_pending_requests("alpha_project")
    assert len(pending) == 1
    assert pending[0].request_id == result.request_id
    assert pending[0].specialist_role == "security_agent"


def test_request_sensitive_hire_reuses_existing_pending_request(tmp_path: Path):
    snapshot = _snapshot()
    registry = _registry(tmp_path, snapshot=snapshot)
    service = HireApprovalService(registry)

    first = service.request_sensitive_hire(
        snapshot,
        "security_agent",
        "Auth and secrets are in scope.",
        "logical_hiring_pm_hint",
    )
    second = service.request_sensitive_hire(
        snapshot,
        "security_agent",
        "Auth and secrets are in scope.",
        "logical_hiring_pm_hint",
    )

    assert first.status == "pending_created"
    assert second.status == "pending_exists"
    assert second.request_id == first.request_id
    assert len(service.list_pending_requests("alpha_project")) == 1


def test_approve_request_adds_specialist_to_roster(tmp_path: Path):
    snapshot = _snapshot()
    registry = _registry(tmp_path, snapshot=snapshot)
    service = HireApprovalService(registry)
    pending = service.request_sensitive_hire(
        snapshot,
        "security_agent",
        "Auth and secrets are in scope.",
        "logical_hiring_pm_hint",
    )

    result = service.apply_decision(
        snapshot,
        HireApprovalDecision(
            request_id=pending.request_id,
            decision="approve",
            actor_user_id=101,
        ),
    )

    assert result.status == "approved"
    assert result.roster_after.specialist_roles == ("security_agent",)
    assert service.list_pending_requests("alpha_project") == ()


def test_reject_request_leaves_roster_unchanged(tmp_path: Path):
    snapshot = _snapshot()
    registry = _registry(tmp_path, snapshot=snapshot)
    service = HireApprovalService(registry)
    pending = service.request_sensitive_hire(
        snapshot,
        "security_agent",
        "Auth and secrets are in scope.",
        "logical_hiring_pm_hint",
    )

    result = service.apply_decision(
        snapshot,
        HireApprovalDecision(
            request_id=pending.request_id,
            decision="reject",
            actor_user_id=101,
        ),
    )

    assert result.status == "rejected"
    assert result.roster_after.specialist_roles == ()
    assert service.list_pending_requests("alpha_project") == ()


def test_apply_decision_returns_not_found_for_unknown_request(tmp_path: Path):
    snapshot = _snapshot()
    registry = _registry(tmp_path, snapshot=snapshot)
    service = HireApprovalService(registry)

    result = service.apply_decision(
        snapshot,
        HireApprovalDecision(
            request_id="hire-123-missing",
            decision="approve",
            actor_user_id=101,
        ),
    )

    assert result.status == "not_found"
    assert result.roster_after.specialist_roles == ()


def test_approve_already_approved_request_is_truthful_noop(tmp_path: Path):
    snapshot = _snapshot()
    registry = _registry(tmp_path, snapshot=snapshot)
    service = HireApprovalService(registry)
    pending = service.request_sensitive_hire(
        snapshot,
        "security_agent",
        "Auth and secrets are in scope.",
        "logical_hiring_pm_hint",
    )
    service.apply_decision(
        snapshot,
        HireApprovalDecision(
            request_id=pending.request_id,
            decision="approve",
            actor_user_id=101,
        ),
    )

    second = service.apply_decision(
        snapshot,
        HireApprovalDecision(
            request_id=pending.request_id,
            decision="approve",
            actor_user_id=101,
        ),
    )

    assert second.status == "already_applied"
    assert second.roster_after.specialist_roles == ("security_agent",)


def test_reject_already_rejected_request_is_truthful_noop(tmp_path: Path):
    snapshot = _snapshot()
    registry = _registry(tmp_path, snapshot=snapshot)
    service = HireApprovalService(registry)
    pending = service.request_sensitive_hire(
        snapshot,
        "security_agent",
        "Auth and secrets are in scope.",
        "logical_hiring_pm_hint",
    )
    service.apply_decision(
        snapshot,
        HireApprovalDecision(
            request_id=pending.request_id,
            decision="reject",
            actor_user_id=101,
        ),
    )

    second = service.apply_decision(
        snapshot,
        HireApprovalDecision(
            request_id=pending.request_id,
            decision="reject",
            actor_user_id=101,
        ),
    )

    assert second.status == "rejected"
    assert second.roster_after.specialist_roles == ()


def test_request_does_not_leak_across_projects(tmp_path: Path):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    alpha_snapshot = _snapshot()
    beta_snapshot = ProjectSnapshot(
        project=_project(
            project_id="beta_project",
            slug="beta-project",
            name="Beta Project",
        ),
        policy=_policy(project_id="beta_project"),
    )
    registry.register_project(alpha_snapshot)
    registry.register_project(beta_snapshot)
    service = HireApprovalService(registry)
    pending = service.request_sensitive_hire(
        alpha_snapshot,
        "security_agent",
        "Auth and secrets are in scope.",
        "logical_hiring_pm_hint",
    )

    result = service.apply_decision(
        beta_snapshot,
        HireApprovalDecision(
            request_id=pending.request_id,
            decision="approve",
            actor_user_id=101,
        ),
    )

    assert result.status == "not_found"
    assert registry.get_project_specialist_roster("alpha_project").specialist_roles == ()


def test_non_owner_cannot_apply_decision(tmp_path: Path):
    snapshot = _snapshot()
    registry = _registry(tmp_path, snapshot=snapshot)
    service = HireApprovalService(registry)
    pending = service.request_sensitive_hire(
        snapshot,
        "security_agent",
        "Auth and secrets are in scope.",
        "logical_hiring_pm_hint",
    )

    with pytest.raises(ValueError, match="hire_approval_requires_owner"):
        service.apply_decision(
            snapshot,
            HireApprovalDecision(
                request_id=pending.request_id,
                decision="approve",
                actor_user_id=999,
            ),
        )


def test_hire_approval_result_non_approved_status_forbids_roster_change():
    before = ProjectSpecialistRoster(
        project_id="alpha_project",
        specialist_roles=(),
    )
    after = ProjectSpecialistRoster(
        project_id="alpha_project",
        specialist_roles=("security_agent",),
    )

    with pytest.raises(
        ValueError,
        match="non_approved_hire_result_forbids_roster_change",
    ):
        HireApprovalResult(
            project_id="alpha_project",
            request_id="hire-1-abc",
            status="rejected",
            roster_before=before,
            roster_after=after,
            message_text="bad",
        )


def test_pending_hire_request_rejects_baseline_role():
    with pytest.raises(ValueError, match="unknown_specialist_role:writer_agent"):
        PendingHireRequest(
            request_id="hire-1-abc",
            project_id="alpha_project",
            specialist_role="writer_agent",
            reason="bad",
            source="logical_hiring_pm_hint",
            status="pending",
            created_at=1000.0,
        )
