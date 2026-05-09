from __future__ import annotations

from dataclasses import dataclass

from core.project_models import Project, ProjectChatBinding
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


def _normalize_project_ref(project_ref: str) -> str:
    if not isinstance(project_ref, str) or not project_ref.strip():
        raise ValueError("empty_project_ref")
    normalized = project_ref.strip().lower()
    if not normalized.isascii():
        raise ValueError("non_ascii_project_ref")
    return normalized


@dataclass(frozen=True)
class ProjectBindingView:
    project: Project
    chat_binding: ProjectChatBinding | None
    has_runtime_binding: bool

    def __post_init__(self) -> None:
        if not isinstance(self.project, Project):
            raise ValueError(
                f"invalid_project_type:{type(self.project).__name__}"
            )
        if self.chat_binding is not None and not isinstance(
            self.chat_binding,
            ProjectChatBinding,
        ):
            raise ValueError(
                "invalid_project_chat_binding_type:"
                f"{type(self.chat_binding).__name__}"
            )
        _validate_bool(
            self.has_runtime_binding,
            field_name="has_runtime_binding",
        )
        if (
            self.chat_binding is not None
            and self.chat_binding.project_id != self.project.project_id
        ):
            raise ValueError(
                "project_binding_view_project_id_mismatch:"
                f"{self.chat_binding.project_id}!={self.project.project_id}"
            )


@dataclass(frozen=True)
class ChatBindingStatus:
    chat_provider: str
    chat_id: int
    snapshot: ProjectSnapshot | None
    reason: str | None = None

    def __post_init__(self) -> None:
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
        if self.snapshot is not None and not isinstance(
            self.snapshot,
            ProjectSnapshot,
        ):
            raise ValueError(
                "invalid_project_snapshot_type:"
                f"{type(self.snapshot).__name__}"
            )
        if self.reason is not None:
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("invalid_chat_binding_reason")
            object.__setattr__(self, "reason", self.reason.strip())
        if self.snapshot is None and self.reason is None:
            raise ValueError("missing_chat_binding_reason")
        if (
            self.snapshot is not None
            and self.snapshot.chat_binding is not None
            and self.snapshot.chat_binding.chat_provider != self.chat_provider
        ):
            raise ValueError("chat_binding_status_provider_mismatch")
        if (
            self.snapshot is not None
            and self.snapshot.chat_binding is not None
            and self.snapshot.chat_binding.chat_id != self.chat_id
        ):
            raise ValueError("chat_binding_status_chat_id_mismatch")


