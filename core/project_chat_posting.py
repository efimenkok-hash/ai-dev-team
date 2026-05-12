from __future__ import annotations

from dataclasses import dataclass

from core.coordinator_role import COORDINATOR_ROLE
from core.progress_emitter import ProgressEvent
from core.project_registry import ProjectSnapshot
from core.telegram_bridge import OutgoingEnvelope, OutgoingMessage

VALID_PROJECT_CHAT_POSTING_SOURCES = frozenset(
    {"bound_chat", "owner_dm_single_project"}
)
_AGENT_LIFECYCLE_EVENT_KINDS = frozenset(
    {"agent_started", "agent_finished", "agent_failed"}
)


def format_progress_event(event: ProgressEvent) -> str:
    if not isinstance(event, ProgressEvent):
        raise ValueError(f"invalid_progress_event_type:{type(event).__name__}")
    kind = event.kind
    agent = event.agent_role or "—"
    detail = (event.detail or "").strip()
    if kind == "task_started":
        return f"🚀 Старт{(' · ' + detail) if detail else ''}"
    if kind == "agent_started":
        return f"▶︎ {agent} начал"
    if kind == "agent_finished":
        ms = event.duration_ms or 0
        return f"✓ {agent} закончил ({ms} мс)"
    if kind == "agent_failed":
        return f"⚠️ {agent} упал — {detail[:160]}"
    if kind == "fsm_transition":
        return f"⤳ {detail}" if detail else "⤳ переход"
    if kind == "task_completed":
        return f"🏁 Готово{(' · ' + detail) if detail else ''}"
    if kind == "task_failed":
        return f"💥 Провалена: {detail or 'причина не указана'}"
    return f"[{kind}] {detail}"


@dataclass(frozen=True)
class ProjectChatPostingContext:
    snapshot: ProjectSnapshot
    chat_id: int
    context_source: str

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, ProjectSnapshot):
            raise ValueError(
                "invalid_project_snapshot_type:"
                f"{type(self.snapshot).__name__}"
            )
        if self.snapshot.runtime_binding is None:
            raise ValueError("project_snapshot_missing_runtime_binding")
        if not isinstance(self.chat_id, int) or isinstance(self.chat_id, bool):
            raise ValueError(f"invalid_project_chat_id:{self.chat_id!r}")
        if self.chat_id == 0:
            raise ValueError("project_chat_id_zero")
        if (
            not isinstance(self.context_source, str)
            or self.context_source not in VALID_PROJECT_CHAT_POSTING_SOURCES
        ):
            raise ValueError(
                "invalid_project_chat_posting_context_source:"
                f"{self.context_source!r}"
            )
        if (
            self.context_source == "bound_chat"
            and self.snapshot.chat_binding is None
        ):
            raise ValueError("bound_chat_requires_explicit_chat_binding")


class ProjectChatPostingService:
    def resolve_event_sender_role(
        self,
        context: ProjectChatPostingContext,
        event: ProgressEvent,
    ) -> str:
        if not isinstance(context, ProjectChatPostingContext):
            raise ValueError(
                "invalid_project_chat_posting_context_type:"
                f"{type(context).__name__}"
            )
        if not isinstance(event, ProgressEvent):
            raise ValueError(
                "invalid_progress_event_type:"
                f"{type(event).__name__}"
            )
        if context.context_source == "owner_dm_single_project":
            return COORDINATOR_ROLE
        if event.kind in _AGENT_LIFECYCLE_EVENT_KINDS:
            if not isinstance(event.agent_role, str) or not event.agent_role.strip():
                raise ValueError("agent_lifecycle_event_missing_agent_role")
            return event.agent_role
        return COORDINATOR_ROLE

    def build_event_envelope(
        self,
        context: ProjectChatPostingContext,
        event: ProgressEvent,
    ) -> OutgoingEnvelope:
        return OutgoingEnvelope(
            message=OutgoingMessage(
                chat_id=context.chat_id,
                text=format_progress_event(event),
            ),
            sender_role=self.resolve_event_sender_role(context, event),
        )

    def build_system_envelope(
        self,
        context: ProjectChatPostingContext,
        text: str,
    ) -> OutgoingEnvelope:
        if not isinstance(context, ProjectChatPostingContext):
            raise ValueError(
                "invalid_project_chat_posting_context_type:"
                f"{type(context).__name__}"
            )
        return OutgoingEnvelope(
            message=OutgoingMessage(chat_id=context.chat_id, text=text),
            sender_role=COORDINATOR_ROLE,
        )

    def build_terminal_envelope(
        self,
        context: ProjectChatPostingContext,
        text: str,
    ) -> OutgoingEnvelope:
        return self.build_system_envelope(context, text)
