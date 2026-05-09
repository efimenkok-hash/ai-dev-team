from __future__ import annotations

from pathlib import Path

import pytest

from core.project_chat_binding_service import (
    ChatBindingStatus,
    ProjectBindingView,
    ProjectChatBindingService,
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
        "chat_id": -100123450001,
        "chat_provider": "telegram",
    }
    data.update(overrides)
    return ProjectChatBinding(**data)


def _snapshot(
    repo_path: Path | None = None,
    *,
    with_runtime_binding: bool = True,
    with_chat_binding: bool = False,
    chat_id: int = -100123450001,
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


def _register(registry: ProjectRegistry, snapshot: ProjectSnapshot) -> ProjectSnapshot:
    registry.register_project(snapshot)
    loaded = registry.get_project_snapshot(snapshot.project.project_id)
    assert loaded is not None
    return loaded


def test_service_construction_happy_path_and_normalizes_owner_ids(tmp_path: Path):
    service = ProjectChatBindingService(
        ProjectRegistry(_make_db(tmp_path)),
        (202, 101, 202),
    )

    assert service.owner_user_ids == (101, 202)


def test_service_rejects_bad_registry():
    with pytest.raises(ValueError, match="invalid_project_registry_type"):
        ProjectChatBindingService("bad", (101,))  # type: ignore[arg-type]


def test_service_rejects_non_tuple_owner_ids(tmp_path: Path):
    with pytest.raises(ValueError, match="owner_user_ids_must_be_tuple"):
        ProjectChatBindingService(
            ProjectRegistry(_make_db(tmp_path)),
            [101],  # type: ignore[arg-type]
        )


def test_service_rejects_empty_owner_ids(tmp_path: Path):
    with pytest.raises(ValueError, match="empty_owner_user_ids"):
        ProjectChatBindingService(ProjectRegistry(_make_db(tmp_path)), ())


@pytest.mark.parametrize("bad", ["101", True, 0, -1])
def test_service_rejects_invalid_owner_ids(tmp_path: Path, bad: object):
    with pytest.raises(ValueError, match="invalid_owner_user_id"):
        ProjectChatBindingService(
            ProjectRegistry(_make_db(tmp_path)),
            (bad,),  # type: ignore[arg-type]
        )


def test_project_binding_view_happy_path():
    view = ProjectBindingView(
        project=_project(),
        chat_binding=_binding(),
        has_runtime_binding=True,
    )

    assert view.project.project_id == "alpha_project"


def test_project_binding_view_rejects_bad_project():
    with pytest.raises(ValueError, match="invalid_project_type"):
        ProjectBindingView(
            project="bad",  # type: ignore[arg-type]
            chat_binding=None,
            has_runtime_binding=True,
        )


def test_project_binding_view_rejects_bad_chat_binding():
    with pytest.raises(ValueError, match="invalid_project_chat_binding_type"):
        ProjectBindingView(
            project=_project(),
            chat_binding="bad",  # type: ignore[arg-type]
            has_runtime_binding=True,
        )


def test_project_binding_view_rejects_non_bool_runtime_flag():
    with pytest.raises(ValueError, match="invalid_has_runtime_binding_type"):
        ProjectBindingView(
            project=_project(),
            chat_binding=None,
            has_runtime_binding="yes",  # type: ignore[arg-type]
        )


def test_project_binding_view_rejects_project_chat_binding_mismatch():
    with pytest.raises(
        ValueError,
        match="project_binding_view_project_id_mismatch",
    ):
        ProjectBindingView(
            project=_project(),
            chat_binding=_binding(project_id="beta_project"),
            has_runtime_binding=True,
        )


def test_chat_binding_status_happy_path_bound(tmp_path: Path):
    repo = _git_repo(tmp_path, "bound")
    snapshot = _snapshot(repo, with_chat_binding=True)

    status = ChatBindingStatus(
        chat_provider="telegram",
        chat_id=-100123450001,
        snapshot=snapshot,
    )

    assert status.snapshot == snapshot


def test_chat_binding_status_happy_path_unbound():
    status = ChatBindingStatus(
        chat_provider="telegram",
        chat_id=-100123450001,
        snapshot=None,
        reason="chat_not_bound",
    )

    assert status.reason == "chat_not_bound"


@pytest.mark.parametrize("bad", ["", "   ", "slack"])
def test_chat_binding_status_rejects_bad_provider(bad: object):
    with pytest.raises(ValueError, match="chat_provider"):
        ChatBindingStatus(
            chat_provider=bad,  # type: ignore[arg-type]
            chat_id=-100123450001,
            snapshot=None,
            reason="chat_not_bound",
        )


@pytest.mark.parametrize("bad", ["1", True, 0])
def test_chat_binding_status_rejects_bad_chat_id(bad: object):
    with pytest.raises(ValueError, match="invalid_chat_id"):
        ChatBindingStatus(
            chat_provider="telegram",
            chat_id=bad,  # type: ignore[arg-type]
            snapshot=None,
            reason="chat_not_bound",
        )


def test_chat_binding_status_rejects_snapshot_none_without_reason():
    with pytest.raises(ValueError, match="missing_chat_binding_reason"):
        ChatBindingStatus(
            chat_provider="telegram",
            chat_id=-100123450001,
            snapshot=None,
        )


def test_chat_binding_status_rejects_mismatched_chat_binding(tmp_path: Path):
    repo = _git_repo(tmp_path, "mismatch")
    snapshot = _snapshot(repo, with_chat_binding=True, chat_id=-100123450777)

    with pytest.raises(ValueError, match="chat_binding_status_chat_id_mismatch"):
        ChatBindingStatus(
            chat_provider="telegram",
            chat_id=-100123450001,
            snapshot=snapshot,
        )


def test_list_project_bindings_returns_stable_order_and_binding_state(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    alpha_repo = _git_repo(tmp_path, "alpha")
    beta_repo = _git_repo(tmp_path, "beta")
    zeta_repo = _git_repo(tmp_path, "zeta")
    _register(
        registry,
        _snapshot(
            zeta_repo,
            project_id="zeta_project",
            slug="zeta-project",
            name="Zeta Project",
            with_chat_binding=False,
        ),
    )
    _register(
        registry,
        _snapshot(
            alpha_repo,
            with_chat_binding=True,
            chat_id=-100123450111,
        ),
    )
    _register(
        registry,
        _snapshot(
            beta_repo,
            project_id="beta_project",
            slug="beta-project",
            name="Beta Project",
            with_runtime_binding=False,
        ),
    )
    service = ProjectChatBindingService(registry, (101,))

    views = service.list_project_bindings()

    assert [view.project.project_id for view in views] == [
        "alpha_project",
        "beta_project",
        "zeta_project",
    ]
    assert views[0].chat_binding is not None
    assert views[1].chat_binding is None
    assert views[0].has_runtime_binding is True
    assert views[1].has_runtime_binding is False


def test_get_chat_binding_status_bound_group_chat_returns_snapshot(tmp_path: Path):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    repo = _git_repo(tmp_path, "status-bound")
    snapshot = _register(
        registry,
        _snapshot(repo, with_chat_binding=True, chat_id=-100123450222),
    )
    service = ProjectChatBindingService(registry, (101,))

    status = service.get_chat_binding_status("telegram", -100123450222)

    assert status.snapshot == snapshot
    assert status.reason is None


def test_get_chat_binding_status_unbound_returns_reason(tmp_path: Path):
    service = ProjectChatBindingService(ProjectRegistry(_make_db(tmp_path)), (101,))

    status = service.get_chat_binding_status("telegram", -100123450333)

    assert status.snapshot is None
    assert status.reason == "chat_not_bound"


def test_bind_chat_to_project_by_project_id(tmp_path: Path):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    repo = _git_repo(tmp_path, "bind-by-id")
    _register(registry, _snapshot(repo))
    service = ProjectChatBindingService(registry, (101,))

    snapshot = service.bind_chat_to_project(
        chat_provider="telegram",
        chat_id=-100123450444,
        actor_user_id=101,
        project_ref="alpha_project",
    )

    assert snapshot.chat_binding is not None
    assert snapshot.chat_binding.chat_id == -100123450444


def test_bind_chat_to_project_by_slug(tmp_path: Path):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    repo = _git_repo(tmp_path, "bind-by-slug")
    _register(registry, _snapshot(repo))
    service = ProjectChatBindingService(registry, (101,))

    snapshot = service.bind_chat_to_project(
        chat_provider="telegram",
        chat_id=-100123450555,
        actor_user_id=101,
        project_ref="alpha-project",
    )

    assert snapshot.project.slug == "alpha-project"
    assert snapshot.chat_binding is not None
    assert snapshot.chat_binding.chat_id == -100123450555


def test_bind_chat_to_project_requires_owner(tmp_path: Path):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    repo = _git_repo(tmp_path, "bind-owner")
    _register(registry, _snapshot(repo))
    service = ProjectChatBindingService(registry, (101,))

    with pytest.raises(ValueError, match="binding_requires_owner_user"):
        service.bind_chat_to_project(
            chat_provider="telegram",
            chat_id=-100123450666,
            actor_user_id=202,
            project_ref="alpha_project",
        )


def test_bind_chat_to_project_rejects_positive_chat_id(tmp_path: Path):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    repo = _git_repo(tmp_path, "bind-positive")
    _register(registry, _snapshot(repo))
    service = ProjectChatBindingService(registry, (101,))

    with pytest.raises(ValueError, match="explicit_project_chat_must_be_group"):
        service.bind_chat_to_project(
            chat_provider="telegram",
            chat_id=12345,
            actor_user_id=101,
            project_ref="alpha_project",
        )


def test_bind_chat_to_project_rejects_missing_project(tmp_path: Path):
    service = ProjectChatBindingService(ProjectRegistry(_make_db(tmp_path)), (101,))

    with pytest.raises(ValueError, match="project_not_found"):
        service.bind_chat_to_project(
            chat_provider="telegram",
            chat_id=-100123450777,
            actor_user_id=101,
            project_ref="missing-project",
        )


def test_bind_chat_to_project_rejects_project_without_runtime_binding(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    repo = _git_repo(tmp_path, "bind-no-runtime")
    _register(registry, _snapshot(repo, with_runtime_binding=False))
    service = ProjectChatBindingService(registry, (101,))

    with pytest.raises(ValueError, match="project_missing_runtime_binding"):
        service.bind_chat_to_project(
            chat_provider="telegram",
            chat_id=-100123450888,
            actor_user_id=101,
            project_ref="alpha_project",
        )


def test_bind_chat_to_project_is_idempotent_for_same_chat_and_project(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    repo = _git_repo(tmp_path, "bind-idempotent")
    _register(
        registry,
        _snapshot(repo, with_chat_binding=True, chat_id=-100123450999),
    )
    service = ProjectChatBindingService(registry, (101,))

    first = service.bind_chat_to_project(
        chat_provider="telegram",
        chat_id=-100123450999,
        actor_user_id=101,
        project_ref="alpha_project",
    )
    second = service.bind_chat_to_project(
        chat_provider="telegram",
        chat_id=-100123450999,
        actor_user_id=101,
        project_ref="alpha_project",
    )

    assert first == second


def test_bind_chat_to_project_rejects_chat_already_bound_to_other_project(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    alpha_repo = _git_repo(tmp_path, "alpha-other-chat")
    beta_repo = _git_repo(tmp_path, "beta-other-chat")
    _register(
        registry,
        _snapshot(alpha_repo, with_chat_binding=True, chat_id=-100123451001),
    )
    _register(
        registry,
        _snapshot(
            beta_repo,
            project_id="beta_project",
            slug="beta-project",
            name="Beta Project",
        ),
    )
    service = ProjectChatBindingService(registry, (101,))

    with pytest.raises(ValueError, match="chat_already_bound_to_other_project"):
        service.bind_chat_to_project(
            chat_provider="telegram",
            chat_id=-100123451001,
            actor_user_id=101,
            project_ref="beta_project",
        )


def test_bind_chat_to_project_rejects_project_already_bound_to_other_chat(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    repo = _git_repo(tmp_path, "project-other-chat")
    _register(
        registry,
        _snapshot(repo, with_chat_binding=True, chat_id=-100123451002),
    )
    service = ProjectChatBindingService(registry, (101,))

    with pytest.raises(ValueError, match="project_already_bound_to_other_chat"):
        service.bind_chat_to_project(
            chat_provider="telegram",
            chat_id=-100123451003,
            actor_user_id=101,
            project_ref="alpha_project",
        )


def test_unbind_chat_requires_owner(tmp_path: Path):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    repo = _git_repo(tmp_path, "unbind-owner")
    _register(
        registry,
        _snapshot(repo, with_chat_binding=True, chat_id=-100123451004),
    )
    service = ProjectChatBindingService(registry, (101,))

    with pytest.raises(ValueError, match="binding_requires_owner_user"):
        service.unbind_chat(
            chat_provider="telegram",
            chat_id=-100123451004,
            actor_user_id=202,
        )


def test_unbind_chat_works_and_second_unbind_fails(tmp_path: Path):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    repo = _git_repo(tmp_path, "unbind")
    _register(
        registry,
        _snapshot(repo, with_chat_binding=True, chat_id=-100123451005),
    )
    service = ProjectChatBindingService(registry, (101,))

    binding = service.unbind_chat(
        chat_provider="telegram",
        chat_id=-100123451005,
        actor_user_id=101,
    )

    assert binding.project_id == "alpha_project"
    assert registry.get_project_snapshot_for_chat("telegram", -100123451005) is None

    with pytest.raises(ValueError, match="chat_not_bound"):
        service.unbind_chat(
            chat_provider="telegram",
            chat_id=-100123451005,
            actor_user_id=101,
        )
