"""Tests for core.project_context."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.project_context import (
    ProjectContextResolution,
    ProjectContextResolver,
)
from core.project_models import Project, ProjectChatBinding, ProjectPolicy
from core.project_registry import ProjectRegistry, ProjectSnapshot
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


def _binding(**overrides: object) -> ProjectChatBinding:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "chat_id": -1001234567890,
        "chat_provider": "telegram",
    }
    data.update(overrides)
    return ProjectChatBinding(**data)


def _snapshot(**overrides: object) -> ProjectSnapshot:
    data: dict[str, object] = {
        "project": _project(),
        "policy": _policy(),
        "chat_binding": _binding(),
    }
    data.update(overrides)
    return ProjectSnapshot(**data)


def _register_project(
    registry: ProjectRegistry,
    *,
    with_chat_binding: bool,
    **overrides: object,
) -> ProjectSnapshot:
    chat_id = overrides.pop("chat_id", None)
    project_id = str(overrides.get("project_id", "alpha_project"))
    data: dict[str, object] = {
        "project": _project(**overrides),
        "policy": _policy(project_id=project_id),
    }
    if with_chat_binding:
        data["chat_binding"] = _binding(
            project_id=project_id,
            **({} if chat_id is None else {"chat_id": chat_id}),
        )
    snapshot = ProjectSnapshot(**data)
    registry.register_project(snapshot)
    loaded = registry.get_project_snapshot(snapshot.project.project_id)
    assert loaded is not None
    return loaded


# ---------------------------------------------------------------------------
# ProjectContextResolution construction
# ---------------------------------------------------------------------------


def test_project_context_resolution_happy_path():
    snapshot = _snapshot()

    resolution = ProjectContextResolution(
        snapshot=snapshot,
        provider="telegram",
        chat_id=snapshot.chat_binding.chat_id,  # type: ignore[union-attr]
        user_id=101,
        source="bound_chat",
    )

    assert resolution.snapshot == snapshot
    assert resolution.source == "bound_chat"
    assert resolution.reason is None
    assert resolution.is_owner_chat is False


def test_project_context_resolution_rejects_invalid_snapshot():
    with pytest.raises(ValueError, match="invalid_project_snapshot_type"):
        ProjectContextResolution(
            snapshot="bad",  # type: ignore[arg-type]
            provider="telegram",
            chat_id=1,
            user_id=101,
            source="none",
            reason="project_chat_not_bound",
        )


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_project_context_resolution_rejects_invalid_provider(bad: object):
    with pytest.raises(ValueError, match="empty_provider"):
        ProjectContextResolution(
            snapshot=None,
            provider=bad,  # type: ignore[arg-type]
            chat_id=1,
            user_id=101,
            source="none",
            reason="project_chat_not_bound",
        )


@pytest.mark.parametrize("bad", ["1", True, 0])
def test_project_context_resolution_rejects_invalid_chat_id(bad: object):
    with pytest.raises(ValueError, match="invalid_chat_id"):
        ProjectContextResolution(
            snapshot=None,
            provider="telegram",
            chat_id=bad,  # type: ignore[arg-type]
            user_id=101,
            source="none",
            reason="project_chat_not_bound",
        )


@pytest.mark.parametrize("bad", ["101", True, 0, -1])
def test_project_context_resolution_rejects_invalid_user_id(bad: object):
    with pytest.raises(ValueError, match="invalid_user_id"):
        ProjectContextResolution(
            snapshot=None,
            provider="telegram",
            chat_id=1,
            user_id=bad,  # type: ignore[arg-type]
            source="none",
            reason="project_chat_not_bound",
        )


def test_project_context_resolution_rejects_invalid_source():
    with pytest.raises(ValueError, match="invalid_project_context_source"):
        ProjectContextResolution(
            snapshot=None,
            provider="telegram",
            chat_id=1,
            user_id=101,
            source="registry",
            reason="project_chat_not_bound",
        )


@pytest.mark.parametrize("bad", ["", "   ", 123])
def test_project_context_resolution_rejects_invalid_reason(bad: object):
    with pytest.raises(ValueError, match="invalid_project_context_reason"):
        ProjectContextResolution(
            snapshot=None,
            provider="telegram",
            chat_id=1,
            user_id=101,
            source="none",
            reason=bad,  # type: ignore[arg-type]
        )


def test_project_context_resolution_rejects_snapshot_none_without_reason():
    with pytest.raises(ValueError, match="missing_project_context_reason"):
        ProjectContextResolution(
            snapshot=None,
            provider="telegram",
            chat_id=1,
            user_id=101,
            source="none",
        )


def test_project_context_resolution_rejects_bound_chat_without_chat_binding():
    with pytest.raises(ValueError, match="bound_chat_requires_chat_binding"):
        ProjectContextResolution(
            snapshot=ProjectSnapshot(project=_project(), policy=_policy()),
            provider="telegram",
            chat_id=1,
            user_id=101,
            source="bound_chat",
        )


def test_project_context_resolution_rejects_bound_chat_provider_mismatch():
    snapshot = _snapshot()
    with pytest.raises(ValueError, match="bound_chat_provider_mismatch"):
        ProjectContextResolution(
            snapshot=snapshot,
            provider="slack",
            chat_id=snapshot.chat_binding.chat_id,  # type: ignore[union-attr]
            user_id=101,
            source="bound_chat",
        )


def test_project_context_resolution_rejects_bound_chat_chat_id_mismatch():
    snapshot = _snapshot()
    with pytest.raises(ValueError, match="bound_chat_chat_id_mismatch"):
        ProjectContextResolution(
            snapshot=snapshot,
            provider="telegram",
            chat_id=42,
            user_id=101,
            source="bound_chat",
        )


def test_project_context_resolution_rejects_owner_dm_without_owner_flag():
    with pytest.raises(
        ValueError,
        match="owner_dm_single_project_requires_owner_chat",
    ):
        ProjectContextResolution(
            snapshot=ProjectSnapshot(project=_project(), policy=_policy()),
            provider="telegram",
            chat_id=101,
            user_id=101,
            source="owner_dm_single_project",
            is_owner_chat=False,
        )


def test_project_context_resolution_rejects_none_source_with_snapshot():
    with pytest.raises(ValueError, match="none_source_forbids_snapshot"):
        ProjectContextResolution(
            snapshot=ProjectSnapshot(project=_project(), policy=_policy()),
            provider="telegram",
            chat_id=1,
            user_id=101,
            source="none",
            reason="project_chat_not_bound",
        )


# ---------------------------------------------------------------------------
# Resolver constructor
# ---------------------------------------------------------------------------


def test_project_context_resolver_accepts_registry_and_normalizes_owner_ids(
    tmp_path: Path,
):
    resolver = ProjectContextResolver(
        ProjectRegistry(_make_db(tmp_path)),
        (202, 101, 202, 101),
    )

    assert resolver.owner_chat_ids == (101, 202)


def test_project_context_resolver_rejects_bad_registry():
    with pytest.raises(ValueError, match="invalid_project_registry_type"):
        ProjectContextResolver("bad", (101,))  # type: ignore[arg-type]


def test_project_context_resolver_rejects_non_tuple_owner_ids(tmp_path: Path):
    with pytest.raises(ValueError, match="owner_chat_ids_must_be_tuple"):
        ProjectContextResolver(
            ProjectRegistry(_make_db(tmp_path)),
            [101],  # type: ignore[arg-type]
        )


def test_project_context_resolver_rejects_empty_owner_ids(tmp_path: Path):
    with pytest.raises(ValueError, match="empty_owner_chat_ids"):
        ProjectContextResolver(ProjectRegistry(_make_db(tmp_path)), ())


def test_project_context_resolver_rejects_non_int_owner_id(tmp_path: Path):
    with pytest.raises(ValueError, match="invalid_user_id"):
        ProjectContextResolver(
            ProjectRegistry(_make_db(tmp_path)),
            ("101",),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("bad", [0, -1, True])
def test_project_context_resolver_rejects_invalid_owner_id(
    tmp_path: Path,
    bad: object,
):
    with pytest.raises(ValueError, match="invalid_user_id"):
        ProjectContextResolver(
            ProjectRegistry(_make_db(tmp_path)),
            (bad,),  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Resolution rules
# ---------------------------------------------------------------------------


def test_resolve_explicit_binding_wins_even_for_owner_chat(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    snapshot = _register_project(
        registry,
        with_chat_binding=True,
        owner_user_id=101,
        chat_id=101,
    )
    resolver = ProjectContextResolver(registry, (101,))

    resolution = resolver.resolve_telegram_context(chat_id=101, user_id=101)

    assert resolution.snapshot == snapshot
    assert resolution.source == "bound_chat"
    assert resolution.reason is None
    assert resolution.is_owner_chat is True


def test_resolve_owner_dm_single_project_fallback(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    snapshot = _register_project(
        registry,
        with_chat_binding=False,
        owner_user_id=101,
    )
    resolver = ProjectContextResolver(registry, (101,))

    resolution = resolver.resolve_telegram_context(chat_id=101, user_id=101)

    assert resolution.snapshot == snapshot
    assert resolution.source == "owner_dm_single_project"
    assert resolution.reason is None
    assert resolution.is_owner_chat is True


def test_resolve_owner_dm_multi_project_ambiguity(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    _register_project(registry, with_chat_binding=False, owner_user_id=101)
    _register_project(
        registry,
        with_chat_binding=False,
        project_id="beta_project",
        slug="beta-project",
        name="Beta Project",
        owner_user_id=202,
    )
    resolver = ProjectContextResolver(registry, (101,))

    resolution = resolver.resolve_telegram_context(chat_id=101, user_id=101)

    assert resolution.snapshot is None
    assert resolution.source == "none"
    assert resolution.reason == "owner_dm_requires_explicit_project_chat"
    assert resolution.is_owner_chat is True


@pytest.mark.parametrize("project_count", [1, 2])
def test_resolve_unbound_non_owner_chat_returns_none(
    tmp_path: Path,
    project_count: int,
):
    registry = ProjectRegistry(_make_db(tmp_path))
    _register_project(registry, with_chat_binding=False, owner_user_id=101)
    if project_count == 2:
        _register_project(
            registry,
            with_chat_binding=False,
            project_id="beta_project",
            slug="beta-project",
            name="Beta Project",
            owner_user_id=202,
        )
    resolver = ProjectContextResolver(registry, (101,))

    resolution = resolver.resolve_telegram_context(chat_id=555, user_id=555)

    assert resolution.snapshot is None
    assert resolution.source == "none"
    assert resolution.reason == "project_chat_not_bound"
    assert resolution.is_owner_chat is False


def test_resolve_bound_group_supergroup_chat(tmp_path: Path):
    registry = ProjectRegistry(_make_db(tmp_path))
    snapshot = _register_project(
        registry,
        with_chat_binding=True,
        owner_user_id=101,
        chat_id=-1009876543210,
    )
    resolver = ProjectContextResolver(registry, (101,))

    resolution = resolver.resolve_telegram_context(
        chat_id=-1009876543210,
        user_id=777,
    )

    assert resolution.snapshot == snapshot
    assert resolution.source == "bound_chat"
    assert resolution.chat_id == -1009876543210


def test_resolve_bound_project_without_runtime_binding_still_returns_snapshot(
    tmp_path: Path,
):
    registry = ProjectRegistry(_make_db(tmp_path))
    snapshot = _register_project(
        registry,
        with_chat_binding=True,
        owner_user_id=101,
        chat_id=-1001234567890,
    )
    resolver = ProjectContextResolver(registry, (101,))

    resolution = resolver.resolve_telegram_context(
        chat_id=-1001234567890,
        user_id=777,
    )

    assert resolution.snapshot == snapshot
    assert resolution.snapshot is not None
    assert resolution.snapshot.runtime_binding is None
    assert resolution.source == "bound_chat"


def test_resolve_telegram_context_rejects_invalid_chat_id(tmp_path: Path):
    resolver = ProjectContextResolver(ProjectRegistry(_make_db(tmp_path)), (101,))

    with pytest.raises(ValueError, match="invalid_chat_id"):
        resolver.resolve_telegram_context(chat_id="bad", user_id=101)  # type: ignore[arg-type]


def test_resolve_telegram_context_rejects_invalid_user_id(tmp_path: Path):
    resolver = ProjectContextResolver(ProjectRegistry(_make_db(tmp_path)), (101,))

    with pytest.raises(ValueError, match="invalid_user_id"):
        resolver.resolve_telegram_context(chat_id=101, user_id=0)
