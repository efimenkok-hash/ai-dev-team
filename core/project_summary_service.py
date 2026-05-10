from __future__ import annotations

from dataclasses import dataclass

from core.project_context import ProjectContextResolver
from core.project_migration_service import ProjectMigrationService
from core.project_registry import ProjectRegistry, ProjectSnapshot

_VALID_PROJECT_SUMMARY_SOURCES = frozenset(
    {"bound_chat", "owner_dm_single_project"}
)


def _normalize_chat_provider(chat_provider: str) -> str:
    if not isinstance(chat_provider, str) or not chat_provider.strip():
        raise ValueError("empty_chat_provider")
    return chat_provider.strip().lower()


def _validate_transport_chat_id(chat_id: int) -> int:
    if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id == 0:
        raise ValueError(f"invalid_chat_id:{chat_id!r}")
    return chat_id


def _validate_bool(value: bool, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"invalid_{field_name}_type:{type(value).__name__}")
    return value


@dataclass(frozen=True)
class ProjectSummaryView:
    snapshot: ProjectSnapshot
    context_source: str
    chat_provider: str
    chat_id: int
    is_owner_chat: bool
    is_explicit_project_chat: bool
    has_runtime_binding: bool

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, ProjectSnapshot):
            raise ValueError(
                "invalid_project_snapshot_type:"
                f"{type(self.snapshot).__name__}"
            )
        if (
            not isinstance(self.context_source, str)
            or self.context_source not in _VALID_PROJECT_SUMMARY_SOURCES
        ):
            raise ValueError(
                f"invalid_project_summary_context_source:{self.context_source!r}"
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
        _validate_bool(self.is_owner_chat, field_name="is_owner_chat")
        _validate_bool(
            self.is_explicit_project_chat,
            field_name="is_explicit_project_chat",
        )
        _validate_bool(
            self.has_runtime_binding,
            field_name="has_runtime_binding",
        )
        if (
            self.context_source == "bound_chat"
            and not self.is_explicit_project_chat
        ):
            raise ValueError("bound_chat_requires_explicit_project_chat")
        if (
            self.context_source == "owner_dm_single_project"
            and not self.is_owner_chat
        ):
            raise ValueError("owner_dm_single_project_requires_owner_chat")
        if self.has_runtime_binding != (self.snapshot.runtime_binding is not None):
            raise ValueError("project_summary_runtime_binding_mismatch")


class ProjectSummaryService:
    def __init__(
        self,
        registry: ProjectRegistry,
        resolver: ProjectContextResolver,
        migration_service: ProjectMigrationService | None = None,
    ) -> None:
        if not isinstance(registry, ProjectRegistry):
            raise ValueError(
                f"invalid_project_registry_type:{type(registry).__name__}"
            )
        if not isinstance(resolver, ProjectContextResolver):
            raise ValueError(
                "invalid_project_context_resolver_type:"
                f"{type(resolver).__name__}"
            )
        if (
            migration_service is not None
            and not isinstance(migration_service, ProjectMigrationService)
        ):
            raise ValueError(
                "invalid_project_migration_service_type:"
                f"{type(migration_service).__name__}"
            )
        self._registry = registry
        self._resolver = resolver
        self._migration_service = migration_service

    @property
    def registry(self) -> ProjectRegistry:
        return self._registry

    @property
    def resolver(self) -> ProjectContextResolver:
        return self._resolver

    @property
    def migration_service(self) -> ProjectMigrationService | None:
        return self._migration_service

    def get_current_project_summary(
        self,
        chat_id: int,
        user_id: int,
    ) -> ProjectSummaryView:
        resolution = self._resolver.resolve_telegram_context(chat_id, user_id)
        if resolution.snapshot is None:
            raise ValueError(
                "project_context_not_resolved:"
                f"{resolution.reason or 'unknown'}"
            )
        return ProjectSummaryView(
            snapshot=resolution.snapshot,
            context_source=resolution.source,
            chat_provider=resolution.provider,
            chat_id=resolution.chat_id,
            is_owner_chat=resolution.is_owner_chat,
            is_explicit_project_chat=resolution.source == "bound_chat",
            has_runtime_binding=resolution.snapshot.runtime_binding is not None,
        )

    def format_current_project_summary(
        self,
        chat_id: int,
        user_id: int,
    ) -> str:
        try:
            summary = self.get_current_project_summary(chat_id, user_id)
        except ValueError as exc:
            code = str(exc)
            if code == "project_context_not_resolved:project_chat_not_bound":
                if self._migration_service is not None:
                    migration_status = self._migration_service.get_migration_status(
                        chat_provider="telegram",
                        chat_id=chat_id,
                        actor_user_id=user_id,
                    )
                    if (
                        migration_status.can_migrate_here
                        and migration_status.snapshot is not None
                    ):
                        return (
                            "📌 Текущий project context\n"
                            "\n"
                            "Проект для этого чата ещё не определён: explicit "
                            "project chat ещё не создан.\n"
                            "\n"
                            "Есть один мигрируемый проект "
                            f"`{migration_status.snapshot.project.slug}` "
                            f"(`{migration_status.snapshot.project.project_id}`).\n"
                            "\n"
                            "Используй `/projects migrate here`."
                        )
                return (
                    "📌 Текущий project context\n"
                    "\n"
                    "Проект не определён: этот чат ещё не привязан.\n"
                    "\n"
                    "Используй `/projects bind <project_id_or_slug>`."
                )
            if (
                code
                == "project_context_not_resolved:"
                "owner_dm_requires_explicit_project_chat"
            ):
                return (
                    "📌 Текущий project context\n"
                    "\n"
                    "Проект не определён: при нескольких проектах нужен "
                    "явный project chat.\n"
                    "\n"
                    "`/project` не выбирает проект сам."
                )
            raise

        snapshot = summary.snapshot
        project = snapshot.project
        lines = ["📌 Текущий project context", ""]
        if summary.context_source == "bound_chat":
            lines.append(
                f"Этот чат привязан к проекту `{project.slug}`."
            )
        else:
            lines.append("Контекст определён через owner DM fallback.")
        lines.extend(
            [
                "",
                f"slug: `{project.slug}`",
                f"project_id: `{project.project_id}`",
                f"name: `{project.name}`",
                f"status: `{project.status}`",
                f"owner_user_id: `{project.owner_user_id}`",
                (
                    "context source: `explicit project chat`"
                    if summary.context_source == "bound_chat"
                    else "context source: `owner DM fallback`"
                ),
                f"runtime binding: `{'yes' if summary.has_runtime_binding else 'no'}`",
                (
                    "explicit chat binding: `yes`"
                    if summary.is_explicit_project_chat
                    else "explicit chat binding: `no`"
                ),
            ]
        )
        if snapshot.chat_binding is not None:
            lines.append(f"chat_id: `{snapshot.chat_binding.chat_id}`")
        runtime_binding = snapshot.runtime_binding
        if runtime_binding is not None:
            lines.extend(
                [
                    "",
                    f"adapter: `{runtime_binding.adapter_name}`",
                    f"repo path: `{runtime_binding.repo_path}`",
                ]
            )
            if runtime_binding.worktree_root is not None:
                lines.append(f"worktree root: `{runtime_binding.worktree_root}`")
            lines.extend(
                [
                    f"base branch: `{runtime_binding.base_branch}`",
                    f"branch prefix: `{runtime_binding.branch_prefix}`",
                    f"language: `{runtime_binding.language}`",
                ]
            )
        return "\n".join(lines)
