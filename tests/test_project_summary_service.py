from __future__ import annotations

from pathlib import Path

import pytest

from core.project_context import ProjectContextResolver
from core.project_models import Project, ProjectChatBinding, ProjectPolicy
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.project_runtime import ProjectRuntimeBinding
from core.project_summary_service import (
    ProjectSummaryService,
    ProjectSummaryView,
)
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
        "chat_id": -1001234567890,
        "chat_provider": "telegram",
    }
    data.update(overrides)
    return ProjectChatBinding(**data)


def _snapshot(
    repo_path: Path | None = None,
    *,
    with_runtime_binding: bool = True,
    with_chat_binding: bool = False,
    chat_id: int = -1001234567890,
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


def test_project_summary_service_construction_happy_path(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    resolver = ProjectContextResolver(registry, (101,))

    service = ProjectSummaryService(registry, resolver)

    assert service.registry is registry
    assert service.resolver is resolver


def test_project_summary_service_rejects_bad_registry(tmp_path: Path):
    resolver = ProjectContextResolver(ProjectRegistry(_make_db(tmp_path)), (101,))

    with pytest.raises(ValueError, match="invalid_project_registry_type"):
        ProjectSummaryService("bad", resolver)  # type: ignore[arg-type]


def test_project_summary_service_rejects_bad_resolver(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))

    with pytest.raises(ValueError, match="invalid_project_context_resolver_type"):
        ProjectSummaryService(registry, "bad")  # type: ignore[arg-type]


def test_project_summary_view_happy_path_for_bound_chat(tmp_path: Path):
    repo = _git_repo(tmp_path, "summary-bound")
    snapshot = _snapshot(repo, with_chat_binding=True)

    view = ProjectSummaryView(
        snapshot=snapshot,
        context_source="bound_chat",
        chat_provider="telegram",
        chat_id=snapshot.chat_binding.chat_id,  # type: ignore[union-attr]
        is_owner_chat=False,
        is_explicit_project_chat=True,
        has_runtime_binding=True,
    )

    assert view.context_source == "bound_chat"


def test_project_summary_view_happy_path_for_owner_dm_fallback(tmp_path: Path):
    repo = _git_repo(tmp_path, "summary-dm")
    snapshot = _snapshot(repo, with_chat_binding=False)

    view = ProjectSummaryView(
        snapshot=snapshot,
        context_source="owner_dm_single_project",
        chat_provider="telegram",
        chat_id=101,
        is_owner_chat=True,
        is_explicit_project_chat=False,
        has_runtime_binding=True,
    )

    assert view.context_source == "owner_dm_single_project"


def test_project_summary_view_rejects_bad_snapshot():
    with pytest.raises(ValueError, match="invalid_project_snapshot_type"):
        ProjectSummaryView(
            snapshot="bad",  # type: ignore[arg-type]
            context_source="bound_chat",
            chat_provider="telegram",
            chat_id=-1,
            is_owner_chat=False,
            is_explicit_project_chat=True,
            has_runtime_binding=False,
        )


def test_project_summary_view_rejects_bad_context_source(tmp_path: Path):
    repo = _git_repo(tmp_path, "summary-bad-source")
    snapshot = _snapshot(repo, with_chat_binding=True)

    with pytest.raises(ValueError, match="invalid_project_summary_context_source"):
        ProjectSummaryView(
            snapshot=snapshot,
            context_source="none",
            chat_provider="telegram",
            chat_id=-1,
            is_owner_chat=False,
            is_explicit_project_chat=False,
            has_runtime_binding=True,
        )


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_project_summary_view_rejects_bad_chat_provider(
    tmp_path: Path,
    bad: object,
):
    repo = _git_repo(tmp_path, "summary-bad-provider")
    snapshot = _snapshot(repo, with_chat_binding=True)

    with pytest.raises(ValueError, match="empty_chat_provider"):
        ProjectSummaryView(
            snapshot=snapshot,
            context_source="bound_chat",
            chat_provider=bad,  # type: ignore[arg-type]
            chat_id=-1,
            is_owner_chat=False,
            is_explicit_project_chat=True,
            has_runtime_binding=True,
        )


@pytest.mark.parametrize("bad", ["1", True, 0])
def test_project_summary_view_rejects_bad_chat_id(
    tmp_path: Path,
    bad: object,
):
    repo = _git_repo(tmp_path, "summary-bad-chat")
    snapshot = _snapshot(repo, with_chat_binding=True)

    with pytest.raises(ValueError, match="invalid_chat_id"):
        ProjectSummaryView(
            snapshot=snapshot,
            context_source="bound_chat",
            chat_provider="telegram",
            chat_id=bad,  # type: ignore[arg-type]
            is_owner_chat=False,
            is_explicit_project_chat=True,
            has_runtime_binding=True,
        )


@pytest.mark.parametrize(
    ("field_name", "kwargs", "match"),
    [
        (
            "is_owner_chat",
            {"is_owner_chat": "yes"},
            "invalid_is_owner_chat_type",
        ),
        (
            "is_explicit_project_chat",
            {"is_explicit_project_chat": "yes"},
            "invalid_is_explicit_project_chat_type",
        ),
        (
            "has_runtime_binding",
            {"has_runtime_binding": "yes"},
            "invalid_has_runtime_binding_type",
        ),
    ],
)
def test_project_summary_view_rejects_bad_bool_fields(
    tmp_path: Path,
    field_name: str,
    kwargs: dict[str, object],
    match: str,
):
    repo = _git_repo(tmp_path, f"summary-bool-{field_name}")
    snapshot = _snapshot(repo, with_chat_binding=True)
    data = {
        "snapshot": snapshot,
        "context_source": "bound_chat",
        "chat_provider": "telegram",
        "chat_id": -1,
        "is_owner_chat": False,
        "is_explicit_project_chat": True,
        "has_runtime_binding": True,
    }
    data.update(kwargs)

    with pytest.raises(ValueError, match=match):
        ProjectSummaryView(**data)  # type: ignore[arg-type]


def test_project_summary_view_rejects_inconsistent_bound_chat_flag(
    tmp_path: Path,
):
    repo = _git_repo(tmp_path, "summary-bound-flag")
    snapshot = _snapshot(repo, with_chat_binding=True)

    with pytest.raises(ValueError, match="bound_chat_requires_explicit_project_chat"):
        ProjectSummaryView(
            snapshot=snapshot,
            context_source="bound_chat",
            chat_provider="telegram",
            chat_id=-1,
            is_owner_chat=False,
            is_explicit_project_chat=False,
            has_runtime_binding=True,
        )


def test_project_summary_view_rejects_inconsistent_owner_dm_flag(
    tmp_path: Path,
):
    repo = _git_repo(tmp_path, "summary-dm-flag")
    snapshot = _snapshot(repo)

    with pytest.raises(ValueError, match="owner_dm_single_project_requires_owner_chat"):
        ProjectSummaryView(
            snapshot=snapshot,
            context_source="owner_dm_single_project",
            chat_provider="telegram",
            chat_id=101,
            is_owner_chat=False,
            is_explicit_project_chat=False,
            has_runtime_binding=True,
        )


def test_project_summary_view_rejects_runtime_binding_mismatch(tmp_path: Path):
    repo = _git_repo(tmp_path, "summary-runtime-mismatch")
    snapshot = _snapshot(repo, with_runtime_binding=False)

    with pytest.raises(ValueError, match="project_summary_runtime_binding_mismatch"):
        ProjectSummaryView(
            snapshot=snapshot,
            context_source="owner_dm_single_project",
            chat_provider="telegram",
            chat_id=101,
            is_owner_chat=True,
            is_explicit_project_chat=False,
            has_runtime_binding=True,
        )


def test_get_current_project_summary_bound_chat(tmp_path: Path):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    repo = _git_repo(tmp_path, "service-bound")
    _register(
        registry,
        _snapshot(repo, with_chat_binding=True, chat_id=-1001234567001),
    )
    service = ProjectSummaryService(
        registry,
        ProjectContextResolver(registry, (101,)),
    )

    summary = service.get_current_project_summary(-1001234567001, 999)

    assert summary.context_source == "bound_chat"
    assert summary.is_explicit_project_chat is True
    assert summary.has_runtime_binding is True


def test_get_current_project_summary_owner_dm_single_project(tmp_path: Path):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    repo = _git_repo(tmp_path, "service-dm")
    _register(registry, _snapshot(repo))
    service = ProjectSummaryService(
        registry,
        ProjectContextResolver(registry, (101,)),
    )

    summary = service.get_current_project_summary(101, 101)

    assert summary.context_source == "owner_dm_single_project"
    assert summary.is_owner_chat is True
    assert summary.is_explicit_project_chat is False


def test_get_current_project_summary_unbound_chat_raises(tmp_path: Path):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    repo = _git_repo(tmp_path, "service-unbound")
    _register(registry, _snapshot(repo))
    service = ProjectSummaryService(
        registry,
        ProjectContextResolver(registry, (101,)),
    )

    with pytest.raises(
        ValueError,
        match="project_context_not_resolved:project_chat_not_bound",
    ):
        service.get_current_project_summary(-1001234567002, 999)


def test_get_current_project_summary_owner_dm_multi_project_raises(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    alpha_repo = _git_repo(tmp_path, "service-multi-alpha")
    beta_repo = _git_repo(tmp_path, "service-multi-beta")
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
    service = ProjectSummaryService(
        registry,
        ProjectContextResolver(registry, (101,)),
    )

    with pytest.raises(
        ValueError,
        match="project_context_not_resolved:owner_dm_requires_explicit_project_chat",
    ):
        service.get_current_project_summary(101, 101)


def test_format_current_project_summary_bound_chat_includes_runtime_fields(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    repo = _git_repo(tmp_path, "format-bound")
    _register(
        registry,
        _snapshot(repo, with_chat_binding=True, chat_id=-1001234567003),
    )
    service = ProjectSummaryService(
        registry,
        ProjectContextResolver(registry, (101,)),
    )

    text = service.format_current_project_summary(-1001234567003, 999)

    assert "alpha-project" in text
    assert "alpha_project" in text
    assert str(repo.resolve()) in text
    assert "adapter" in text.lower()
    assert "explicit project chat" in text.lower()


def test_format_current_project_summary_owner_dm_fallback_mentions_fallback(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    repo = _git_repo(tmp_path, "format-dm")
    _register(registry, _snapshot(repo))
    service = ProjectSummaryService(
        registry,
        ProjectContextResolver(registry, (101,)),
    )

    text = service.format_current_project_summary(101, 101)

    assert "fallback" in text.lower()
    assert "owner dm fallback" in text.lower()


def test_format_current_project_summary_unbound_points_to_projects_bind(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    repo = _git_repo(tmp_path, "format-unbound")
    _register(registry, _snapshot(repo))
    service = ProjectSummaryService(
        registry,
        ProjectContextResolver(registry, (101,)),
    )

    text = service.format_current_project_summary(-1001234567004, 999)

    assert "не привязан" in text.lower()
    assert "/projects bind" in text


def test_format_current_project_summary_owner_dm_multi_project_requires_explicit_chat(
    tmp_path: Path,
):
    db = _make_db(tmp_path)
    registry = ProjectRegistry(db)
    alpha_repo = _git_repo(tmp_path, "format-multi-alpha")
    beta_repo = _git_repo(tmp_path, "format-multi-beta")
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
    service = ProjectSummaryService(
        registry,
        ProjectContextResolver(registry, (101,)),
    )

    text = service.format_current_project_summary(101, 101)

    assert "явный project chat" in text.lower()
    assert "не выбирает проект сам" in text.lower()
