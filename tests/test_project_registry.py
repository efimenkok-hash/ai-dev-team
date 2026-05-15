"""Tests for core.project_registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.hire_approval import PendingHireRequest
from core.project_models import (
    Project,
    ProjectChatBinding,
    ProjectMembership,
    ProjectPolicy,
)
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.project_runtime import ProjectRuntimeBinding
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


def _membership(**overrides: object) -> ProjectMembership:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "member_id": "coordinator_01",
        "member_type": "agent",
        "role_name": "coordinator_agent",
        "status": "active",
    }
    data.update(overrides)
    return ProjectMembership(**data)


def _binding(**overrides: object) -> ProjectChatBinding:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "chat_id": -1001234567890,
        "chat_provider": "telegram",
    }
    data.update(overrides)
    return ProjectChatBinding(**data)


def _git_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir(exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    return repo


def _runtime_binding(repo_path: Path, **overrides: object) -> ProjectRuntimeBinding:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "adapter_name": "alpha_adapter",
        "repo_path": repo_path,
        "worktree_root": repo_path.parent / "worktrees",
        "base_branch": "main",
        "branch_prefix": "feature/",
        "language": "python",
        "rules": (),
        "commands": (),
        "forbidden_paths": ("secrets/",),
        "forbidden_tokens": ("API_KEY",),
    }
    data.update(overrides)
    return ProjectRuntimeBinding(**data)


def _snapshot(**overrides: object) -> ProjectSnapshot:
    data: dict[str, object] = {
        "project": _project(),
        "policy": _policy(),
        "memberships": (
            _membership(member_id="writer_01", role_name="writer_agent"),
            _membership(member_id="architect_01", role_name="architect_agent"),
        ),
        "chat_binding": _binding(),
    }
    data.update(overrides)
    return ProjectSnapshot(**data)


def _pending_hire_request(**overrides: object) -> PendingHireRequest:
    data: dict[str, object] = {
        "request_id": "hire-1000-abcd1234",
        "project_id": "alpha_project",
        "specialist_role": "security_agent",
        "reason": "Auth and secrets are in scope.",
        "source": "logical_hiring_pm_hint",
        "status": "pending",
        "created_at": 1000.0,
    }
    data.update(overrides)
    return PendingHireRequest(**data)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_registry_accepts_state_db(tmp_path: Path):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    assert registry.state_db is db


def test_registry_rejects_non_state_db():
    with pytest.raises(ValueError, match="invalid_state_db_type"):
        ProjectRegistry("bad")  # type: ignore[arg-type]


def test_project_snapshot_happy_path_normalizes_membership_order():
    snapshot = _snapshot()
    assert snapshot.memberships == (
        _membership(member_id="architect_01", role_name="architect_agent"),
        _membership(member_id="writer_01", role_name="writer_agent"),
    )


def test_project_snapshot_is_frozen():
    snapshot = _snapshot()
    with pytest.raises(Exception):
        snapshot.project = _project(project_id="beta_project", slug="beta-project")  # type: ignore[misc]


def test_project_snapshot_rejects_bad_project():
    with pytest.raises(ValueError, match="invalid_project_type"):
        ProjectSnapshot(project="bad")  # type: ignore[arg-type]


def test_project_snapshot_rejects_bad_policy():
    with pytest.raises(ValueError, match="invalid_project_policy_type"):
        ProjectSnapshot(project=_project(), policy="bad")  # type: ignore[arg-type]


def test_project_snapshot_rejects_bad_chat_binding():
    with pytest.raises(ValueError, match="invalid_project_chat_binding_type"):
        ProjectSnapshot(project=_project(), chat_binding="bad")  # type: ignore[arg-type]


def test_project_snapshot_rejects_bad_runtime_binding(tmp_path: Path):
    with pytest.raises(ValueError, match="invalid_project_runtime_binding_type"):
        ProjectSnapshot(
            project=_project(),
            runtime_binding="bad",  # type: ignore[arg-type]
        )


def test_project_snapshot_rejects_non_tuple_memberships():
    with pytest.raises(ValueError, match="memberships_must_be_tuple"):
        ProjectSnapshot(
            project=_project(),
            memberships=[_membership()],  # type: ignore[arg-type]
        )


def test_project_snapshot_rejects_bad_membership_item():
    with pytest.raises(ValueError, match="invalid_project_membership_type"):
        ProjectSnapshot(
            project=_project(),
            memberships=(_membership(), "bad"),  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Snapshot consistency
# ---------------------------------------------------------------------------


def test_project_snapshot_rejects_policy_project_id_mismatch():
    with pytest.raises(ValueError, match="project_policy_project_id_mismatch"):
        ProjectSnapshot(
            project=_project(project_id="alpha_project", slug="alpha-project"),
            policy=_policy(project_id="beta_project"),
        )


def test_project_snapshot_rejects_chat_binding_project_id_mismatch():
    with pytest.raises(ValueError, match="project_chat_binding_project_id_mismatch"):
        ProjectSnapshot(
            project=_project(project_id="alpha_project", slug="alpha-project"),
            chat_binding=_binding(project_id="beta_project"),
        )


def test_project_snapshot_rejects_runtime_binding_project_id_mismatch(tmp_path: Path):
    with pytest.raises(ValueError, match="project_runtime_binding_project_id_mismatch"):
        ProjectSnapshot(
            project=_project(project_id="alpha_project", slug="alpha-project"),
            runtime_binding=_runtime_binding(
                _git_repo(tmp_path),
                project_id="beta_project",
            ),
        )


def test_project_snapshot_rejects_membership_project_id_mismatch():
    with pytest.raises(ValueError, match="project_membership_project_id_mismatch"):
        ProjectSnapshot(
            project=_project(project_id="alpha_project", slug="alpha-project"),
            memberships=(_membership(project_id="beta_project"),),
        )


def test_project_snapshot_rejects_duplicate_member_id():
    with pytest.raises(ValueError, match="duplicate_project_member_id:writer_01"):
        ProjectSnapshot(
            project=_project(),
            memberships=(
                _membership(member_id="writer_01", role_name="writer_agent"),
                _membership(member_id="writer_01", role_name="architect_agent"),
            ),
        )


def test_project_snapshot_memberships_order_is_stable():
    snapshot = ProjectSnapshot(
        project=_project(),
        memberships=(
            _membership(member_id="writer_01", role_name="writer_agent"),
            _membership(member_id="architect_01", role_name="architect_agent"),
            _membership(member_id="coordinator_01", role_name="coordinator_agent"),
        ),
    )

    assert [membership.member_id for membership in snapshot.memberships] == [
        "architect_01",
        "coordinator_01",
        "writer_01",
    ]


# ---------------------------------------------------------------------------
# Registry registration
# ---------------------------------------------------------------------------


def test_register_project_saves_full_snapshot(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    snapshot = _snapshot(runtime_binding=_runtime_binding(_git_repo(tmp_path)))

    registry.register_project(snapshot)

    assert registry.get_project_snapshot("alpha_project") == snapshot


def test_registry_returns_empty_project_specialist_roster_for_new_project(
    tmp_path: Path,
):
    registry = ProjectRegistry(_make_db(tmp_path))
    registry.register_project(_snapshot(runtime_binding=_runtime_binding(_git_repo(tmp_path))))

    roster = registry.get_project_specialist_roster("alpha_project")

    assert roster == ProjectSpecialistRoster(
        project_id="alpha_project",
        specialist_roles=(),
    )


def test_registry_add_and_remove_project_specialist_return_updated_roster(
    tmp_path: Path,
):
    registry = ProjectRegistry(_make_db(tmp_path))
    registry.register_project(_snapshot(runtime_binding=_runtime_binding(_git_repo(tmp_path))))

    added = registry.add_project_specialist("alpha_project", "security_agent")
    assert added.specialist_roles == ("security_agent",)

    removed = registry.remove_project_specialist(
        "alpha_project",
        "security_agent",
    )
    assert removed.specialist_roles == ()


def test_registry_project_specialist_roster_persists_across_instances(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    first = ProjectRegistry(db)
    first.register_project(_snapshot(runtime_binding=_runtime_binding(_git_repo(tmp_path))))
    first.add_project_specialist("alpha_project", "security_agent")
    first.add_project_specialist("alpha_project", "data_agent")

    second = ProjectRegistry(StateDB(db.path))

    assert second.get_project_specialist_roster("alpha_project").specialist_roles == (
        "security_agent",
        "data_agent",
    )


def test_registry_project_specialist_roster_is_isolated_per_project(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    registry.register_project(_snapshot(runtime_binding=_runtime_binding(_git_repo(tmp_path, "alpha"))))
    registry.register_project(
        _snapshot(
            project=_project(
                project_id="beta_project",
                slug="beta-project",
                name="Beta Project",
            ),
            policy=_policy(project_id="beta_project"),
            memberships=(),
            chat_binding=_binding(project_id="beta_project", chat_id=-100999),
            runtime_binding=_runtime_binding(
                _git_repo(tmp_path, "beta"),
                project_id="beta_project",
                adapter_name="beta_adapter",
            ),
        )
    )
    registry.add_project_specialist("alpha_project", "security_agent")

    assert registry.get_project_specialist_roster("alpha_project").specialist_roles == (
        "security_agent",
    )
    assert registry.get_project_specialist_roster("beta_project").specialist_roles == ()


def test_registry_pending_hire_request_round_trip_and_approval(
    tmp_path: Path,
):
    registry = ProjectRegistry(_make_db(tmp_path))
    registry.register_project(_snapshot(runtime_binding=_runtime_binding(_git_repo(tmp_path))))

    created = registry.create_pending_hire_request(_pending_hire_request())
    assert registry.get_hire_request(created.request_id) == created
    assert registry.list_pending_hire_requests("alpha_project") == (created,)

    approved = registry.approve_hire_request(created.request_id, 101)

    assert approved.status == "approved"
    assert registry.list_pending_hire_requests("alpha_project") == ()
    assert registry.get_project_specialist_roster("alpha_project").specialist_roles == (
        "security_agent",
    )


def test_registry_pending_hire_requests_persist_across_instances(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    first = ProjectRegistry(db)
    first.register_project(_snapshot(runtime_binding=_runtime_binding(_git_repo(tmp_path))))
    created = first.create_pending_hire_request(_pending_hire_request())

    second = ProjectRegistry(StateDB(db.path))

    assert second.list_pending_hire_requests("alpha_project") == (created,)


def test_register_project_saves_snapshot_without_policy(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    snapshot = ProjectSnapshot(
        project=_project(),
        memberships=(_membership(),),
        chat_binding=_binding(),
    )

    registry.register_project(snapshot)

    loaded = registry.get_project_snapshot("alpha_project")
    assert loaded == snapshot
    assert loaded is not None
    assert loaded.policy is None


def test_register_project_saves_snapshot_without_chat_binding(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    snapshot = ProjectSnapshot(
        project=_project(),
        policy=_policy(),
        memberships=(_membership(),),
    )

    registry.register_project(snapshot)

    loaded = registry.get_project_snapshot("alpha_project")
    assert loaded == snapshot
    assert loaded is not None
    assert loaded.chat_binding is None


def test_register_project_saves_snapshot_without_memberships(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    snapshot = ProjectSnapshot(
        project=_project(),
        policy=_policy(),
        chat_binding=_binding(),
    )

    registry.register_project(snapshot)

    loaded = registry.get_project_snapshot("alpha_project")
    assert loaded == snapshot
    assert loaded is not None
    assert loaded.memberships == ()


def test_register_project_rejects_duplicate_project_id(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    snapshot = _snapshot(runtime_binding=_runtime_binding(_git_repo(tmp_path)))

    registry.register_project(snapshot)

    with pytest.raises(ValueError, match="project_already_exists:alpha_project"):
        registry.register_project(snapshot)


def test_register_project_rejects_duplicate_slug(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    registry.register_project(_snapshot(runtime_binding=_runtime_binding(_git_repo(tmp_path, "alpha-repo"))))

    with pytest.raises(ValueError, match="project_slug_already_exists:alpha-project"):
        registry.register_project(
            _snapshot(
                project=_project(
                    project_id="beta_project",
                    slug="alpha-project",
                    name="Beta Project",
                    description="Secondary project.",
                    owner_user_id=202,
                ),
                policy=_policy(project_id="beta_project"),
                memberships=(
                    _membership(project_id="beta_project", member_id="beta_member"),
                ),
                chat_binding=_binding(project_id="beta_project", chat_id=-100222),
                runtime_binding=_runtime_binding(
                    _git_repo(tmp_path, "beta-repo"),
                    project_id="beta_project",
                ),
            )
        )


def test_register_project_rejects_conflicting_chat_binding(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    registry.register_project(_snapshot(runtime_binding=_runtime_binding(_git_repo(tmp_path, "alpha-repo"))))

    with pytest.raises(ValueError, match="chat_binding_conflict:telegram:-1001234567890"):
        registry.register_project(
            _snapshot(
                project=_project(
                    project_id="beta_project",
                    slug="beta-project",
                    name="Beta Project",
                    description="Secondary project.",
                    owner_user_id=202,
                ),
                policy=_policy(project_id="beta_project"),
                memberships=(
                    _membership(project_id="beta_project", member_id="beta_member"),
                ),
                chat_binding=_binding(project_id="beta_project"),
                runtime_binding=_runtime_binding(
                    _git_repo(tmp_path, "beta-repo"),
                    project_id="beta_project",
                ),
            )
        )


def test_register_project_rejects_bad_snapshot_type(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    with pytest.raises(ValueError, match="invalid_project_snapshot_type"):
        registry.register_project("bad")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


def test_register_project_does_not_leave_partial_state_on_failure(tmp_path: Path):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    registry.register_project(_snapshot(runtime_binding=_runtime_binding(_git_repo(tmp_path, "alpha-repo"))))

    with pytest.raises(ValueError, match="chat_binding_conflict:telegram:-1001234567890"):
        registry.register_project(
            ProjectSnapshot(
                project=_project(
                    project_id="beta_project",
                    slug="beta-project",
                    name="Beta Project",
                    description="Secondary project.",
                    owner_user_id=202,
                ),
                policy=_policy(project_id="beta_project", allow_agent_dm=True),
                memberships=(
                    _membership(
                        project_id="beta_project",
                        member_id="beta_member",
                        role_name="writer_agent",
                    ),
                ),
                chat_binding=_binding(
                    project_id="beta_project",
                    chat_id=-1001234567890,
                ),
                runtime_binding=_runtime_binding(
                    _git_repo(tmp_path, "beta-repo"),
                    project_id="beta_project",
                ),
            )
        )

    assert registry.get_project_snapshot("beta_project") is None
    assert db.get_project("beta_project") is None
    assert db.get_project_policy("beta_project") is None
    assert db.list_project_memberships("beta_project") == []
    assert db.get_project_chat_binding("beta_project") is None
    assert db.get_project_runtime_binding("beta_project") is None


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_get_project_snapshot_round_trip(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    snapshot = _snapshot(runtime_binding=_runtime_binding(_git_repo(tmp_path)))
    registry.register_project(snapshot)

    assert registry.get_project_snapshot("alpha_project") == snapshot


def test_get_project_snapshot_by_slug_round_trip(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    snapshot = _snapshot(runtime_binding=_runtime_binding(_git_repo(tmp_path)))
    registry.register_project(snapshot)

    assert registry.get_project_snapshot_by_slug("  Alpha-Project  ") == snapshot


def test_get_project_snapshot_for_chat_round_trip(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    snapshot = _snapshot(runtime_binding=_runtime_binding(_git_repo(tmp_path)))
    registry.register_project(snapshot)

    assert registry.get_project_snapshot_for_chat("telegram", -1001234567890) == snapshot


def test_get_project_snapshot_returns_none_for_unknown_project(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    assert registry.get_project_snapshot("missing_project") is None


def test_get_project_snapshot_by_slug_returns_none_for_unknown_slug(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    assert registry.get_project_snapshot_by_slug("missing-project") is None


def test_get_project_snapshot_for_chat_returns_none_for_unknown_chat(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    assert registry.get_project_snapshot_for_chat("telegram", -100404) is None


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_list_projects_order_is_stable(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    registry.register_project(
        ProjectSnapshot(
            project=_project(
                project_id="zeta_project",
                slug="zeta-project",
            )
        )
    )
    registry.register_project(
        ProjectSnapshot(
            project=_project(
                project_id="alpha_project",
                slug="alpha-project",
            )
        )
    )
    registry.register_project(
        ProjectSnapshot(
            project=_project(
                project_id="beta_project",
                slug="beta-project",
            )
        )
    )

    assert [project.project_id for project in registry.list_projects()] == [
        "alpha_project",
        "beta_project",
        "zeta_project",
    ]


def test_list_project_snapshots_order_is_stable_and_collects_optional_fields(
    tmp_path: Path,
):
    registry = ProjectRegistry(_make_db(tmp_path))
    registry.register_project(
        ProjectSnapshot(
            project=_project(project_id="beta_project", slug="beta-project"),
        )
    )
    registry.register_project(
        ProjectSnapshot(
            project=_project(project_id="alpha_project", slug="alpha-project"),
            policy=_policy(),
            memberships=(_membership(),),
            chat_binding=_binding(),
            runtime_binding=_runtime_binding(_git_repo(tmp_path)),
        )
    )

    snapshots = registry.list_project_snapshots()

    assert [snapshot.project.project_id for snapshot in snapshots] == [
        "alpha_project",
        "beta_project",
    ]
    assert snapshots[0].policy is not None
    assert snapshots[0].chat_binding is not None
    assert snapshots[0].memberships == (_membership(),)
    assert snapshots[0].runtime_binding == _runtime_binding(_git_repo(tmp_path))
    assert snapshots[1].policy is None
    assert snapshots[1].chat_binding is None
    assert snapshots[1].memberships == ()
    assert snapshots[1].runtime_binding is None


# ---------------------------------------------------------------------------
# Granular mutations
# ---------------------------------------------------------------------------


def test_set_project_policy_reflects_in_snapshot(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    registry.register_project(ProjectSnapshot(project=_project()))

    registry.set_project_policy(_policy(allow_agent_dm=True))

    snapshot = registry.get_project_snapshot("alpha_project")
    assert snapshot is not None
    assert snapshot.policy == _policy(allow_agent_dm=True)


def test_upsert_project_membership_reflects_in_snapshot(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    registry.register_project(ProjectSnapshot(project=_project()))

    registry.upsert_project_membership(
        _membership(member_id="writer_01", role_name="writer_agent")
    )

    snapshot = registry.get_project_snapshot("alpha_project")
    assert snapshot is not None
    assert snapshot.memberships == (
        _membership(member_id="writer_01", role_name="writer_agent"),
    )


def test_bind_project_chat_reflects_in_snapshot(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    registry.register_project(ProjectSnapshot(project=_project()))

    registry.bind_project_chat(_binding(chat_id=-1009876543210))

    snapshot = registry.get_project_snapshot("alpha_project")
    assert snapshot is not None
    assert snapshot.chat_binding == _binding(chat_id=-1009876543210)


def test_set_project_runtime_binding_reflects_in_snapshot(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    registry.register_project(ProjectSnapshot(project=_project()))
    binding = _runtime_binding(_git_repo(tmp_path), branch_prefix="task/")

    registry.set_project_runtime_binding(binding)

    snapshot = registry.get_project_snapshot("alpha_project")
    assert snapshot is not None
    assert snapshot.runtime_binding == binding


def test_get_project_runtime_binding_round_trip(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    registry.register_project(ProjectSnapshot(project=_project()))
    binding = _runtime_binding(_git_repo(tmp_path))
    registry.set_project_runtime_binding(binding)

    assert registry.get_project_runtime_binding("alpha_project") == binding


def test_full_aggregate_round_trip_with_runtime_binding(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    binding = _runtime_binding(_git_repo(tmp_path))
    snapshot = ProjectSnapshot(
        project=_project(),
        policy=_policy(allow_agent_dm=True),
        memberships=(
            _membership(member_id="writer_01", role_name="writer_agent"),
            _membership(member_id="architect_01", role_name="architect_agent"),
        ),
        chat_binding=_binding(chat_id=-1009876543210),
        runtime_binding=binding,
    )

    registry.register_project(snapshot)

    assert registry.get_project_snapshot("alpha_project") == snapshot


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_registry_read_methods_reject_bad_ids_and_types(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    with pytest.raises(ValueError, match="invalid_project_id"):
        registry.get_project_snapshot("bad-id")
    with pytest.raises(ValueError, match="invalid_slug"):
        registry.get_project_snapshot_by_slug("bad_slug")
    with pytest.raises(ValueError, match="invalid_chat_provider"):
        registry.get_project_snapshot_for_chat("discord", -1001)
    with pytest.raises(ValueError, match="invalid_chat_id"):
        registry.get_project_snapshot_for_chat("telegram", 0)


def test_registry_mutation_methods_reject_bad_public_input_types(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    with pytest.raises(ValueError, match="invalid_project_policy_type"):
        registry.set_project_policy("bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid_project_membership_type"):
        registry.upsert_project_membership("bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid_project_chat_binding_type"):
        registry.bind_project_chat("bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid_project_runtime_binding_type"):
        registry.set_project_runtime_binding("bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid_pending_hire_request_type"):
        registry.create_pending_hire_request("bad")  # type: ignore[arg-type]
