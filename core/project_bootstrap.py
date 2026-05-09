"""
core/project_bootstrap.py

Single-project bootstrap logic for the project-aware runtime foundation.

Scope for roadmap step P1.5:
1. Resolve the active single-project runtime from ProjectRegistry first.
2. Preserve legacy REPO_PATH / WORKTREE_ROOT compatibility as bootstrap-only
   fallback.
3. Keep the result explicit so bot builders can make truthful decisions
   without touching Telegram runtime integration.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from core.project_models import Project, ProjectPolicy
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.project_runtime import ProjectRuntimeBinding
from core.state_db import StateDB

VALID_BOOTSTRAP_SOURCES = frozenset(
    {"registry", "legacy_env_seeded", "legacy_env_ephemeral", "none"}
)

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_EPHEMERAL_LEGACY_OWNER_USER_ID = 1


def _env_text(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _sanitize_repo_name(raw_name: str, *, separator: str) -> str:
    normalized_chars: list[str] = []
    for char in raw_name.strip().lower():
        if char.isascii() and char.isalnum():
            normalized_chars.append(char)
        else:
            normalized_chars.append(separator)
    collapsed = re.sub(
        rf"{re.escape(separator)}+",
        separator,
        "".join(normalized_chars),
    )
    return collapsed.strip(separator)


def _finalize_identifier(candidate: str, *, fallback: str) -> str:
    trimmed = candidate[:64].strip("_")
    if trimmed and _IDENTIFIER_RE.fullmatch(trimmed):
        return trimmed
    return fallback


def _derive_adapter_name(base_identifier: str) -> str:
    suffix = "_adapter"
    if len(base_identifier) + len(suffix) <= 64:
        candidate = f"{base_identifier}{suffix}"
    else:
        candidate = f"{base_identifier[: 64 - len(suffix)].rstrip('_')}{suffix}"
    return _finalize_identifier(candidate, fallback="default_adapter")


@dataclass(frozen=True)
class ProjectBootstrapResult:
    registry: ProjectRegistry | None
    active_snapshot: ProjectSnapshot | None
    source: str
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.registry is not None and not isinstance(self.registry, ProjectRegistry):
            raise ValueError(
                f"invalid_project_registry_type:{type(self.registry).__name__}"
            )
        if self.active_snapshot is not None and not isinstance(
            self.active_snapshot,
            ProjectSnapshot,
        ):
            raise ValueError(
                "invalid_project_snapshot_type:"
                f"{type(self.active_snapshot).__name__}"
            )
        if not isinstance(self.source, str) or self.source not in VALID_BOOTSTRAP_SOURCES:
            raise ValueError(f"invalid_bootstrap_source:{self.source!r}")
        if self.reason is not None:
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("invalid_bootstrap_reason")
            object.__setattr__(self, "reason", self.reason.strip())

        if self.active_snapshot is None and self.reason is None:
            raise ValueError("missing_bootstrap_reason")
        if self.source == "registry" and self.registry is None:
            raise ValueError("registry_source_requires_registry")
        if (
            self.source == "legacy_env_seeded"
            and (self.registry is None or self.active_snapshot is None)
        ):
            raise ValueError("legacy_env_seeded_requires_registry_and_snapshot")
        if (
            self.source == "legacy_env_ephemeral"
            and (self.registry is not None or self.active_snapshot is None)
        ):
            raise ValueError("legacy_env_ephemeral_requires_snapshot_only")
        if self.source == "none" and self.active_snapshot is not None:
            raise ValueError("none_source_cannot_have_active_snapshot")


def _derive_legacy_project_identity(repo_path: Path) -> tuple[str, str, str]:
    if not isinstance(repo_path, Path):
        raise ValueError("repo_path_must_be_path")
    repo_name = repo_path.name.strip()
    slug_candidate = _sanitize_repo_name(repo_name, separator="-")
    slug = slug_candidate if slug_candidate and _SLUG_RE.fullmatch(slug_candidate) else "default-project"

    base_candidate = _sanitize_repo_name(repo_name, separator="_")
    if not base_candidate:
        return ("default_project", slug, "default_adapter")
    if not base_candidate[0].isalpha():
        base_candidate = f"repo_{base_candidate}"

    project_id = _finalize_identifier(base_candidate, fallback="default_project")
    adapter_name = _derive_adapter_name(base_candidate)
    return (project_id, slug, adapter_name)


def _resolve_legacy_repo_path(env: Mapping[str, str]) -> Path:
    repo_path_raw = _env_text(env, "REPO_PATH")
    if repo_path_raw is None:
        raise ValueError("legacy_repo_path_missing")
    repo_path = Path(repo_path_raw).expanduser().resolve()
    if not repo_path.exists():
        raise ValueError("legacy_repo_path_missing")
    if not repo_path.is_dir():
        raise ValueError("legacy_repo_path_not_dir")
    if not (repo_path / ".git").exists():
        raise ValueError("legacy_repo_path_not_git")
    return repo_path


def _resolve_legacy_owner_user_id(
    env: Mapping[str, str],
    *,
    allow_ephemeral_default: bool,
) -> int:
    raw_owner_ids = env.get("TELEGRAM_OWNER_CHAT_ID")
    if raw_owner_ids is None:
        if allow_ephemeral_default:
            return _EPHEMERAL_LEGACY_OWNER_USER_ID
        raise ValueError("legacy_owner_chat_id_missing")
    if not isinstance(raw_owner_ids, str):
        raise ValueError("legacy_owner_user_id_invalid")

    parts = [part.strip() for part in raw_owner_ids.split(",") if part.strip()]
    if not parts:
        raise ValueError("legacy_owner_chat_id_missing")

    normalized_owner_ids: set[int] = set()
    for part in parts:
        try:
            owner_user_id = int(part)
        except ValueError as exc:
            raise ValueError("legacy_owner_user_id_invalid") from exc
        if owner_user_id <= 0:
            raise ValueError("legacy_owner_user_id_invalid")
        normalized_owner_ids.add(owner_user_id)

    return min(normalized_owner_ids)


def _build_legacy_project_snapshot(
    env: Mapping[str, str],
    *,
    allow_ephemeral_owner_fallback: bool = False,
) -> ProjectSnapshot:
    repo_path = _resolve_legacy_repo_path(env)
    owner_user_id = _resolve_legacy_owner_user_id(
        env,
        allow_ephemeral_default=allow_ephemeral_owner_fallback,
    )
    project_id, slug, adapter_name = _derive_legacy_project_identity(repo_path)

    worktree_root_raw = _env_text(env, "WORKTREE_ROOT")
    worktree_root = (
        Path(worktree_root_raw).expanduser()
        if worktree_root_raw is not None
        else None
    )

    project_name = repo_path.name.strip() or "Default Project"
    project = Project(
        project_id=project_id,
        slug=slug,
        name=project_name,
        description="Legacy single-project bootstrap derived from REPO_PATH.",
        owner_user_id=owner_user_id,
        status="active",
    )
    policy = ProjectPolicy(project_id=project_id)
    runtime_kwargs: dict[str, object] = {
        "project_id": project_id,
        "adapter_name": adapter_name,
        "repo_path": repo_path,
        "base_branch": "main",
        "branch_prefix": "feature/",
        "language": "python",
        "rules": (),
        "commands": (),
        "forbidden_paths": (),
        "forbidden_tokens": (),
    }
    if worktree_root is not None:
        runtime_kwargs["worktree_root"] = worktree_root
    runtime_binding = ProjectRuntimeBinding(**runtime_kwargs)
    return ProjectSnapshot(
        project=project,
        policy=policy,
        runtime_binding=runtime_binding,
    )


def _try_build_legacy_project_snapshot(
    env: Mapping[str, str],
    *,
    allow_ephemeral_owner_fallback: bool = False,
) -> tuple[ProjectSnapshot | None, str]:
    try:
        return (
            _build_legacy_project_snapshot(
                env,
                allow_ephemeral_owner_fallback=allow_ephemeral_owner_fallback,
            ),
            "legacy_snapshot_available",
        )
    except ValueError as exc:
        return (None, str(exc))


def build_project_bootstrap_result(
    env: Mapping[str, str],
    state_db: StateDB | None,
) -> ProjectBootstrapResult:
    if not isinstance(env, Mapping):
        raise ValueError("env_must_be_mapping")
    if state_db is not None and not isinstance(state_db, StateDB):
        raise ValueError(
            f"invalid_state_db_type:{type(state_db).__name__}"
        )

    registry = ProjectRegistry(state_db) if state_db is not None else None
    if registry is not None:
        projects = registry.list_projects()
        if len(projects) > 1:
            return ProjectBootstrapResult(
                registry=registry,
                active_snapshot=None,
                source="registry",
                reason="multiple_projects_require_explicit_binding",
            )
        if len(projects) == 1:
            try:
                snapshot = registry.get_project_snapshot(projects[0].project_id)
            except ValueError:
                return ProjectBootstrapResult(
                    registry=registry,
                    active_snapshot=None,
                    source="registry",
                    reason="active_project_runtime_binding_invalid",
                )
            if snapshot is None:
                return ProjectBootstrapResult(
                    registry=registry,
                    active_snapshot=None,
                    source="registry",
                    reason="active_project_not_found",
                )
            if snapshot.runtime_binding is None:
                return ProjectBootstrapResult(
                    registry=registry,
                    active_snapshot=None,
                    source="registry",
                    reason="active_project_missing_runtime_binding",
                )
            return ProjectBootstrapResult(
                registry=registry,
                active_snapshot=snapshot,
                source="registry",
            )

    legacy_snapshot, legacy_reason = _try_build_legacy_project_snapshot(
        env,
        allow_ephemeral_owner_fallback=registry is None,
    )
    if legacy_snapshot is None:
        return ProjectBootstrapResult(
            registry=registry,
            active_snapshot=None,
            source="none",
            reason=legacy_reason,
        )

    if registry is None:
        return ProjectBootstrapResult(
            registry=None,
            active_snapshot=legacy_snapshot,
            source="legacy_env_ephemeral",
        )

    registry.register_project(legacy_snapshot)
    persisted_snapshot = registry.get_project_snapshot(legacy_snapshot.project.project_id)
    if persisted_snapshot is None:
        raise RuntimeError("persisted_project_snapshot_missing_after_seed")
    return ProjectBootstrapResult(
        registry=registry,
        active_snapshot=persisted_snapshot,
        source="legacy_env_seeded",
    )
