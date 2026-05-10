from __future__ import annotations

from pathlib import Path

import pytest

from core.project_chat_binding_service import ProjectChatBindingService
from core.project_migration_service import (
    ProjectMigrationService,
    ProjectMigrationStatus,
)
from core.project_models import Project, ProjectChatBinding, ProjectPolicy
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.project_runtime import ProjectRuntimeBinding
from core.state_db import StateDB


def _make_db(tmp_path: Path) -> StateDB:
    return StateDB(tmp_path / "state.db")


def _git_repo(tmp_path: Path, name: str) -> Path:
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
        "worktree_root": repo_path.parent / f"{repo_path.name}-worktrees",
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


def _binding(**overrides: object) -> ProjectChatBinding:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "chat_provider": "telegram",
        "chat_id": -100123450801,
    }
    data.update(overrides)
    return ProjectChatBinding(**data)


def _snapshot(
    repo_path: Path | None = None,
    *,
    with_runtime_binding: bool = True,
    with_chat_binding: bool = False,
    chat_id: int = -100123450801,
    **project_overrides: object,
) -> ProjectSnapshot:
    project = _project(**project_overrides)
    data: dict[str, object] = {
        "project": project,
        "policy": _policy(project_id=project.project_id),
    }
    if with_runtime_binding:
        assert repo_path is not None
        data["runtime_binding"] = _runtime_binding(
            repo_path,
            project_id=project.project_id,
            adapter_name=f"{project.project_id}_adapter",
        )
    if with_chat_binding:
        data["chat_binding"] = _binding(
            project_id=project.project_id,
            chat_id=chat_id,
        )
    return ProjectSnapshot(**data)


def _register(
    registry: ProjectRegistry,
    snapshot: ProjectSnapshot,
) -> ProjectSnapshot:
    registry.register_project(snapshot)
    loaded = registry.get_project_snapshot(snapshot.project.project_id)
    assert loaded is not None
    return loaded


def _service(
    registry: ProjectRegistry,
    owner_user_ids: tuple[int, ...] = (101,),
) -> ProjectMigrationService:
    return ProjectMigrationService(
        registry,
        ProjectChatBindingService(registry, owner_user_ids),
        owner_user_ids,
    )


def test_project_migration_service_construction_happy_path(tmp_path: Path):
    service = _service(ProjectRegistry(_make_db(tmp_path)), (202, 101, 202))

    assert service.owner_user_ids == (101, 202)


