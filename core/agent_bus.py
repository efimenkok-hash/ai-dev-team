"""
core/agent_bus.py

Reference backend-native message bus for agent-to-agent exchange.

Scope for roadmap step P4.1:
1. Provide a small transport-agnostic bus API.
2. Keep the implementation in-memory and deterministic for tests.
3. Do not add persistence, runtime wiring, projection, or prompt
   integration on this step.
"""

from __future__ import annotations

import re
from dataclasses import replace

from core.agent_bus_models import (
    AgentMessage,
    AgentMessageRef,
    AgentReply,
    AgentRequest,
    ProjectThread,
)

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


class InMemoryAgentBus:
    def __init__(self) -> None:
        self._thread_counter = 0
        self._message_counters: dict[tuple[str, str], int] = {}
        self._threads_by_key: dict[tuple[str, str], ProjectThread] = {}
        self._messages_by_thread: dict[
            tuple[str, str],
            list[AgentMessage],
        ] = {}
        self._messages_by_key: dict[
            tuple[str, str, str],
            AgentMessage,
        ] = {}
        self._all_messages: list[AgentMessage] = []

    def open_thread(
        self,
        *,
        project_id: str,
        opened_by_role: str,
        created_at: float,
        task_id: str | None = None,
    ) -> ProjectThread:
        self._thread_counter += 1
        thread = ProjectThread(
            project_id=project_id,
            thread_id=f"thread_{self._thread_counter:06d}",
            opened_by_role=opened_by_role,
            status="open",
            created_at=created_at,
            last_message_at=created_at,
            task_id=task_id,
        )
        thread_key = (thread.project_id, thread.thread_id)
        self._threads_by_key[thread_key] = thread
        self._messages_by_thread[thread_key] = []
        self._message_counters[thread_key] = 0
        return thread

    def publish_request(self, request: AgentRequest) -> AgentMessage:
        if not isinstance(request, AgentRequest):
            raise ValueError(
                f"invalid_request_type:{type(request).__name__}"
            )
        message = AgentMessage(
            project_id=request.project_id,
            thread_id=request.thread_id,
            message_id=self._next_message_id(
                request.project_id,
                request.thread_id,
            ),
            sender_role=request.sender_role,
            recipient_role=request.recipient_role,
            message_kind="request",
            body=request.body,
            created_at=request.created_at,
            in_reply_to=None,
        )
        self._append_message(message)
        return message

    def publish_reply(self, reply: AgentReply) -> AgentMessage:
        if not isinstance(reply, AgentReply):
            raise ValueError(f"invalid_reply_type:{type(reply).__name__}")
        target = self._messages_by_key.get(
            (
                reply.in_reply_to.project_id,
                reply.in_reply_to.thread_id,
                reply.in_reply_to.message_id,
            )
        )
        if target is None:
            raise ValueError(
                f"unknown_in_reply_to:{reply.in_reply_to.message_id}"
            )
        if target.message_kind != "request":
            raise ValueError(
                "reply_target_must_be_request:"
                f"{target.message_kind}"
            )
        message = AgentMessage(
            project_id=reply.project_id,
            thread_id=reply.thread_id,
            message_id=self._next_message_id(reply.project_id, reply.thread_id),
            sender_role=reply.sender_role,
            recipient_role=reply.recipient_role,
            message_kind="reply",
            body=reply.body,
            created_at=reply.created_at,
            in_reply_to=AgentMessageRef(
                project_id=reply.in_reply_to.project_id,
                thread_id=reply.in_reply_to.thread_id,
                message_id=reply.in_reply_to.message_id,
            ),
        )
        self._append_message(message)
        return message

    def list_thread_messages(
        self,
        project_id: str,
        thread_id: str,
    ) -> tuple[AgentMessage, ...]:
        thread_key = self._normalize_thread_key(project_id, thread_id)
        return tuple(self._messages_by_thread.get(thread_key, ()))

    def list_inbox(
        self,
        project_id: str,
        recipient_role: str,
    ) -> tuple[AgentMessage, ...]:
        normalized_project_id = _normalize_identifier(
            project_id,
            field_name="project_id",
        )
        normalized_recipient_role = _normalize_identifier(
            recipient_role,
            field_name="recipient_role",
        )
        return tuple(
            message
            for message in self._all_messages
            if (
                message.project_id == normalized_project_id
                and message.recipient_role == normalized_recipient_role
            )
        )

    def _next_message_id(self, project_id: str, thread_id: str) -> str:
        thread = self._require_thread(project_id, thread_id)
        thread_key = (thread.project_id, thread.thread_id)
        self._message_counters[thread_key] += 1
        return f"msg_{self._message_counters[thread_key]:06d}"

    def _require_thread(self, project_id: str, thread_id: str) -> ProjectThread:
        thread_key = self._normalize_thread_key(project_id, thread_id)
        thread = self._threads_by_key.get(thread_key)
        if thread is None:
            raise ValueError(
                f"unknown_thread:{thread_key[0]}:{thread_key[1]}"
            )
        return thread

    def _normalize_thread_key(
        self,
        project_id: str,
        thread_id: str,
    ) -> tuple[str, str]:
        return (
            _normalize_identifier(project_id, field_name="project_id"),
            _normalize_identifier(thread_id, field_name="thread_id"),
        )

    def _append_message(self, message: AgentMessage) -> None:
        thread = self._require_thread(message.project_id, message.thread_id)
        if message.created_at < thread.last_message_at:
            raise ValueError(
                "message_created_at_before_thread_last_message_at:"
                f"{message.created_at}<{thread.last_message_at}"
            )
        thread_key = (thread.project_id, thread.thread_id)
        message_key = (
            message.project_id,
            message.thread_id,
            message.message_id,
        )
        self._messages_by_thread[thread_key].append(message)
        self._messages_by_key[message_key] = message
        self._all_messages.append(message)
        self._threads_by_key[thread_key] = replace(
            thread,
            last_message_at=message.created_at,
        )
