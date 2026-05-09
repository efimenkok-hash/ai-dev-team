"""
Project-aware runtime routing for message-scoped execution.

Scope for roadmap step P2.3:
1. Resolve a concrete project runtime from message context or bootstrap
   fallback.
2. Materialize the correct SandboxWorkspace for the selected project.
3. Keep runtime resolution strict and explicit so callers never guess.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.project_bootstrap import ProjectBootstrapResult
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.project_runtime import ProjectRuntimeBinding
from core.sandbox_workspace import SandboxWorkspace
from core.telegram_bridge import IncomingMessage

VALID_RESOLVED_PROJECT_RUNTIME_SOURCES = frozenset(
    {"message_project_id", "bootstrap_active_project"}
)


def describe_project_runtime_error(code: str) -> str:
    if not isinstance(code, str) or not code.strip():
        raise ValueError("invalid_project_runtime_error_code")
    normalized = code.strip()
    if normalized == "message_project_registry_unavailable":
        return "Project registry для контекста сообщения сейчас недоступен."
    if normalized == "message_project_not_found":
        return "Проект из контекста сообщения не найден в registry."
    if normalized == "message_project_missing_runtime_binding":
        return "Проект определён, но у него нет runtime binding."
    if normalized == "message_project_runtime_invalid":
        return (
            "Проект определён, но его runtime binding сейчас невалиден "
            "на этой машине."
        )
    if normalized == "bootstrap_active_project_unavailable":
        return "Bootstrap active project для этого сообщения не определён."
    if normalized == "bootstrap_active_project_missing_runtime_binding":
        return "Bootstrap active project найден, но у него нет runtime binding."
    if normalized == "bootstrap_active_project_runtime_invalid":
        return (
            "Bootstrap active project найден, но его runtime binding сейчас "
            "невалиден на этой машине."
        )
    return f"Project runtime не удалось разрешить. Код: `{normalized}`."


def _build_sandbox(
    runtime_binding: ProjectRuntimeBinding,
) -> SandboxWorkspace:
    if not isinstance(runtime_binding, ProjectRuntimeBinding):
        raise ValueError("invalid_project_runtime_binding")
    return SandboxWorkspace(runtime_binding.build_sandbox_config())


@dataclass(frozen=True)
class ResolvedProjectRuntime:
    snapshot: ProjectSnapshot
    runtime_binding: ProjectRuntimeBinding
    sandbox: SandboxWorkspace
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, ProjectSnapshot):
            raise ValueError(
                f"invalid_project_snapshot_type:{type(self.snapshot).__name__}"
            )
        if not isinstance(self.runtime_binding, ProjectRuntimeBinding):
            raise ValueError(
                "invalid_project_runtime_binding_type:"
                f"{type(self.runtime_binding).__name__}"
            )
        if not isinstance(self.sandbox, SandboxWorkspace):
            raise ValueError(
                f"invalid_sandbox_workspace_type:{type(self.sandbox).__name__}"
            )
        if (
            not isinstance(self.source, str)
            or self.source not in VALID_RESOLVED_PROJECT_RUNTIME_SOURCES
        ):
            raise ValueError(f"invalid_resolved_project_runtime_source:{self.source!r}")
        if self.snapshot.runtime_binding is None:
            raise ValueError("snapshot_missing_runtime_binding")
        project_id = self.snapshot.project.project_id
        if self.snapshot.runtime_binding.project_id != project_id:
            raise ValueError(
                "snapshot_runtime_binding_project_id_mismatch:"
                f"{self.snapshot.runtime_binding.project_id}!={project_id}"
            )
        if self.runtime_binding.project_id != project_id:
            raise ValueError(
                "runtime_binding_project_id_mismatch:"
                f"{self.runtime_binding.project_id}!={project_id}"
            )
        if self.snapshot.runtime_binding != self.runtime_binding:
            raise ValueError("snapshot_runtime_binding_mismatch")


class ProjectRuntimeRouter:
    def __init__(
        self,
        registry: ProjectRegistry | None,
        bootstrap_result: ProjectBootstrapResult | None,
    ) -> None:
        if registry is not None and not isinstance(registry, ProjectRegistry):
            raise ValueError(
                f"invalid_project_registry_type:{type(registry).__name__}"
            )
        if (
            bootstrap_result is not None
            and not isinstance(bootstrap_result, ProjectBootstrapResult)
        ):
            raise ValueError(
                "invalid_project_bootstrap_result_type:"
                f"{type(bootstrap_result).__name__}"
            )
        self._registry = registry
        self._bootstrap_result = bootstrap_result

    @property
    def registry(self) -> ProjectRegistry | None:
        return self._registry

    @property
    def bootstrap_result(self) -> ProjectBootstrapResult | None:
        return self._bootstrap_result

    def resolve_message_runtime(
        self,
        msg: IncomingMessage,
    ) -> ResolvedProjectRuntime:
        if not isinstance(msg, IncomingMessage):
            raise ValueError(
                f"invalid_incoming_message_type:{type(msg).__name__}"
            )
        if msg.project_id is not None:
            if self._registry is None:
                raise ValueError("message_project_registry_unavailable")
            try:
                snapshot = self._registry.get_project_snapshot(msg.project_id)
            except ValueError as exc:
                raise ValueError("message_project_runtime_invalid") from exc
            if snapshot is None:
                raise ValueError("message_project_not_found")
            return self._resolve_snapshot_runtime(
                snapshot,
                source="message_project_id",
                missing_binding_code="message_project_missing_runtime_binding",
                invalid_runtime_code="message_project_runtime_invalid",
            )

        if self._bootstrap_result is None or self._bootstrap_result.active_snapshot is None:
            raise ValueError("bootstrap_active_project_unavailable")
        return self._resolve_snapshot_runtime(
            self._bootstrap_result.active_snapshot,
            source="bootstrap_active_project",
            missing_binding_code="bootstrap_active_project_missing_runtime_binding",
            invalid_runtime_code="bootstrap_active_project_runtime_invalid",
        )

    def has_any_routable_runtime(self) -> bool:
        if self._snapshot_has_routable_runtime(
            self._bootstrap_result.active_snapshot
            if self._bootstrap_result is not None
            else None
        ):
            return True
        if self._registry is None:
            return False
        for project in self._registry.list_projects():
            try:
                snapshot = self._registry.get_project_snapshot(project.project_id)
            except ValueError:
                continue
            if self._snapshot_has_routable_runtime(snapshot):
                return True
        return False

    def _resolve_snapshot_runtime(
        self,
        snapshot: ProjectSnapshot,
        *,
        source: str,
        missing_binding_code: str,
        invalid_runtime_code: str,
    ) -> ResolvedProjectRuntime:
        if not isinstance(snapshot, ProjectSnapshot):
            raise ValueError(
                f"invalid_project_snapshot_type:{type(snapshot).__name__}"
            )
        if snapshot.runtime_binding is None:
            raise ValueError(missing_binding_code)
        try:
            sandbox = _build_sandbox(snapshot.runtime_binding)
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError(invalid_runtime_code) from exc
        return ResolvedProjectRuntime(
            snapshot=snapshot,
            runtime_binding=snapshot.runtime_binding,
            sandbox=sandbox,
            source=source,
        )

    @staticmethod
    def _snapshot_has_routable_runtime(
        snapshot: ProjectSnapshot | None,
    ) -> bool:
        if snapshot is None or snapshot.runtime_binding is None:
            return False
        try:
            _build_sandbox(snapshot.runtime_binding)
        except (OSError, TypeError, ValueError):
            return False
        return True
