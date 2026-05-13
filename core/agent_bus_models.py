"""
core/agent_bus_models.py

Transport-agnostic backend message bus models for agent-to-agent exchange.

Scope for roadmap step P4.1:
1. Define immutable bus-domain entities only.
2. Validate and normalize every field eagerly in __post_init__.
3. Keep the model persistence-, runtime-, and transport-agnostic: no
   StateDB wiring, no Telegram types, no projection/UI fields.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

VALID_PROJECT_THREAD_STATUSES = frozenset({"open", "closed"})
VALID_AGENT_MESSAGE_KINDS = frozenset({"request", "reply"})


def _normalize_identifier(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"empty_{field_name}")
    normalized = value.strip().lower()
    if not normalized.isascii():
        raise ValueError(f"non_ascii_{field_name}")
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"invalid_{field_name}:{normalized}")
    return normalized


def _normalize_choice(
    value: str,
    *,
    field_name: str,
    allowed: frozenset[str],
) -> str:
    normalized = _normalize_identifier(value, field_name=field_name)
    if normalized not in allowed:
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


def _normalize_timestamp(value: float, *, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"invalid_{field_name}:{value!r}")
    return float(value)


def _normalize_body(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("empty_body")
    return value.strip()


@dataclass(frozen=True)
class AgentMessageRef:
    project_id: str
    thread_id: str
    message_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_id",
            _normalize_identifier(self.project_id, field_name="project_id"),
        )
        object.__setattr__(
            self,
            "thread_id",
            _normalize_identifier(self.thread_id, field_name="thread_id"),
        )
        object.__setattr__(
            self,
            "message_id",
            _normalize_identifier(self.message_id, field_name="message_id"),
        )


@dataclass(frozen=True)
class ProjectThread:
    project_id: str
    thread_id: str
    opened_by_role: str
    status: str = "open"
    created_at: float = 0.0
    last_message_at: float = 0.0
    task_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_id",
            _normalize_identifier(self.project_id, field_name="project_id"),
        )
        object.__setattr__(
            self,
            "thread_id",
            _normalize_identifier(self.thread_id, field_name="thread_id"),
        )
        object.__setattr__(
            self,
            "opened_by_role",
            _normalize_identifier(
                self.opened_by_role,
                field_name="opened_by_role",
            ),
        )
        object.__setattr__(
            self,
            "status",
            _normalize_choice(
                self.status,
                field_name="thread_status",
                allowed=VALID_PROJECT_THREAD_STATUSES,
            ),
        )
        object.__setattr__(
            self,
            "created_at",
            _normalize_timestamp(self.created_at, field_name="created_at"),
        )
        object.__setattr__(
            self,
            "last_message_at",
            _normalize_timestamp(
                self.last_message_at,
                field_name="last_message_at",
            ),
        )
        if self.last_message_at < self.created_at:
            raise ValueError("last_message_at_before_created_at")
        if self.task_id is not None:
            object.__setattr__(
                self,
                "task_id",
                _normalize_task_id(self.task_id, field_name="task_id"),
            )


@dataclass(frozen=True)
class AgentRequest:
    project_id: str
    thread_id: str
    sender_role: str
    recipient_role: str
    body: str
    created_at: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_id",
            _normalize_identifier(self.project_id, field_name="project_id"),
        )
        object.__setattr__(
            self,
            "thread_id",
            _normalize_identifier(self.thread_id, field_name="thread_id"),
        )
        object.__setattr__(
            self,
            "sender_role",
            _normalize_identifier(self.sender_role, field_name="sender_role"),
        )
        object.__setattr__(
            self,
            "recipient_role",
            _normalize_identifier(
                self.recipient_role,
                field_name="recipient_role",
            ),
        )
        object.__setattr__(self, "body", _normalize_body(self.body))
        object.__setattr__(
            self,
            "created_at",
            _normalize_timestamp(self.created_at, field_name="created_at"),
        )


@dataclass(frozen=True)
class AgentReply:
    project_id: str
    thread_id: str
    sender_role: str
    recipient_role: str
    in_reply_to: AgentMessageRef
    body: str
    created_at: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_id",
            _normalize_identifier(self.project_id, field_name="project_id"),
        )
        object.__setattr__(
            self,
            "thread_id",
            _normalize_identifier(self.thread_id, field_name="thread_id"),
        )
        object.__setattr__(
            self,
            "sender_role",
            _normalize_identifier(self.sender_role, field_name="sender_role"),
        )
        object.__setattr__(
            self,
            "recipient_role",
            _normalize_identifier(
                self.recipient_role,
                field_name="recipient_role",
            ),
        )
        if not isinstance(self.in_reply_to, AgentMessageRef):
            raise ValueError(
                "invalid_in_reply_to_type:"
                f"{type(self.in_reply_to).__name__}"
            )
        object.__setattr__(self, "body", _normalize_body(self.body))
        object.__setattr__(
            self,
            "created_at",
            _normalize_timestamp(self.created_at, field_name="created_at"),
        )
        if self.in_reply_to.project_id != self.project_id:
            raise ValueError(
                "in_reply_to_project_id_mismatch:"
                f"{self.in_reply_to.project_id}!={self.project_id}"
            )
        if self.in_reply_to.thread_id != self.thread_id:
            raise ValueError(
                "in_reply_to_thread_id_mismatch:"
                f"{self.in_reply_to.thread_id}!={self.thread_id}"
            )


@dataclass(frozen=True)
class AgentMessage:
    project_id: str
    thread_id: str
    message_id: str
    sender_role: str
    recipient_role: str
    message_kind: str
    body: str
    created_at: float
    in_reply_to: AgentMessageRef | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_id",
            _normalize_identifier(self.project_id, field_name="project_id"),
        )
        object.__setattr__(
            self,
            "thread_id",
            _normalize_identifier(self.thread_id, field_name="thread_id"),
        )
        object.__setattr__(
            self,
            "message_id",
            _normalize_identifier(self.message_id, field_name="message_id"),
        )
        object.__setattr__(
            self,
            "sender_role",
            _normalize_identifier(self.sender_role, field_name="sender_role"),
        )
        object.__setattr__(
            self,
            "recipient_role",
            _normalize_identifier(
                self.recipient_role,
                field_name="recipient_role",
            ),
        )
        object.__setattr__(
            self,
            "message_kind",
            _normalize_choice(
                self.message_kind,
                field_name="message_kind",
                allowed=VALID_AGENT_MESSAGE_KINDS,
            ),
        )
        object.__setattr__(self, "body", _normalize_body(self.body))
        object.__setattr__(
            self,
            "created_at",
            _normalize_timestamp(self.created_at, field_name="created_at"),
        )
        if self.in_reply_to is not None and not isinstance(
            self.in_reply_to,
            AgentMessageRef,
        ):
            raise ValueError(
                "invalid_in_reply_to_type:"
                f"{type(self.in_reply_to).__name__}"
            )
        if self.message_kind == "request" and self.in_reply_to is not None:
            raise ValueError("request_message_cannot_have_in_reply_to")
        if self.message_kind == "reply" and self.in_reply_to is None:
            raise ValueError("reply_message_requires_in_reply_to")
        if self.in_reply_to is None:
            return
        if self.in_reply_to.project_id != self.project_id:
            raise ValueError(
                "in_reply_to_project_id_mismatch:"
                f"{self.in_reply_to.project_id}!={self.project_id}"
            )
        if self.in_reply_to.thread_id != self.thread_id:
            raise ValueError(
                "in_reply_to_thread_id_mismatch:"
                f"{self.in_reply_to.thread_id}!={self.thread_id}"
            )
