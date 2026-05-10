from __future__ import annotations

from dataclasses import dataclass

from core.coordinator_onboarding import describe_context_source
from core.project_registry import ProjectSnapshot

_VALID_OWNER_ESCALATION_CONTEXT_SOURCES = frozenset(
    {"bound_chat", "owner_dm_single_project"}
)
_VALID_OWNER_ESCALATION_FINAL_STATES = frozenset({"FAIL", "BLOCKED"})

VALID_OWNER_ESCALATION_TYPES = frozenset(
    {
        "project_blocked",
        "quality_repair_exhausted",
        "runtime_validation_failed",
        "publish_failure",
        "system_failure",
    }
)

_QUALITY_REPAIR_FAILURE_REASONS = frozenset(
    {
        "review_fix_loop_exceeded",
        "qa_fix_loop_exceeded",
        "review_rejected_without_for_fixer",
        "qa_failed_without_for_fixer",
    }
)


def _normalize_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"empty_{field_name}")
    return value.strip()


@dataclass(frozen=True)
class CoordinatorOwnerEscalationContext:
    snapshot: ProjectSnapshot
    owner_task_text: str
    context_source: str
    final_state: str
    failure_reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, ProjectSnapshot):
            raise ValueError(
                f"invalid_project_snapshot_type:{type(self.snapshot).__name__}"
            )
        if self.snapshot.runtime_binding is None:
            raise ValueError("snapshot_missing_runtime_binding")
        object.__setattr__(
            self,
            "owner_task_text",
            _normalize_text(self.owner_task_text, "owner_task_text"),
        )
        if (
            not isinstance(self.context_source, str)
            or self.context_source.strip()
            not in _VALID_OWNER_ESCALATION_CONTEXT_SOURCES
        ):
            raise ValueError(f"invalid_context_source:{self.context_source!r}")
        object.__setattr__(self, "context_source", self.context_source.strip())
        if (
            not isinstance(self.final_state, str)
            or self.final_state.strip()
            not in _VALID_OWNER_ESCALATION_FINAL_STATES
        ):
            raise ValueError(f"invalid_final_state:{self.final_state!r}")
        object.__setattr__(self, "final_state", self.final_state.strip())
        object.__setattr__(
            self,
            "failure_reason",
            _normalize_text(self.failure_reason, "failure_reason"),
        )


def classify_owner_escalation_type(
    context: CoordinatorOwnerEscalationContext,
) -> str:
    if not isinstance(context, CoordinatorOwnerEscalationContext):
        raise ValueError(
            "invalid_coordinator_owner_escalation_context_type:"
            f"{type(context).__name__}"
        )
    if context.final_state == "BLOCKED" or context.failure_reason.startswith(
        ("writer_blocked:", "tester_blocked:", "fixer_blocked:")
    ):
        return "project_blocked"
    if (
        context.failure_reason.startswith("runtime_validator_exception:")
        or context.failure_reason == "runtime_validator_returned_invalid_report"
        or context.failure_reason.startswith("qa_fix_loop_exceeded:runtime:")
    ):
        return "runtime_validation_failed"
    if context.failure_reason.startswith("commit_failed:"):
        return "publish_failure"
    if context.failure_reason in _QUALITY_REPAIR_FAILURE_REASONS:
        return "quality_repair_exhausted"
    return "system_failure"


