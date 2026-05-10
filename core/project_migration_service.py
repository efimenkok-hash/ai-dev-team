from __future__ import annotations

from dataclasses import dataclass

from core.project_chat_binding_service import ProjectChatBindingService
from core.project_registry import ProjectRegistry, ProjectSnapshot

_VALID_CHAT_PROVIDERS = frozenset({"telegram"})


def _normalize_chat_provider(chat_provider: str) -> str:
    if not isinstance(chat_provider, str) or not chat_provider.strip():
        raise ValueError("empty_chat_provider")
    normalized = chat_provider.strip().lower()
    if normalized not in _VALID_CHAT_PROVIDERS:
        raise ValueError(f"invalid_chat_provider:{normalized}")
    return normalized


def _validate_transport_chat_id(chat_id: int) -> int:
    if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id == 0:
        raise ValueError(f"invalid_chat_id:{chat_id!r}")
    return chat_id


def _validate_positive_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"invalid_{field_name}:{value!r}")
    return value


def _validate_bool(value: bool, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"invalid_{field_name}_type:{type(value).__name__}")
    return value


@dataclass(frozen=True)
class ProjectMigrationStatus:
    snapshot: ProjectSnapshot | None
    chat_provider: str
    chat_id: int
    actor_user_id: int
    is_owner_user: bool
    is_group_chat: bool
    can_migrate_here: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.snapshot is not None and not isinstance(
            self.snapshot,
            ProjectSnapshot,
        ):
            raise ValueError(
                f"invalid_project_snapshot_type:{type(self.snapshot).__name__}"
            )
        object.__setattr__(
            self,
            "chat_provider",
            _normalize_chat_provider(self.chat_provider),
        )
        object.__setattr__(
            self,
            "chat_id",
            _validate_transport_chat_id(self.chat_id),
        )
        object.__setattr__(
            self,
            "actor_user_id",
            _validate_positive_int(
                self.actor_user_id,
                field_name="actor_user_id",
            ),
        )
        _validate_bool(self.is_owner_user, field_name="is_owner_user")
        _validate_bool(self.is_group_chat, field_name="is_group_chat")
        _validate_bool(self.can_migrate_here, field_name="can_migrate_here")
        if self.reason is not None:
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("invalid_project_migration_reason")
            object.__setattr__(self, "reason", self.reason.strip())
        if self.can_migrate_here:
            if self.snapshot is None:
                raise ValueError("migratable_status_requires_snapshot")
            if self.reason is not None:
                raise ValueError("migratable_status_cannot_have_reason")
        elif self.reason is None:
            raise ValueError("non_migratable_status_requires_reason")


