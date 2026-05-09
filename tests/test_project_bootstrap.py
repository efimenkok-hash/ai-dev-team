"""Tests for core.project_bootstrap."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.project_bootstrap import (
    ProjectBootstrapResult,
    _build_legacy_project_snapshot,
    _derive_legacy_project_identity,
    build_project_bootstrap_result,
)
from core.project_models import Project, ProjectPolicy
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.project_runtime import ProjectRuntimeBinding
from core.state_db import StateDB


def _make_db(tmp_path: Path) -> StateDB:
    return StateDB(tmp_path / "state.db")


def _git_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    return repo


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
        "forbidden_paths": (),
        "forbidden_tokens": (),
    }
    data.update(overrides)
    return ProjectRuntimeBinding(**data)


def _snapshot(repo_path: Path, **overrides: object) -> ProjectSnapshot:
    data: dict[str, object] = {
        "project": _project(),
        "policy": _policy(),
        "runtime_binding": _runtime_binding(repo_path),
    }
    data.update(overrides)
    return ProjectSnapshot(**data)


def _env(repo_path: Path | None = None, **overrides: str) -> dict[str, str]:
    env: dict[str, str] = {"TELEGRAM_OWNER_CHAT_ID": "101"}
    if repo_path is not None:
        env["REPO_PATH"] = str(repo_path)
    env.update(overrides)
    return env


def test_project_bootstrap_result_is_frozen(tmp_path: Path):
    repo = _git_repo(tmp_path)
    result = ProjectBootstrapResult(
        registry=None,
        active_snapshot=_build_legacy_project_snapshot(_env(repo)),
        source="legacy_env_ephemeral",
    )

    with pytest.raises(Exception):
        result.source = "none"  # type: ignore[misc]


def test_build_project_bootstrap_result_rejects_non_mapping_env():
    with pytest.raises(ValueError, match="env_must_be_mapping"):
        build_project_bootstrap_result("bad", None)  # type: ignore[arg-type]


def test_build_project_bootstrap_result_rejects_invalid_state_db_type():
    with pytest.raises(ValueError, match="invalid_state_db_type"):
        build_project_bootstrap_result({}, "bad")  # type: ignore[arg-type]


def test_registry_with_one_project_and_runtime_binding_returns_active_snapshot(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    snapshot = _snapshot(_git_repo(tmp_path))
    registry.register_project(snapshot)

    result = build_project_bootstrap_result({}, db)

    assert result.registry is not None
    assert result.active_snapshot == snapshot
    assert result.source == "registry"
    assert result.reason is None


def test_registry_with_one_project_without_runtime_binding_returns_reason(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    snapshot = ProjectSnapshot(project=_project(), policy=_policy())
    registry.register_project(snapshot)

    result = build_project_bootstrap_result({}, db)

    assert result.registry is not None
    assert result.active_snapshot is None
    assert result.source == "registry"
    assert result.reason == "active_project_missing_runtime_binding"


def test_registry_with_multiple_projects_requires_explicit_binding(tmp_path: Path):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    registry.register_project(_snapshot(_git_repo(tmp_path, "repo-one")))
    registry.register_project(
        _snapshot(
            _git_repo(tmp_path, "repo-two"),
            project=_project(
                project_id="beta_project",
                slug="beta-project",
                name="Beta Project",
                owner_user_id=202,
            ),
            policy=_policy(project_id="beta_project"),
            runtime_binding=_runtime_binding(
                _git_repo(tmp_path, "repo-two"),
                project_id="beta_project",
                adapter_name="beta_adapter",
            ),
        )
    )

    result = build_project_bootstrap_result({}, db)

    assert result.registry is not None
    assert result.active_snapshot is None
    assert result.source == "registry"
    assert result.reason == "multiple_projects_require_explicit_binding"


def test_empty_registry_and_valid_legacy_env_seeds_project(tmp_path: Path):
    db = _make_db(tmp_path)
    repo = _git_repo(tmp_path, "legacy-repo")

    result = build_project_bootstrap_result(_env(repo), db)

    assert result.registry is not None
    assert result.active_snapshot is not None
    assert result.source == "legacy_env_seeded"
    assert result.reason is None
    loaded = result.registry.get_project_snapshot(
        result.active_snapshot.project.project_id
    )
    assert loaded == result.active_snapshot
    assert db.list_projects() == [result.active_snapshot.project]


def test_empty_registry_and_invalid_legacy_env_returns_no_active_snapshot(
    tmp_path: Path,
):
    db = _make_db(tmp_path)

    result = build_project_bootstrap_result({}, db)

    assert result.registry is not None
    assert result.active_snapshot is None
    assert result.source == "none"
    assert result.reason == "legacy_repo_path_missing"


def test_no_state_db_and_valid_legacy_env_returns_ephemeral_snapshot(tmp_path: Path):
    repo = _git_repo(tmp_path, "ephemeral-repo")

    result = build_project_bootstrap_result(_env(repo), None)

    assert result.registry is None
    assert result.active_snapshot is not None
    assert result.active_snapshot.runtime_binding is not None
    assert result.source == "legacy_env_ephemeral"
    assert result.reason is None


def test_derive_legacy_project_identity_normalizes_repo_name(tmp_path: Path):
    repo = _git_repo(tmp_path, "My Awesome Repo")

    project_id, slug, adapter_name = _derive_legacy_project_identity(repo)

    assert project_id == "my_awesome_repo"
    assert slug == "my-awesome-repo"
    assert adapter_name == "my_awesome_repo_adapter"


def test_derive_legacy_project_identity_uses_deterministic_fallback(tmp_path: Path):
    repo = _git_repo(tmp_path, "---")

    project_id, slug, adapter_name = _derive_legacy_project_identity(repo)

    assert project_id == "default_project"
    assert slug == "default-project"
    assert adapter_name == "default_adapter"


def test_build_legacy_project_snapshot_respects_worktree_root(tmp_path: Path):
    repo = _git_repo(tmp_path, "worktree-repo")
    worktree_root = tmp_path / "custom-worktrees"

    snapshot = _build_legacy_project_snapshot(
        _env(repo, WORKTREE_ROOT=str(worktree_root))
    )

    assert snapshot.runtime_binding is not None
    assert snapshot.runtime_binding.worktree_root == worktree_root.resolve()


def test_build_project_bootstrap_result_accepts_multi_owner_legacy_bootstrap(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    repo = _git_repo(tmp_path, "owner-repo")

    result = build_project_bootstrap_result(
        _env(repo, TELEGRAM_OWNER_CHAT_ID="101,202"),
        db,
    )

    assert result.registry is not None
    assert result.active_snapshot is not None
    assert result.source == "legacy_env_seeded"
    assert result.active_snapshot.project.owner_user_id == 101


def test_build_project_bootstrap_result_rejects_invalid_legacy_owner_ids(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    repo = _git_repo(tmp_path, "owner-invalid-repo")

    result = build_project_bootstrap_result(
        _env(repo, TELEGRAM_OWNER_CHAT_ID="101,bad"),
        db,
    )

    assert result.registry is not None
    assert result.active_snapshot is None
    assert result.source == "none"
    assert result.reason == "legacy_owner_user_id_invalid"
