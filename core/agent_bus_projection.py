"""
core/agent_bus_projection.py

Public projection layer for backend agent-bus messages.

Scope for roadmap step P4.5:
1. Keep backend bus / StateDB as the source of truth.
2. Translate one persisted bus message into one public project-chat envelope.
3. Avoid prompt/runtime orchestration, throttling, retries, or summarization.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from core.agent_bus import StateBackedAgentBus
from core.agent_bus_models import (
    AgentMessage,
    AgentReply,
    AgentRequest,
    ProjectThread,
)
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.telegram_bridge import OutgoingEnvelope, OutgoingMessage

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

VALID_AGENT_BUS_PROJECTION_STATUSES = frozenset(
    {
        "projected",
        "not_projected_no_chat_binding",
        "not_projected_unsupported_provider",
        "projection_send_failed",
    }
)


def _normalize_identifier(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"empty_{field_name}")
    normalized = value.strip().lower()
    if not normalized.isascii():
        raise ValueError(f"non_ascii_{field_name}")
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"invalid_{field_name}:{normalized}")
    return normalized


def _normalize_task_id(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"empty_{field_name}")
    normalized = value.strip().lower()
    if not normalized.isascii():
        raise ValueError(f"non_ascii_{field_name}")
    if not _TASK_ID_RE.fullmatch(normalized):
        raise ValueError(f"invalid_{field_name}:{normalized}")
    return normalized


def _normalize_chat_provider(value: str) -> str:
    normalized = _normalize_identifier(value, field_name="chat_provider")
    if normalized != "telegram":
        raise ValueError(f"unsupported_chat_provider:{normalized}")
    return normalized


def _validate_non_zero_chat_id(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value == 0:
        raise ValueError(f"invalid_{field_name}:{value!r}")
    return value


def _normalize_status(value: str) -> str:
    normalized = _normalize_identifier(value, field_name="projection_status")
    if normalized not in VALID_AGENT_BUS_PROJECTION_STATUSES:
        raise ValueError(f"invalid_projection_status:{normalized}")
    return normalized


def _validate_thread_message_scope(
    thread: ProjectThread,
    message: AgentMessage,
) -> None:
    if not isinstance(thread, ProjectThread):
        raise ValueError(
            "invalid_project_thread_type:"
            f"{type(thread).__name__}"
        )
    if not isinstance(message, AgentMessage):
        raise ValueError(
            "invalid_agent_message_type:"
            f"{type(message).__name__}"
        )
    if thread.project_id != message.project_id:
        raise ValueError(
            "thread_message_project_id_mismatch:"
            f"{thread.project_id}!={message.project_id}"
        )
    if thread.thread_id != message.thread_id:
        raise ValueError(
            "thread_message_thread_id_mismatch:"
            f"{thread.thread_id}!={message.thread_id}"
        )


def _format_failure_reason(exc: Exception) -> str:
    detail = str(exc).strip()
    return (
        exc.__class__.__name__
        if not detail
        else f"{exc.__class__.__name__}:{detail}"
    )


@dataclass(frozen=True)
class AgentBusProjectionTarget:
    project_id: str
    chat_id: int
    chat_provider: str
    thread_id: str
    task_id: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_id",
            _normalize_identifier(self.project_id, field_name="project_id"),
        )
        object.__setattr__(
            self,
            "chat_id",
            _validate_non_zero_chat_id(self.chat_id, field_name="chat_id"),
        )
        object.__setattr__(
            self,
            "chat_provider",
            _normalize_chat_provider(self.chat_provider),
        )
        object.__setattr__(
            self,
            "thread_id",
            _normalize_identifier(self.thread_id, field_name="thread_id"),
        )
        if self.task_id is not None:
            object.__setattr__(
                self,
                "task_id",
                _normalize_task_id(self.task_id, field_name="task_id"),
            )


@dataclass(frozen=True)
class AgentBusProjectionResult:
    message: AgentMessage
    thread: ProjectThread
    status: str
    envelope: OutgoingEnvelope | None = None
    projected_chat_id: int | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        _validate_thread_message_scope(self.thread, self.message)
        object.__setattr__(self, "status", _normalize_status(self.status))
        if self.envelope is not None and not isinstance(
            self.envelope,
            OutgoingEnvelope,
        ):
            raise ValueError(
                "invalid_outgoing_envelope_type:"
                f"{type(self.envelope).__name__}"
            )
        if self.projected_chat_id is not None:
            object.__setattr__(
                self,
                "projected_chat_id",
                _validate_non_zero_chat_id(
                    self.projected_chat_id,
                    field_name="projected_chat_id",
                ),
            )
        if self.failure_reason is not None:
            if (
                not isinstance(self.failure_reason, str)
                or not self.failure_reason.strip()
            ):
                raise ValueError("invalid_failure_reason")
            object.__setattr__(
                self,
                "failure_reason",
                self.failure_reason.strip(),
            )
        if self.status == "projected":
            if self.envelope is None:
                raise ValueError("projected_requires_envelope")
            if self.projected_chat_id is None:
                raise ValueError("projected_requires_chat_id")
            if self.failure_reason is not None:
                raise ValueError("projected_forbids_failure_reason")
        if self.status == "projection_send_failed":
            if self.envelope is None:
                raise ValueError("projection_send_failed_requires_envelope")
            if self.projected_chat_id is None:
                raise ValueError("projection_send_failed_requires_chat_id")
            if self.failure_reason is None:
                raise ValueError(
                    "projection_send_failed_requires_failure_reason"
                )
        if (
            self.envelope is not None
            and self.projected_chat_id is not None
            and self.envelope.message.chat_id != self.projected_chat_id
        ):
            raise ValueError(
                "projected_chat_id_envelope_chat_id_mismatch:"
                f"{self.projected_chat_id}!={self.envelope.message.chat_id}"
            )


class AgentBusProjectionService:
    def __init__(
        self,
        project_registry: ProjectRegistry,
        send_envelope: Callable[[OutgoingEnvelope], None],
    ) -> None:
        if not isinstance(project_registry, ProjectRegistry):
            raise ValueError(
                "invalid_project_registry_type:"
                f"{type(project_registry).__name__}"
            )
        if not callable(send_envelope):
            raise ValueError("send_envelope_not_callable")
        self._project_registry = project_registry
        self._send_envelope = send_envelope

    @property
    def project_registry(self) -> ProjectRegistry:
        return self._project_registry

    def resolve_target(
        self,
        thread: ProjectThread,
    ) -> AgentBusProjectionTarget | None:
        if not isinstance(thread, ProjectThread):
            raise ValueError(
                "invalid_project_thread_type:"
                f"{type(thread).__name__}"
            )
        snapshot = self._require_snapshot(thread.project_id)
        binding = snapshot.chat_binding
        if binding is None:
            return None
        if binding.chat_provider != "telegram":
            return None
        return AgentBusProjectionTarget(
            project_id=thread.project_id,
            chat_id=binding.chat_id,
            chat_provider=binding.chat_provider,
            thread_id=thread.thread_id,
            task_id=thread.task_id,
        )

    def format_public_projection(
        self,
        thread: ProjectThread,
        message: AgentMessage,
        *,
        project_slug: str | None = None,
    ) -> str:
        _validate_thread_message_scope(thread, message)
        if project_slug is not None:
            if not isinstance(project_slug, str) or not project_slug.strip():
                raise ValueError("invalid_project_slug")
            normalized_project_slug = project_slug.strip().lower()
        else:
            normalized_project_slug = None
        header = (
            "Межагентный запрос"
            if message.message_kind == "request"
            else "Межагентный ответ"
        )
        project_label = (
            thread.project_id
            if normalized_project_slug is None
            else f"{normalized_project_slug} ({thread.project_id})"
        )
        anchor_label = "Задача" if thread.task_id is not None else "Тред"
        anchor_value = (
            thread.task_id
            if thread.task_id is not None
            else thread.thread_id
        )
        return "\n".join(
            (
                header,
                f"Проект: {project_label}",
                f"{anchor_label}: {anchor_value}",
                f"Маршрут: {message.sender_role} -> {message.recipient_role}",
                "",
                message.body,
            )
        )

    def build_envelope(
        self,
        thread: ProjectThread,
        message: AgentMessage,
    ) -> OutgoingEnvelope | None:
        _validate_thread_message_scope(thread, message)
        snapshot = self._require_snapshot(thread.project_id)
        target = self.resolve_target(thread)
        if target is None:
            return None
        return OutgoingEnvelope(
            message=OutgoingMessage(
                chat_id=target.chat_id,
                text=self.format_public_projection(
                    thread,
                    message,
                    project_slug=snapshot.project.slug,
                ),
            ),
            sender_role=message.sender_role,
        )

    def project_message(
        self,
        thread: ProjectThread,
        message: AgentMessage,
    ) -> AgentBusProjectionResult:
        _validate_thread_message_scope(thread, message)
        snapshot = self._require_snapshot(thread.project_id)
        binding = snapshot.chat_binding
        if binding is None:
            return AgentBusProjectionResult(
                message=message,
                thread=thread,
                status="not_projected_no_chat_binding",
            )
        if binding.chat_provider != "telegram":
            return AgentBusProjectionResult(
                message=message,
                thread=thread,
                status="not_projected_unsupported_provider",
            )
        envelope = self.build_envelope(thread, message)
        if envelope is None:
            raise ValueError(
                "projection_target_missing_for_supported_provider:"
                f"{thread.project_id}:{thread.thread_id}"
            )
        try:
            self._send_envelope(envelope)
        except Exception as exc:
            return AgentBusProjectionResult(
                message=message,
                thread=thread,
                status="projection_send_failed",
                envelope=envelope,
                projected_chat_id=envelope.message.chat_id,
                failure_reason=_format_failure_reason(exc),
            )
        return AgentBusProjectionResult(
            message=message,
            thread=thread,
            status="projected",
            envelope=envelope,
            projected_chat_id=envelope.message.chat_id,
        )

    def _require_snapshot(self, project_id: str) -> ProjectSnapshot:
        normalized_project_id = _normalize_identifier(
            project_id,
            field_name="project_id",
        )
        snapshot = self._project_registry.get_project_snapshot(
            normalized_project_id
        )
        if snapshot is None:
            raise ValueError(f"unknown_project_id:{normalized_project_id}")
        return snapshot


class ProjectingAgentBus:
    def __init__(
        self,
        backend_bus: StateBackedAgentBus,
        projection_service: AgentBusProjectionService,
    ) -> None:
        if not isinstance(backend_bus, StateBackedAgentBus):
            raise ValueError(
                "invalid_state_backed_agent_bus_type:"
                f"{type(backend_bus).__name__}"
            )
        if not isinstance(projection_service, AgentBusProjectionService):
            raise ValueError(
                "invalid_agent_bus_projection_service_type:"
                f"{type(projection_service).__name__}"
            )
        self._backend_bus = backend_bus
        self._projection_service = projection_service

    @property
    def backend_bus(self) -> StateBackedAgentBus:
        return self._backend_bus

    @property
    def projection_service(self) -> AgentBusProjectionService:
        return self._projection_service

    def open_thread(
        self,
        *,
        project_id: str,
        opened_by_role: str,
        created_at: float,
        task_id: str | None = None,
    ) -> ProjectThread:
        return self._backend_bus.open_thread(
            project_id=project_id,
            opened_by_role=opened_by_role,
            created_at=created_at,
            task_id=task_id,
        )

    def get_thread(
        self,
        project_id: str,
        thread_id: str,
    ) -> ProjectThread | None:
        return self._backend_bus.get_thread(project_id, thread_id)

    def get_task_thread(
        self,
        project_id: str,
        task_id: str,
    ) -> ProjectThread | None:
        return self._backend_bus.get_task_thread(project_id, task_id)

    def get_or_open_task_thread(
        self,
        project_id: str,
        task_id: str,
        *,
        opened_by_role: str,
        created_at: float,
    ) -> ProjectThread:
        return self._backend_bus.get_or_open_task_thread(
            project_id,
            task_id,
            opened_by_role=opened_by_role,
            created_at=created_at,
        )

    def publish_request(
        self,
        request: AgentRequest,
    ) -> AgentBusProjectionResult:
        if not isinstance(request, AgentRequest):
            raise ValueError(
                f"invalid_request_type:{type(request).__name__}"
            )
        message = self._backend_bus.publish_request(request)
        return self._project_message(message)

    def publish_reply(
        self,
        reply: AgentReply,
    ) -> AgentBusProjectionResult:
        if not isinstance(reply, AgentReply):
            raise ValueError(
                f"invalid_reply_type:{type(reply).__name__}"
            )
        message = self._backend_bus.publish_reply(reply)
        return self._project_message(message)

    def list_thread_messages(
        self,
        project_id: str,
        thread_id: str,
    ) -> tuple[AgentMessage, ...]:
        return self._backend_bus.list_thread_messages(project_id, thread_id)

    def list_inbox(
        self,
        project_id: str,
        recipient_role: str,
    ) -> tuple[AgentMessage, ...]:
        return self._backend_bus.list_inbox(project_id, recipient_role)

    def _project_message(
        self,
        message: AgentMessage,
    ) -> AgentBusProjectionResult:
        thread = self._backend_bus.get_thread(
            message.project_id,
            message.thread_id,
        )
        if thread is None:
            raise ValueError(
                f"unknown_thread:{message.project_id}:{message.thread_id}"
            )
        return self._projection_service.project_message(thread, message)