class ProjectMigrationService:
    def __init__(
        self,
        registry: ProjectRegistry,
        chat_binding_service: ProjectChatBindingService,
        owner_user_ids: tuple[int, ...],
    ) -> None:
        if not isinstance(registry, ProjectRegistry):
            raise ValueError(
                f"invalid_project_registry_type:{type(registry).__name__}"
            )
        if not isinstance(
            chat_binding_service,
            ProjectChatBindingService,
        ):
            raise ValueError(
                "invalid_project_chat_binding_service_type:"
                f"{type(chat_binding_service).__name__}"
            )
        if not isinstance(owner_user_ids, tuple):
            raise ValueError("owner_user_ids_must_be_tuple")
        if not owner_user_ids:
            raise ValueError("empty_owner_user_ids")

        normalized_owner_ids: set[int] = set()
        for owner_user_id in owner_user_ids:
            normalized_owner_ids.add(
                _validate_positive_int(
                    owner_user_id,
                    field_name="owner_user_id",
                )
            )

        self._registry = registry
        self._chat_binding_service = chat_binding_service
        self._owner_user_ids = tuple(sorted(normalized_owner_ids))

    @property
    def registry(self) -> ProjectRegistry:
        return self._registry

    @property
    def chat_binding_service(self) -> ProjectChatBindingService:
        return self._chat_binding_service

    @property
    def owner_user_ids(self) -> tuple[int, ...]:
        return self._owner_user_ids

    def get_migration_status(
        self,
        *,
        chat_provider: str,
        chat_id: int,
        actor_user_id: int,
    ) -> ProjectMigrationStatus:
        normalized_chat_provider = _normalize_chat_provider(chat_provider)
        normalized_chat_id = _validate_transport_chat_id(chat_id)
        normalized_actor_user_id = _validate_positive_int(
            actor_user_id,
            field_name="actor_user_id",
        )
        is_owner_user = normalized_actor_user_id in self._owner_user_ids
        is_group_chat = normalized_chat_id < 0

        if not is_owner_user:
            return ProjectMigrationStatus(
                snapshot=None,
                chat_provider=normalized_chat_provider,
                chat_id=normalized_chat_id,
                actor_user_id=normalized_actor_user_id,
                is_owner_user=is_owner_user,
                is_group_chat=is_group_chat,
                can_migrate_here=False,
                reason="migration_requires_owner_user",
            )
        if not is_group_chat:
            return ProjectMigrationStatus(
                snapshot=None,
                chat_provider=normalized_chat_provider,
                chat_id=normalized_chat_id,
                actor_user_id=normalized_actor_user_id,
                is_owner_user=is_owner_user,
                is_group_chat=is_group_chat,
                can_migrate_here=False,
                reason="migration_requires_group_chat",
            )

        current_chat_status = self._chat_binding_service.get_chat_binding_status(
            normalized_chat_provider,
            normalized_chat_id,
        )
        if current_chat_status.snapshot is not None:
            return ProjectMigrationStatus(
                snapshot=current_chat_status.snapshot,
                chat_provider=normalized_chat_provider,
                chat_id=normalized_chat_id,
                actor_user_id=normalized_actor_user_id,
                is_owner_user=is_owner_user,
                is_group_chat=is_group_chat,
                can_migrate_here=False,
                reason="chat_already_bound",
            )

        snapshots = self._registry.list_project_snapshots()
        if not snapshots:
            return ProjectMigrationStatus(
                snapshot=None,
                chat_provider=normalized_chat_provider,
                chat_id=normalized_chat_id,
                actor_user_id=normalized_actor_user_id,
                is_owner_user=is_owner_user,
                is_group_chat=is_group_chat,
                can_migrate_here=False,
                reason="no_migratable_project",
            )
        if len(snapshots) > 1:
            return ProjectMigrationStatus(
                snapshot=None,
                chat_provider=normalized_chat_provider,
                chat_id=normalized_chat_id,
                actor_user_id=normalized_actor_user_id,
                is_owner_user=is_owner_user,
                is_group_chat=is_group_chat,
                can_migrate_here=False,
                reason="multiple_projects_require_projects_bind",
            )

        snapshot = snapshots[0]
        if snapshot.runtime_binding is None:
            return ProjectMigrationStatus(
                snapshot=snapshot,
                chat_provider=normalized_chat_provider,
                chat_id=normalized_chat_id,
                actor_user_id=normalized_actor_user_id,
                is_owner_user=is_owner_user,
                is_group_chat=is_group_chat,
                can_migrate_here=False,
                reason="project_missing_runtime_binding",
            )
        if snapshot.chat_binding is not None:
            return ProjectMigrationStatus(
                snapshot=snapshot,
                chat_provider=normalized_chat_provider,
                chat_id=normalized_chat_id,
                actor_user_id=normalized_actor_user_id,
                is_owner_user=is_owner_user,
                is_group_chat=is_group_chat,
                can_migrate_here=False,
                reason="project_already_bound_to_other_chat",
            )

        return ProjectMigrationStatus(
            snapshot=snapshot,
            chat_provider=normalized_chat_provider,
            chat_id=normalized_chat_id,
            actor_user_id=normalized_actor_user_id,
            is_owner_user=is_owner_user,
            is_group_chat=is_group_chat,
            can_migrate_here=True,
        )

    def migrate_current_chat(
        self,
        *,
        chat_provider: str,
        chat_id: int,
        actor_user_id: int,
    ) -> ProjectSnapshot:
        status = self.get_migration_status(
            chat_provider=chat_provider,
            chat_id=chat_id,
            actor_user_id=actor_user_id,
        )
        if not status.can_migrate_here:
            raise ValueError(status.reason or "migration_not_available")
        if status.snapshot is None:
            raise ValueError("migratable_status_missing_snapshot")
        return self._chat_binding_service.bind_chat_to_project(
            chat_provider=status.chat_provider,
            chat_id=status.chat_id,
            actor_user_id=status.actor_user_id,
            project_ref=status.snapshot.project.project_id,
        )