class CoordinatorOwnerEscalationService:
    def _require_context(
        self,
        context: CoordinatorOwnerEscalationContext,
    ) -> CoordinatorOwnerEscalationContext:
        if not isinstance(context, CoordinatorOwnerEscalationContext):
            raise ValueError(
                "invalid_coordinator_owner_escalation_context_type:"
                f"{type(context).__name__}"
            )
        if context.snapshot.runtime_binding is None:
            raise ValueError("snapshot_missing_runtime_binding")
        return context

    def classify_owner_escalation_type(
        self,
        context: CoordinatorOwnerEscalationContext,
    ) -> str:
        return classify_owner_escalation_type(self._require_context(context))

    def _summary_for_type(
        self,
        escalation_type: str,
    ) -> str:
        summaries = {
            "project_blocked": (
                "The task is blocked inside the current project contour and "
                "automatic progress cannot continue safely."
            ),
            "quality_repair_exhausted": (
                "The pipeline reached a quality barrier and the automatic "
                "repair path is exhausted for this project task."
            ),
            "runtime_validation_failed": (
                "The project task failed runtime validation, so the result "
                "cannot be accepted as a normal success path."
            ),
            "publish_failure": (
                "The code path reached the publish stage, but commit or "
                "publish did not complete successfully."
            ),
            "system_failure": (
                "The pipeline ended with an internal execution failure that "
                "requires owner-facing escalation instead of automatic retry."
            ),
        }
        if escalation_type not in summaries:
            raise ValueError(f"invalid_owner_escalation_type:{escalation_type}")
        return summaries[escalation_type]

    def _recommended_owner_action(
        self,
        escalation_type: str,
    ) -> str:
        actions = {
            "project_blocked": (
                "Provide the missing decision, clarification, or unblocker in "
                "the current project context before rerunning the task."
            ),
            "quality_repair_exhausted": (
                "Review the quality barrier and give explicit correction "
                "direction for the next attempt in this project."
            ),
            "runtime_validation_failed": (
                "Review the runtime validation findings and decide how the "
                "project should be corrected before another run."
            ),
            "publish_failure": (
                "Inspect the publish or commit issue, confirm the repository "
                "state, and retry publication only after that review."
            ),
            "system_failure": (
                "Review the failure reason and worker logs, then rerun after "
                "the underlying system issue is understood."
            ),
        }
        if escalation_type not in actions:
            raise ValueError(f"invalid_owner_escalation_type:{escalation_type}")
        return actions[escalation_type]

    def build_owner_escalation_artifact(
        self,
        context: CoordinatorOwnerEscalationContext,
    ) -> str:
        context = self._require_context(context)
        escalation_type = self.classify_owner_escalation_type(context)
        snapshot = context.snapshot
        lines = [
            "Coordinator owner escalation",
            "",
            "Escalation identity:",
            f"- escalation_type: {escalation_type}",
            f"- final_state: {context.final_state}",
            f"- failure_reason: {context.failure_reason}",
            "",
            "Project anchor:",
            f"- project_id: {snapshot.project.project_id}",
            f"- slug: {snapshot.project.slug}",
            f"- name: {snapshot.project.name}",
            "",
            "Context mode:",
            f"- mode: {describe_context_source(context.context_source)}",
            "",
            "Coordinator summary:",
            self._summary_for_type(escalation_type),
            (
                "This is an owner-facing escalation because the task cannot "
                "continue through the normal success or auto-fix path."
            ),
            "",
            "Recommended owner action:",
            self._recommended_owner_action(escalation_type),
            "",
            "Owner task anchor:",
            "<<<OWNER_TASK",
            context.owner_task_text,
            "OWNER_TASK",
            "",
            "Scope guard:",
            "This escalation is anchored to the current project contour only.",
            "Do not invent another project or runtime contour.",
            "This is not a hiring activation.",
            "This is not a team assembly decision.",
        ]
        return "\n".join(lines)

    def build_owner_escalation_reply(
        self,
        context: CoordinatorOwnerEscalationContext,
    ) -> str:
        context = self._require_context(context)
        escalation_type = self.classify_owner_escalation_type(context)
        slug = context.snapshot.project.slug
        replies = {
            "project_blocked": (
                f"Координатор: задача по проекту `{slug}` заблокирована. "
                "Нужен owner input или снятие блокера в текущем проектном контуре."
            ),
            "quality_repair_exhausted": (
                f"Координатор: задача по проекту `{slug}` упёрлась в quality "
                "barrier; auto-fix path исчерпан. Нужен owner review замечаний "
                "и явное решение по правкам."
            ),
            "runtime_validation_failed": (
                f"Координатор: задача по проекту `{slug}` не прошла runtime "
                "validation. Нужен owner review runtime findings и решение по "
                "исправлению."
            ),
            "publish_failure": (
                f"Координатор: задача по проекту `{slug}` дошла до publish "
                "step, но commit/publish не завершился. Нужен owner review "
                "проблемы публикации перед повторной попыткой."
            ),
            "system_failure": (
                f"Координатор: задача по проекту `{slug}` завершилась "
                "внутренним pipeline/system сбоем. Нужен manual review "
                "причины и повторный запуск после проверки."
            ),
        }
        if escalation_type not in replies:
            raise ValueError(f"invalid_owner_escalation_type:{escalation_type}")
        return replies[escalation_type]
