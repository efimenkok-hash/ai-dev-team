"""
core/agent_bus_projection_throttle.py

Burst-aware public projection throttling for backend agent-bus exchange.

Scope for roadmap step P4.6:
1. Keep backend bus / StateDB as the full source of truth.
2. Reduce project-chat spam only at the public projection layer.
3. Use deterministic coordinator-style summaries without LLM summarization.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from core.agent_bus_models import AgentMessage, AgentReply, AgentRequest, ProjectThread
from core.agent_bus_projection import AgentBusProjectionResult, ProjectingAgentBus
from core.coordinator_role import COORDINATOR_ROLE
from core.telegram_bridge import OutgoingEnvelope, OutgoingMessage

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _normalize_identifier(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"empty_{field_name}")
    normalized = value.strip().lower()
    if not normalized.isascii():
        raise ValueError(f"non_ascii_{field_name}")
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"invalid_{field_name}:{normalized}")
    return normalized


def _validate_positive_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"invalid_{field_name}:{value!r}")
    return value


def _validate_positive_float(value: float, *, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"invalid_{field_name}:{value!r}")
    return float(value)


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


@dataclass(frozen=True)
class AgentBusProjectionThrottlePolicy:
    raw_burst_limit: int = 2
    summary_batch_size: int = 3
    burst_window_seconds: float = 30.0
    preview_chars: int = 140

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "raw_burst_limit",
            _validate_positive_int(
                self.raw_burst_limit,
                field_name="raw_burst_limit",
            ),
        )
        object.__setattr__(
            self,
            "summary_batch_size",
            _validate_positive_int(
                self.summary_batch_size,
                field_name="summary_batch_size",
            ),
        )
        object.__setattr__(
            self,
            "burst_window_seconds",
            _validate_positive_float(
                self.burst_window_seconds,
                field_name="burst_window_seconds",
            ),
        )
        object.__setattr__(
            self,
            "preview_chars",
            _validate_positive_int(
                self.preview_chars,
                field_name="preview_chars",
            ),
        )
        if self.summary_batch_size < 2:
            raise ValueError(
                "summary_batch_size_too_small:"
                f"{self.summary_batch_size}"
            )


@dataclass(frozen=True)
class AgentBusProjectionBatchResult:
    source_message: AgentMessage
    thread: ProjectThread
    projection_results: tuple[AgentBusProjectionResult, ...] = ()
    suppressed_messages: tuple[AgentMessage, ...] = ()

    def __post_init__(self) -> None:
        _validate_thread_message_scope(self.thread, self.source_message)
        if not isinstance(self.projection_results, tuple):
            raise ValueError("projection_results_must_be_tuple")
        if not isinstance(self.suppressed_messages, tuple):
            raise ValueError("suppressed_messages_must_be_tuple")
        for result in self.projection_results:
            if not isinstance(result, AgentBusProjectionResult):
                raise ValueError(
                    "invalid_projection_result_type:"
                    f"{type(result).__name__}"
                )
            if result.thread != self.thread:
                raise ValueError(
                    "projection_result_thread_mismatch:"
                    f"{result.thread.thread_id}!={self.thread.thread_id}"
                )
            if result.message.project_id != self.thread.project_id:
                raise ValueError(
                    "projection_result_project_id_mismatch:"
                    f"{result.message.project_id}!={self.thread.project_id}"
                )
            if result.message.thread_id != self.thread.thread_id:
                raise ValueError(
                    "projection_result_thread_id_mismatch:"
                    f"{result.message.thread_id}!={self.thread.thread_id}"
                )
        for message in self.suppressed_messages:
            _validate_thread_message_scope(self.thread, message)


@dataclass
class _ThreadProjectionThrottleState:
    raw_count: int = 0
    last_activity_at: float | None = None
    deferred_batches: list[tuple[AgentMessage, ...]] = field(default_factory=list)
    suppressed_messages: list[AgentMessage] = field(default_factory=list)


class ThrottledProjectingAgentBus:
    def __init__(
        self,
        projecting_bus: ProjectingAgentBus,
        policy: AgentBusProjectionThrottlePolicy | None = None,
    ) -> None:
        if not isinstance(projecting_bus, ProjectingAgentBus):
            raise ValueError(
                "invalid_projecting_agent_bus_type:"
                f"{type(projecting_bus).__name__}"
            )
        if (
            policy is not None
            and not isinstance(policy, AgentBusProjectionThrottlePolicy)
        ):
            raise ValueError(
                "invalid_agent_bus_projection_throttle_policy_type:"
                f"{type(policy).__name__}"
            )
        self._projecting_bus = projecting_bus
        self._policy = (
            policy
            if policy is not None
            else AgentBusProjectionThrottlePolicy()
        )
        self._states: dict[tuple[str, str], _ThreadProjectionThrottleState] = {}

    @property
    def projecting_bus(self) -> ProjectingAgentBus:
        return self._projecting_bus

    @property
    def policy(self) -> AgentBusProjectionThrottlePolicy:
        return self._policy

    def publish_request(
        self,
        request: AgentRequest,
    ) -> AgentBusProjectionBatchResult:
        if not isinstance(request, AgentRequest):
            raise ValueError(
                f"invalid_request_type:{type(request).__name__}"
            )
        message = self._projecting_bus.backend_bus.publish_request(request)
        return self._handle_persisted_message(message)

    def publish_reply(
        self,
        reply: AgentReply,
    ) -> AgentBusProjectionBatchResult:
        if not isinstance(reply, AgentReply):
            raise ValueError(
                f"invalid_reply_type:{type(reply).__name__}"
            )
        message = self._projecting_bus.backend_bus.publish_reply(reply)
        return self._handle_persisted_message(message)

    def flush_thread(
        self,
        project_id: str,
        thread_id: str,
    ) -> tuple[AgentBusProjectionResult, ...]:
        thread_key = self._normalize_thread_key(project_id, thread_id)
        state = self._states.get(thread_key)
        if state is None or (
            not state.suppressed_messages and not state.deferred_batches
        ):
            return ()
        thread = self._projecting_bus.get_thread(*thread_key)
        if thread is None:
            raise ValueError(f"unknown_thread:{thread_key[0]}:{thread_key[1]}")
        return self._flush_pending_batches(thread, state)

    def flush_all(self) -> tuple[AgentBusProjectionResult, ...]:
        results: list[AgentBusProjectionResult] = []
        for project_id, thread_id in sorted(self._states.keys()):
            results.extend(self.flush_thread(project_id, thread_id))
        return tuple(results)

    def pending_summary_count(
        self,
        project_id: str,
        thread_id: str,
    ) -> int:
        thread_key = self._normalize_thread_key(project_id, thread_id)
        state = self._states.get(thread_key)
        if state is None:
            return 0
        return sum(len(batch) for batch in state.deferred_batches) + len(
            state.suppressed_messages
        )

    def _handle_persisted_message(
        self,
        message: AgentMessage,
    ) -> AgentBusProjectionBatchResult:
        thread = self._projecting_bus.get_thread(
            message.project_id,
            message.thread_id,
        )
        if thread is None:
            raise ValueError(
                f"unknown_thread:{message.project_id}:{message.thread_id}"
            )
        state = self._get_or_create_state(thread.project_id, thread.thread_id)
        stale_projection_results: tuple[AgentBusProjectionResult, ...] = ()
        stale_suppressed_messages: tuple[AgentMessage, ...] = ()
        if self._is_idle_gap(state, message.created_at):
            (
                stale_projection_results,
                stale_suppressed_messages,
            ) = self._close_stale_burst(thread, state)
        state.last_activity_at = message.created_at
        if state.suppressed_messages:
            result = self._append_suppressed_message(thread, message, state)
            return AgentBusProjectionBatchResult(
                source_message=message,
                thread=thread,
                projection_results=(
                    *stale_projection_results,
                    *result.projection_results,
                ),
                suppressed_messages=(
                    *stale_suppressed_messages,
                    *result.suppressed_messages,
                ),
            )
        if state.raw_count < self._policy.raw_burst_limit:
            state.raw_count += 1
            current_projection = (
                self._projecting_bus.projection_service.project_message(
                    thread,
                    message,
                )
            )
            return AgentBusProjectionBatchResult(
                source_message=message,
                thread=thread,
                projection_results=(
                    *stale_projection_results,
                    current_projection,
                ),
                suppressed_messages=stale_suppressed_messages,
            )
        result = self._append_suppressed_message(thread, message, state)
        return AgentBusProjectionBatchResult(
            source_message=message,
            thread=thread,
            projection_results=(
                *stale_projection_results,
                *result.projection_results,
            ),
            suppressed_messages=(
                *stale_suppressed_messages,
                *result.suppressed_messages,
            ),
        )

    def _is_idle_gap(
        self,
        state: _ThreadProjectionThrottleState,
        created_at: float,
    ) -> bool:
        return (
            state.last_activity_at is not None
            and (
                created_at - state.last_activity_at
                > self._policy.burst_window_seconds
            )
        )

    def _close_stale_burst(
        self,
        thread: ProjectThread,
        state: _ThreadProjectionThrottleState,
    ) -> tuple[tuple[AgentBusProjectionResult, ...], tuple[AgentMessage, ...]]:
        results: list[AgentBusProjectionResult] = []
        suppressed_messages = tuple(state.suppressed_messages)
        if suppressed_messages:
            result = self._project_suppressed_summary(
                thread,
                suppressed_messages,
            )
            results.append(result)
            if result.status == "projected":
                state.suppressed_messages.clear()
            else:
                state.deferred_batches.append(suppressed_messages)
                state.suppressed_messages.clear()
        state.raw_count = 0
        state.last_activity_at = None
        return tuple(results), suppressed_messages

    def _append_suppressed_message(
        self,
        thread: ProjectThread,
        message: AgentMessage,
        state: _ThreadProjectionThrottleState,
    ) -> AgentBusProjectionBatchResult:
        state.suppressed_messages.append(message)
        if len(state.suppressed_messages) < self._policy.summary_batch_size:
            return AgentBusProjectionBatchResult(
                source_message=message,
                thread=thread,
                suppressed_messages=(message,),
            )
        compacted_messages = tuple(state.suppressed_messages)
        result = self._project_suppressed_summary(thread, compacted_messages)
        if result.status == "projected":
            state.suppressed_messages.clear()
        return AgentBusProjectionBatchResult(
            source_message=message,
            thread=thread,
            projection_results=(result,),
            suppressed_messages=compacted_messages,
        )

    def _project_suppressed_summary(
        self,
        thread: ProjectThread,
        suppressed_messages: tuple[AgentMessage, ...],
    ) -> AgentBusProjectionResult:
        if not suppressed_messages:
            raise ValueError("empty_suppressed_messages")
        for message in suppressed_messages:
            _validate_thread_message_scope(thread, message)
        representative_message = suppressed_messages[-1]
        snapshot = self._projecting_bus.projection_service.project_registry.get_project_snapshot(
            thread.project_id
        )
        if snapshot is None:
            raise ValueError(f"unknown_project_id:{thread.project_id}")
        binding = snapshot.chat_binding
        if binding is None:
            return AgentBusProjectionResult(
                message=representative_message,
                thread=thread,
                status="not_projected_no_chat_binding",
            )
        if binding.chat_provider != "telegram":
            return AgentBusProjectionResult(
                message=representative_message,
                thread=thread,
                status="not_projected_unsupported_provider",
            )
        envelope = OutgoingEnvelope(
            message=OutgoingMessage(
                chat_id=binding.chat_id,
                text=self._format_summary_projection(
                    thread,
                    suppressed_messages,
                    project_slug=snapshot.project.slug,
                ),
            ),
            sender_role=COORDINATOR_ROLE,
        )
        try:
            self._projecting_bus.projection_service.send_envelope(envelope)
        except Exception as exc:
            return AgentBusProjectionResult(
                message=representative_message,
                thread=thread,
                status="projection_send_failed",
                envelope=envelope,
                projected_chat_id=envelope.message.chat_id,
                failure_reason=self._format_failure_reason(exc),
            )
        return AgentBusProjectionResult(
            message=representative_message,
            thread=thread,
            status="projected",
            envelope=envelope,
            projected_chat_id=envelope.message.chat_id,
        )

    def _format_summary_projection(
        self,
        thread: ProjectThread,
        suppressed_messages: tuple[AgentMessage, ...],
        *,
        project_slug: str | None,
    ) -> str:
        if project_slug is not None:
            project_label = f"{project_slug} ({thread.project_id})"
        else:
            project_label = thread.project_id
        anchor_label = "Задача" if thread.task_id is not None else "Тред"
        anchor_value = (
            thread.task_id
            if thread.task_id is not None
            else thread.thread_id
        )
        route_overview = self._format_route_overview(suppressed_messages)
        preview_lines = tuple(
            f"{index}. {message.sender_role} -> {message.recipient_role}: "
            f"{self._truncate_preview(message.body)}"
            for index, message in enumerate(suppressed_messages, start=1)
        )
        return "\n".join(
            (
                "Сжатая сводка внутреннего обмена команды",
                f"Проект: {project_label}",
                f"{anchor_label}: {anchor_value}",
                f"Сообщений: {len(suppressed_messages)}",
                f"Маршруты: {route_overview}",
                "",
                *preview_lines,
            )
        )

    def _format_route_overview(
        self,
        messages: tuple[AgentMessage, ...],
    ) -> str:
        routes: list[str] = []
        seen: set[str] = set()
        for message in messages:
            route = f"{message.sender_role} -> {message.recipient_role}"
            if route not in seen:
                seen.add(route)
                routes.append(route)
        return "; ".join(routes)

    def _truncate_preview(self, body: str) -> str:
        normalized = body.strip()
        if len(normalized) <= self._policy.preview_chars:
            return normalized
        if self._policy.preview_chars == 1:
            return normalized[:1]
        return normalized[: self._policy.preview_chars - 1] + "…"

    def _get_or_create_state(
        self,
        project_id: str,
        thread_id: str,
    ) -> _ThreadProjectionThrottleState:
        thread_key = (project_id, thread_id)
        state = self._states.get(thread_key)
        if state is None:
            state = _ThreadProjectionThrottleState()
            self._states[thread_key] = state
        return state

    def _normalize_thread_key(
        self,
        project_id: str,
        thread_id: str,
    ) -> tuple[str, str]:
        return (
            _normalize_identifier(project_id, field_name="project_id"),
            _normalize_identifier(thread_id, field_name="thread_id"),
        )

    def _flush_pending_batches(
        self,
        thread: ProjectThread,
        state: _ThreadProjectionThrottleState,
    ) -> tuple[AgentBusProjectionResult, ...]:
        results: list[AgentBusProjectionResult] = []
        while state.deferred_batches:
            batch = state.deferred_batches[0]
            result = self._project_suppressed_summary(thread, batch)
            results.append(result)
            if result.status != "projected":
                return tuple(results)
            state.deferred_batches.pop(0)
        if not state.suppressed_messages:
            return tuple(results)
        active_batch = tuple(state.suppressed_messages)
        result = self._project_suppressed_summary(thread, active_batch)
        results.append(result)
        if result.status == "projected":
            state.suppressed_messages.clear()
        return tuple(results)

    @staticmethod
    def _format_failure_reason(exc: Exception) -> str:
        detail = str(exc).strip()
        return (
            exc.__class__.__name__
            if not detail
            else f"{exc.__class__.__name__}:{detail}"
        )
