"""
core/project_context.py

Telegram chat-to-project resolution contracts for AI Office.

Scope for roadmap step P2.1:
1. Define one strict result model for project resolution by chat context.
2. Resolve Telegram chat context from explicit project bindings first.
3. Allow owner-DM fallback only when the registry contains exactly one project.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.project_registry import ProjectRegistry, ProjectSnapshot

VALID_PROJECT_CONTEXT_SOURCES = frozenset(
    {"bound_chat", "owner_dm_single_project", "none"}
)


def _normalize_provider(provider: str) -> str:
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("empty_provider")
    return provider.strip().lower()


def _validate_chat_id(chat_id: int) -> int:
    if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id == 0:
        raise ValueError(f"invalid_chat_id:{chat_id!r}")
    return chat_id


def _validate_user_id(user_id: int) -> int:
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError(f"invalid_user_id:{user_id!r}")
    return user_id


def _validate_bool(value: bool, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"invalid_{field_name}_type:{type(value).__name__}")
    return value


@dataclass(frozen=True)
class ProjectContextResolution:
    snapshot: ProjectSnapshot | None
    provider: str
    chat_id: int
    user_id: int
    source: str
    reason: str | None = None
    is_owner_chat: bool = False

    def __post_init__(self) -> None:
        if self.snapshot is not None and not isinstance(
            self.snapshot,
            ProjectSnapshot,
        ):
            raise ValueError(
                "invalid_project_snapshot_type:"
                f"{type(self.snapshot).__name__}"
            )
        object.__setattr__(self, "provider", _normalize_provider(self.provider))
        object.__setattr__(self, "chat_id", _validate_chat_id(self.chat_id))
        object.__setattr__(self, "user_id", _validate_user_id(self.user_id))
        _validate_bool(self.is_owner_chat, field_name="is_owner_chat")

        if not isinstance(self.source, str) or self.source not in VALID_PROJECT_CONTEXT_SOURCES:
            raise ValueError(f"invalid_project_context_source:{self.source!r}")
        if self.reason is not None:
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("invalid_project_context_reason")
            object.__setattr__(self, "reason", self.reason.strip())

        if self.snapshot is None and self.reason is None:
            raise ValueError("missing_project_context_reason")

        if self.source == "bound_chat":
            if self.snapshot is None:
                raise ValueError("bound_chat_requires_snapshot")
            if self.snapshot.chat_binding is None:
                raise ValueError("bound_chat_requires_chat_binding")
            if self.snapshot.chat_binding.chat_provider != self.provider:
                raise ValueError("bound_chat_provider_mismatch")
            if self.snapshot.chat_binding.chat_id != self.chat_id:
                raise ValueError("bound_chat_chat_id_mismatch")

        if self.source == "owner_dm_single_project":
            if self.snapshot is None:
                raise ValueError("owner_dm_single_project_requires_snapshot")
            if not self.is_owner_chat:
                raise ValueError("owner_dm_single_project_requires_owner_chat")

        if self.source == "none" and self.snapshot is not None:
            raise ValueError("none_source_forbids_snapshot")


class ProjectContextResolver:
    def __init__(
        self,
        registry: ProjectRegistry,
        owner_chat_ids: tuple[int, ...],
    ) -> None:
        if not isinstance(registry, ProjectRegistry):
            raise ValueError(
                f"invalid_project_registry_type:{type(registry).__name__}"
            )
        if not isinstance(owner_chat_ids, tuple):
            raise ValueError("owner_chat_ids_must_be_tuple")
        if not owner_chat_ids:
            raise ValueError("empty_owner_chat_ids")

        normalized_owner_ids: set[int] = set()
        for owner_chat_id in owner_chat_ids:
            normalized_owner_ids.add(_validate_user_id(owner_chat_id))

        self._registry = registry
        self._owner_chat_ids = tuple(sorted(normalized_owner_ids))

    @property
    def registry(self) -> ProjectRegistry:
        return self._registry

    @property
    def owner_chat_ids(self) -> tuple[int, ...]:
        return self._owner_chat_ids

    def resolve_telegram_context(
        self,
        chat_id: int,
        user_id: int,
    ) -> ProjectContextResolution:
        normalized_chat_id = _validate_chat_id(chat_id)
        normalized_user_id = _validate_user_id(user_id)
        is_owner_chat = self._is_owner_chat(
            normalized_chat_id,
            normalized_user_id,
        )

        snapshot = self._registry.get_project_snapshot_for_chat(
            "telegram",
            normalized_chat_id,
        )
        if snapshot is not None:
            return ProjectContextResolution(
                snapshot=snapshot,
                provider="telegram",
                chat_id=normalized_chat_id,
                user_id=normalized_user_id,
                source="bound_chat",
                is_owner_chat=is_owner_chat,
            )

        if is_owner_chat:
            snapshots = self._registry.list_project_snapshots()
            if len(snapshots) == 1:
                return ProjectContextResolution(
                    snapshot=snapshots[0],
                    provider="telegram",
                    chat_id=normalized_chat_id,
                    user_id=normalized_user_id,
                    source="owner_dm_single_project",
                    is_owner_chat=True,
                )
            if len(snapshots) > 1:
                return ProjectContextResolution(
                    snapshot=None,
                    provider="telegram",
                    chat_id=normalized_chat_id,
                    user_id=normalized_user_id,
                    source="none",
                    reason="owner_dm_requires_explicit_project_chat",
                    is_owner_chat=True,
                )

        return ProjectContextResolution(
            snapshot=None,
            provider="telegram",
            chat_id=normalized_chat_id,
            user_id=normalized_user_id,
            source="none",
            reason="project_chat_not_bound",
            is_owner_chat=is_owner_chat,
        )

    def _is_owner_chat(self, chat_id: int, user_id: int) -> bool:
        return (
            chat_id in self._owner_chat_ids
            and user_id in self._owner_chat_ids
        )