def test_project_migration_service_rejects_bad_registry(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    binding_service = ProjectChatBindingService(registry, (101,))

    with pytest.raises(ValueError, match="invalid_project_registry_type"):
        ProjectMigrationService("bad", binding_service, (101,))  # type: ignore[arg-type]


def test_project_migration_service_rejects_bad_chat_binding_service(
    tmp_path: Path,
):
    registry = ProjectRegistry(_make_db(tmp_path))

    with pytest.raises(
        ValueError,
        match="invalid_project_chat_binding_service_type",
    ):
        ProjectMigrationService(
            registry,
            "bad",  # type: ignore[arg-type]
            (101,),
        )


def test_project_migration_service_rejects_bad_owner_user_ids(
    tmp_path: Path,
):
    registry = ProjectRegistry(_make_db(tmp_path))
    binding_service = ProjectChatBindingService(registry, (101,))

    with pytest.raises(ValueError, match="owner_user_ids_must_be_tuple"):
        ProjectMigrationService(
            registry,
            binding_service,
            [101],  # type: ignore[arg-type]
        )


def test_project_migration_service_rejects_empty_owner_user_ids(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    binding_service = ProjectChatBindingService(registry, (101,))

    with pytest.raises(ValueError, match="empty_owner_user_ids"):
        ProjectMigrationService(registry, binding_service, ())


@pytest.mark.parametrize("bad", ["101", True, 0, -1])
def test_project_migration_service_rejects_invalid_owner_user_ids(
    tmp_path: Path,
    bad: object,
):
    registry = ProjectRegistry(_make_db(tmp_path))
    binding_service = ProjectChatBindingService(registry, (101,))

    with pytest.raises(ValueError, match="invalid_owner_user_id"):
        ProjectMigrationService(
            registry,
            binding_service,
            (bad,),  # type: ignore[arg-type]
        )


def test_project_migration_status_happy_path_migratable(tmp_path: Path):
    repo = _git_repo(tmp_path, "status-migratable")
    snapshot = _snapshot(repo)

    status = ProjectMigrationStatus(
        snapshot=snapshot,
        chat_provider="telegram",
        chat_id=-100123450801,
        actor_user_id=101,
        is_owner_user=True,
        is_group_chat=True,
        can_migrate_here=True,
    )

    assert status.can_migrate_here is True


def test_project_migration_status_happy_path_non_migratable():
    status = ProjectMigrationStatus(
        snapshot=None,
        chat_provider="telegram",
        chat_id=-100123450801,
        actor_user_id=101,
        is_owner_user=True,
        is_group_chat=True,
        can_migrate_here=False,
        reason="no_migratable_project",
    )

    assert status.reason == "no_migratable_project"


def test_project_migration_status_rejects_bad_snapshot():
    with pytest.raises(ValueError, match="invalid_project_snapshot_type"):
        ProjectMigrationStatus(
            snapshot="bad",  # type: ignore[arg-type]
            chat_provider="telegram",
            chat_id=-100123450801,
            actor_user_id=101,
            is_owner_user=True,
            is_group_chat=True,
            can_migrate_here=False,
            reason="no_migratable_project",
        )


@pytest.mark.parametrize("bad", ["", "   ", "slack"])
def test_project_migration_status_rejects_bad_provider(bad: object):
    with pytest.raises(ValueError, match="chat_provider"):
        ProjectMigrationStatus(
            snapshot=None,
            chat_provider=bad,  # type: ignore[arg-type]
            chat_id=-100123450801,
            actor_user_id=101,
            is_owner_user=True,
            is_group_chat=True,
            can_migrate_here=False,
            reason="no_migratable_project",
        )


@pytest.mark.parametrize("bad", ["1", True, 0])
def test_project_migration_status_rejects_bad_chat_id(bad: object):
    with pytest.raises(ValueError, match="invalid_chat_id"):
        ProjectMigrationStatus(
            snapshot=None,
            chat_provider="telegram",
            chat_id=bad,  # type: ignore[arg-type]
            actor_user_id=101,
            is_owner_user=True,
            is_group_chat=True,
            can_migrate_here=False,
            reason="no_migratable_project",
        )


@pytest.mark.parametrize("bad", ["101", True, 0])
def test_project_migration_status_rejects_bad_actor_user_id(bad: object):
    with pytest.raises(ValueError, match="invalid_actor_user_id"):
        ProjectMigrationStatus(
            snapshot=None,
            chat_provider="telegram",
            chat_id=-100123450801,
            actor_user_id=bad,  # type: ignore[arg-type]
            is_owner_user=True,
            is_group_chat=True,
            can_migrate_here=False,
            reason="no_migratable_project",
        )


@pytest.mark.parametrize(
    ("field_name", "kwargs", "match"),
    [
        ("is_owner_user", {"is_owner_user": "yes"}, "invalid_is_owner_user_type"),
        ("is_group_chat", {"is_group_chat": "yes"}, "invalid_is_group_chat_type"),
        (
            "can_migrate_here",
            {"can_migrate_here": "yes"},
            "invalid_can_migrate_here_type",
        ),
    ],
)
def test_project_migration_status_rejects_bad_bool_fields(
    field_name: str,
    kwargs: dict[str, object],
    match: str,
):
    data = {
        "snapshot": None,
        "chat_provider": "telegram",
        "chat_id": -100123450801,
        "actor_user_id": 101,
        "is_owner_user": True,
        "is_group_chat": True,
        "can_migrate_here": False,
        "reason": "no_migratable_project",
    }
    data.update(kwargs)

    with pytest.raises(ValueError, match=match):
        ProjectMigrationStatus(**data)  # type: ignore[arg-type]


def test_project_migration_status_rejects_migratable_without_snapshot():
    with pytest.raises(ValueError, match="migratable_status_requires_snapshot"):
        ProjectMigrationStatus(
            snapshot=None,
            chat_provider="telegram",
            chat_id=-100123450801,
            actor_user_id=101,
            is_owner_user=True,
            is_group_chat=True,
            can_migrate_here=True,
        )


def test_project_migration_status_rejects_non_migratable_without_reason(
    tmp_path: Path,
):
    repo = _git_repo(tmp_path, "status-no-reason")
    snapshot = _snapshot(repo)

    with pytest.raises(ValueError, match="non_migratable_status_requires_reason"):
        ProjectMigrationStatus(
            snapshot=snapshot,
            chat_provider="telegram",
            chat_id=-100123450801,
            actor_user_id=101,
            is_owner_user=True,
            is_group_chat=True,
            can_migrate_here=False,
        )


def test_get_migration_status_single_unbound_runtime_bound_project_is_migratable(
    tmp_path: Path,
):
    registry = ProjectRegistry(_make_db(tmp_path))
    repo = _git_repo(tmp_path, "migratable")
    _register(registry, _snapshot(repo))
    service = _service(registry)

    status = service.get_migration_status(
        chat_provider="telegram",
        chat_id=-100123450801,
        actor_user_id=101,
    )

    assert status.can_migrate_here is True
    assert status.snapshot is not None
    assert status.snapshot.project.project_id == "alpha_project"


def test_get_migration_status_non_owner_requires_owner(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    repo = _git_repo(tmp_path, "non-owner")
    _register(registry, _snapshot(repo))
    service = _service(registry)

    status = service.get_migration_status(
        chat_provider="telegram",
        chat_id=-100123450801,
        actor_user_id=999,
    )

    assert status.can_migrate_here is False
    assert status.reason == "migration_requires_owner_user"


def test_get_migration_status_dm_requires_group_chat(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    repo = _git_repo(tmp_path, "dm")
    _register(registry, _snapshot(repo))
    service = _service(registry)

    status = service.get_migration_status(
        chat_provider="telegram",
        chat_id=101,
        actor_user_id=101,
    )

    assert status.can_migrate_here is False
    assert status.reason == "migration_requires_group_chat"


def test_get_migration_status_already_bound_chat(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    repo = _git_repo(tmp_path, "already-bound")
    _register(
        registry,
        _snapshot(repo, with_chat_binding=True, chat_id=-100123450801),
    )
    service = _service(registry)

    status = service.get_migration_status(
        chat_provider="telegram",
        chat_id=-100123450801,
        actor_user_id=101,
    )

    assert status.can_migrate_here is False
    assert status.reason == "chat_already_bound"
    assert status.snapshot is not None


def test_get_migration_status_zero_projects(tmp_path: Path):
    service = _service(ProjectRegistry(_make_db(tmp_path)))

    status = service.get_migration_status(
        chat_provider="telegram",
        chat_id=-100123450801,
        actor_user_id=101,
    )

    assert status.can_migrate_here is False
    assert status.reason == "no_migratable_project"


def test_get_migration_status_multiple_projects_require_bind(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    alpha_repo = _git_repo(tmp_path, "multi-alpha")
    beta_repo = _git_repo(tmp_path, "multi-beta")
    _register(registry, _snapshot(alpha_repo))
    _register(
        registry,
        _snapshot(
            beta_repo,
            project_id="beta_project",
            slug="beta-project",
            name="Beta Project",
            owner_user_id=202,
        ),
    )
    service = _service(registry)

    status = service.get_migration_status(
        chat_provider="telegram",
        chat_id=-100123450801,
        actor_user_id=101,
    )

    assert status.can_migrate_here is False
    assert status.reason == "multiple_projects_require_projects_bind"


def test_get_migration_status_single_project_without_runtime_binding(
    tmp_path: Path,
):
    registry = ProjectRegistry(_make_db(tmp_path))
    repo = _git_repo(tmp_path, "no-runtime")
    _register(registry, _snapshot(repo, with_runtime_binding=False))
    service = _service(registry)

    status = service.get_migration_status(
        chat_provider="telegram",
        chat_id=-100123450801,
        actor_user_id=101,
    )

    assert status.can_migrate_here is False
    assert status.reason == "project_missing_runtime_binding"
    assert status.snapshot is not None


def test_get_migration_status_single_project_already_bound_elsewhere(
    tmp_path: Path,
):
    registry = ProjectRegistry(_make_db(tmp_path))
    repo = _git_repo(tmp_path, "bound-elsewhere")
    _register(
        registry,
        _snapshot(repo, with_chat_binding=True, chat_id=-100123450999),
    )
    service = _service(registry)

    status = service.get_migration_status(
        chat_provider="telegram",
        chat_id=-100123450801,
        actor_user_id=101,
    )

    assert status.can_migrate_here is False
    assert status.reason == "project_already_bound_to_other_chat"
    assert status.snapshot is not None


def test_migrate_current_chat_successfully_binds_current_chat(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    repo = _git_repo(tmp_path, "migrate-success")
    _register(registry, _snapshot(repo))
    service = _service(registry)

    snapshot = service.migrate_current_chat(
        chat_provider="telegram",
        chat_id=-100123450801,
        actor_user_id=101,
    )

    assert snapshot.chat_binding is not None
    assert snapshot.chat_binding.chat_id == -100123450801
    assert registry.get_project_snapshot_for_chat("telegram", -100123450801) is not None


def test_migrate_current_chat_second_attempt_reports_already_bound(
    tmp_path: Path,
):
    registry = ProjectRegistry(_make_db(tmp_path))
    repo = _git_repo(tmp_path, "migrate-twice")
    _register(registry, _snapshot(repo))
    service = _service(registry)

    service.migrate_current_chat(
        chat_provider="telegram",
        chat_id=-100123450801,
        actor_user_id=101,
    )

    with pytest.raises(ValueError, match="chat_already_bound"):
        service.migrate_current_chat(
            chat_provider="telegram",
            chat_id=-100123450801,
            actor_user_id=101,
        )


def test_migrate_current_chat_rejects_multiple_projects(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    alpha_repo = _git_repo(tmp_path, "migrate-multi-alpha")
    beta_repo = _git_repo(tmp_path, "migrate-multi-beta")
    _register(registry, _snapshot(alpha_repo))
    _register(
        registry,
        _snapshot(
            beta_repo,
            project_id="beta_project",
            slug="beta-project",
            name="Beta Project",
            owner_user_id=202,
        ),
    )
    service = _service(registry)

    with pytest.raises(
        ValueError,
        match="multiple_projects_require_projects_bind",
    ):
        service.migrate_current_chat(
            chat_provider="telegram",
            chat_id=-100123450801,
            actor_user_id=101,
        )


def test_migrate_current_chat_rejects_dm(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    repo = _git_repo(tmp_path, "migrate-dm")
    _register(registry, _snapshot(repo))
    service = _service(registry)

    with pytest.raises(ValueError, match="migration_requires_group_chat"):
        service.migrate_current_chat(
            chat_provider="telegram",
            chat_id=101,
            actor_user_id=101,
        )


def test_migrate_current_chat_rejects_non_owner(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    repo = _git_repo(tmp_path, "migrate-non-owner")
    _register(registry, _snapshot(repo))
    service = _service(registry)

    with pytest.raises(ValueError, match="migration_requires_owner_user"):
        service.migrate_current_chat(
            chat_provider="telegram",
            chat_id=-100123450801,
            actor_user_id=999,
        )