class ProjectChatBindingService:
    def __init__(
        self,
        registry: ProjectRegistry,
        owner_user_ids: tuple[int, ...],
    ) -> None:
        if not isinstance(registry, ProjectRegistry):
            raise ValueError(
                f"invalid_project_registry_type:{type(registry).__name__}"
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
        self._owner_user_ids = tuple(sorted(normalized_owner_ids))

    @property
    def registry(self) -> ProjectRegistry:
        return self._registry

    @property
    def owner_user_ids(self) -> tuple[int, ...]:
        return self._owner_user_ids

    def list_project_bindings(self) -> tuple[ProjectBindingView, ...]:
        return tuple(
            ProjectBindingView(
                project=snapshot.project,
                chat_binding=snapshot.chat_binding,
                has_runtime_binding=snapshot.runtime_binding is not None,
            )
            for snapshot in self._registry.list_project_snapshots()
        )

    def get_chat_binding_status(
        self,
        chat_provider: str,
        chat_id: int,
    ) -> ChatBindingStatus:
        normalized_chat_provider = _normalize_chat_provider(chat_provider)
        normalized_chat_id = _validate_transport_chat_id(chat_id)
        snapshot = self._registry.get_project_snapshot_for_chat(
            normalized_chat_provider,
            normalized_chat_id,
        )
        if snapshot is None:
            return ChatBindingStatus(
                chat_provider=normalized_chat_provider,
                chat_id=normalized_chat_id,
                snapshot=None,
                reason="chat_not_bound",
            )
        return ChatBindingStatus(
            chat_provider=normalized_chat_provider,
            chat_id=normalized_chat_id,
            snapshot=snapshot,
        )

    def bind_chat_to_project(
        self,
        *,
        chat_provider: str,
        chat_id: int,
        actor_user_id: int,
        project_ref: str,
    ) -> ProjectSnapshot:
        normalized_chat_provider = _normalize_chat_provider(chat_provider)
        normalized_chat_id = _validate_transport_chat_id(chat_id)
        normalized_actor_user_id = _validate_positive_int(
            actor_user_id,
            field_name="actor_user_id",
        )
        normalized_project_ref = _normalize_project_ref(project_ref)

        self._require_owner_user(normalized_actor_user_id)
        if normalized_chat_id > 0:
            raise ValueError("explicit_project_chat_must_be_group")

        target_snapshot = self._resolve_project_ref(normalized_project_ref)
        if target_snapshot.runtime_binding is None:
            raise ValueError("project_missing_runtime_binding")

        current_status = self.get_chat_binding_status(
            normalized_chat_provider,
            normalized_chat_id,
        )
        current_snapshot = current_status.snapshot
        if (
            current_snapshot is not None
            and current_snapshot.project.project_id != target_snapshot.project.project_id
        ):
            raise ValueError("chat_already_bound_to_other_project")

        if (
            target_snapshot.chat_binding is not None
            and (
                target_snapshot.chat_binding.chat_provider != normalized_chat_provider
                or target_snapshot.chat_binding.chat_id != normalized_chat_id
            )
        ):
            raise ValueError("project_already_bound_to_other_chat")

        if current_snapshot is not None:
            return current_snapshot

        self._registry.bind_project_chat(
            ProjectChatBinding(
                project_id=target_snapshot.project.project_id,
                chat_provider=normalized_chat_provider,
                chat_id=normalized_chat_id,
            )
        )
        bound_snapshot = self._registry.get_project_snapshot(
            target_snapshot.project.project_id
        )
        if bound_snapshot is None:
            raise ValueError(
                "bound_project_missing_after_write:"
                f"{target_snapshot.project.project_id}"
            )
        return bound_snapshot

    def unbind_chat(
        self,
        *,
        chat_provider: str,
        chat_id: int,
        actor_user_id: int,
    ) -> ProjectChatBinding:
        normalized_chat_provider = _normalize_chat_provider(chat_provider)
        normalized_chat_id = _validate_transport_chat_id(chat_id)
        normalized_actor_user_id = _validate_positive_int(
            actor_user_id,
            field_name="actor_user_id",
        )

        self._require_owner_user(normalized_actor_user_id)

        status = self.get_chat_binding_status(
            normalized_chat_provider,
            normalized_chat_id,
        )
        if status.snapshot is None or status.snapshot.chat_binding is None:
            raise ValueError("chat_not_bound")
        binding = status.snapshot.chat_binding

        def _delete_binding(conn) -> None:
            conn.execute(
                """
                DELETE FROM project_chat_bindings
                WHERE project_id = ? AND chat_provider = ? AND chat_id = ?
                """,
                (
                    binding.project_id,
                    binding.chat_provider,
                    binding.chat_id,
                ),
            )

        self._registry.state_db._run_write_transaction(_delete_binding)
        return binding

    def _require_owner_user(self, actor_user_id: int) -> None:
        if actor_user_id not in self._owner_user_ids:
            raise ValueError("binding_requires_owner_user")

    def _resolve_project_ref(self, project_ref: str) -> ProjectSnapshot:
        try:
            snapshot = self._registry.get_project_snapshot(project_ref)
        except ValueError:
            snapshot = None
        if snapshot is not None:
            return snapshot

        try:
            snapshot = self._registry.get_project_snapshot_by_slug(project_ref)
        except ValueError:
            snapshot = None
        if snapshot is not None:
            return snapshot

        raise ValueError("project_not_found")
