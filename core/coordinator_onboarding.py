from __future__ import annotations

from dataclasses import dataclass

from core.project_context import VALID_PROJECT_CONTEXT_SOURCES
from core.project_registry import ProjectSnapshot

_VALID_ONBOARDING_CONTEXT_SOURCES = frozenset(
    {"bound_chat", "owner_dm_single_project"}
)


def _normalize_provider(provider: str) -> str:
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("empty_chat_provider")
    return provider.strip().lower()


def _validate_chat_id(chat_id: int) -> int:
    if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id == 0:
        raise ValueError(f"invalid_chat_id:{chat_id!r}")
    return chat_id


def _validate_user_id(user_id: int) -> int:
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError(f"invalid_user_id:{user_id!r}")
    return user_id


def _normalize_owner_task_text(owner_task_text: str) -> str:
    if not isinstance(owner_task_text, str) or not owner_task_text.strip():
        raise ValueError("empty_owner_task_text")
    return owner_task_text.strip()


def describe_context_source(context_source: str) -> str:
    if not isinstance(context_source, str) or not context_source.strip():
        raise ValueError("empty_context_source")
    normalized = context_source.strip()
    if normalized not in _VALID_ONBOARDING_CONTEXT_SOURCES:
        raise ValueError(f"invalid_context_source:{normalized}")
    if normalized == "bound_chat":
        return "explicit project chat"
    return "owner DM fallback"


@dataclass(frozen=True)
class ProjectCaptainOnboardingContext:
    snapshot: ProjectSnapshot
    chat_provider: str
    chat_id: int
    user_id: int
    context_source: str
    owner_task_text: str

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, ProjectSnapshot):
            raise ValueError(
                f"invalid_project_snapshot_type:{type(self.snapshot).__name__}"
            )
        if self.snapshot.runtime_binding is None:
            raise ValueError("snapshot_missing_runtime_binding")
        object.__setattr__(
            self,
            "chat_provider",
            _normalize_provider(self.chat_provider),
        )
        object.__setattr__(self, "chat_id", _validate_chat_id(self.chat_id))
        object.__setattr__(self, "user_id", _validate_user_id(self.user_id))
        if (
            not isinstance(self.context_source, str)
            or self.context_source not in VALID_PROJECT_CONTEXT_SOURCES
        ):
            raise ValueError(f"invalid_context_source:{self.context_source!r}")
        if self.context_source not in _VALID_ONBOARDING_CONTEXT_SOURCES:
            raise ValueError(f"invalid_context_source:{self.context_source!r}")
        object.__setattr__(
            self,
            "owner_task_text",
            _normalize_owner_task_text(self.owner_task_text),
        )
        if self.context_source == "bound_chat":
            if self.snapshot.chat_binding is None:
                raise ValueError("bound_chat_requires_explicit_chat_binding")
        elif self.context_source != "owner_dm_single_project":
            raise ValueError(f"invalid_context_source:{self.context_source!r}")


class ProjectCaptainOnboardingService:
    def build_pipeline_task_prompt(
        self,
        context: ProjectCaptainOnboardingContext,
    ) -> str:
        if not isinstance(context, ProjectCaptainOnboardingContext):
            raise ValueError(
                "invalid_project_captain_onboarding_context_type:"
                f"{type(context).__name__}"
            )

        snapshot = context.snapshot
        runtime_binding = snapshot.runtime_binding
        if runtime_binding is None:
            raise ValueError("snapshot_missing_runtime_binding")

        lines = [
            "Coordinator project captain onboarding",
            "",
            "Coordinator role: project captain",
            "Project context authority: authoritative",
            (
                "Do not substitute another project, repository, branch space, "
                "or runtime contour."
            ),
            "Do not expand scope beyond the owner's task.",
            "",
            "Project identity:",
            f"- project_id: {snapshot.project.project_id}",
            f"- slug: {snapshot.project.slug}",
            f"- name: {snapshot.project.name}",
            f"- status: {snapshot.project.status}",
            f"- owner_user_id: {snapshot.project.owner_user_id}",
            "",
            "Project context source:",
            f"- source: {describe_context_source(context.context_source)}",
            f"- chat_provider: {context.chat_provider}",
            f"- chat_id: {context.chat_id}",
            f"- user_id: {context.user_id}",
            "",
            "Runtime binding:",
            f"- adapter_name: {runtime_binding.adapter_name}",
            f"- repo_path: {runtime_binding.repo_path}",
        ]
        if runtime_binding.worktree_root is not None:
            lines.append(f"- worktree_root: {runtime_binding.worktree_root}")
        lines.extend(
            [
                f"- base_branch: {runtime_binding.base_branch}",
                f"- branch_prefix: {runtime_binding.branch_prefix}",
                f"- language: {runtime_binding.language}",
                "",
                "Coordinator instruction:",
                (
                    "Treat the project context above as authoritative for all "
                    "downstream planning and execution."
                ),
                "Do not invent another project scope, repo, or runtime.",
                "Do not expand scope beyond the owner's task.",
                "",
                "Original owner task:",
                "<<<OWNER_TASK",
                context.owner_task_text,
                "OWNER_TASK",
            ]
        )
        return "\n".join(lines)
