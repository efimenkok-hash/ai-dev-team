"""
core/agent_dm_models.py

Typed owner-agent DM foundation models for the AI Office domain.

Scope through roadmap step E4.2:
1. Define immutable owner-agent DM session and transcript message entities.
2. Validate and normalize every field eagerly in __post_init__.
3. Keep the model persistence- and runtime-agnostic: no StateDB wiring,
   no TelegramBridge integration, no live DM capture, and no reply queue.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

VALID_AGENT_DM_CHAT_PROVIDERS = frozenset({"telegram"})
VALID_AGENT_DM_SESSION_STATUSES = frozenset({"active", "closed"})
VALID_AGENT_DM_MESSAGE_SENDER_KINDS = frozenset({"owner", "agent"})
DEFAULT_AGENT_DM_MESSAGE_MAXLEN = 20


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


def _validate_positive_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"invalid_{field_name}:{value!r}")
    return value


def _normalize_timestamp(value: float, *, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"invalid_{field_name}:{value!r}")
    return float(value)


@dataclass(frozen=True)
class AgentDmSession:
    owner_user_id: int
    project_id: str
    agent_role: str
    thread_bot_role: str
    dm_chat_id: int
    chat_provider: str = "telegram"
    status: str = "active"
    created_at: float = 0.0
    last_interaction_at: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "owner_user_id",
            _validate_positive_int(
                self.owner_user_id,
                field_name="owner_user_id",
            ),
        )
        object.__setattr__(
            self,
            "project_id",
            _normalize_identifier(self.project_id, field_name="project_id"),
        )
        object.__setattr__(
            self,
            "agent_role",
            _normalize_identifier(self.agent_role, field_name="agent_role"),
        )
        object.__setattr__(
            self,
            "thread_bot_role",
            _normalize_identifier(
                self.thread_bot_role,
                field_name="thread_bot_role",
            ),
        )
        object.__setattr__(
            self,
            "dm_chat_id",
            _validate_positive_int(self.dm_chat_id, field_name="dm_chat_id"),
        )
        object.__setattr__(
            self,
            "chat_provider",
            _normalize_choice(
                self.chat_provider,
                field_name="chat_provider",
                allowed=VALID_AGENT_DM_CHAT_PROVIDERS,
            ),
        )
        object.__setattr__(
            self,
            "status",
            _normalize_choice(
                self.status,
                field_name="session_status",
                allowed=VALID_AGENT_DM_SESSION_STATUSES,
            ),
        )
        object.__setattr__(
            self,
            "created_at",
            _normalize_timestamp(self.created_at, field_name="created_at"),
        )
        object.__setattr__(
            self,
            "last_interaction_at",
            _normalize_timestamp(
                self.last_interaction_at,
                field_name="last_interaction_at",
            ),
        )
        if self.last_interaction_at < self.created_at:
            raise ValueError("last_interaction_at_before_created_at")
        if (
            self.chat_provider == "telegram"
            and self.dm_chat_id != self.owner_user_id
        ):
            raise ValueError(
                "telegram_dm_chat_must_match_owner_user_id:"
                f"{self.dm_chat_id}!={self.owner_user_id}"
            )


@dataclass(frozen=True)
class AgentDmMessage:
    owner_user_id: int
    project_id: str
    agent_role: str
    sender_kind: str
    sender_role: str
    body: str
    created_at: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "owner_user_id",
            _validate_positive_int(
                self.owner_user_id,
                field_name="owner_user_id",
            ),
        )
        object.__setattr__(
            self,
            "project_id",
            _normalize_identifier(self.project_id, field_name="project_id"),
        )
        object.__setattr__(
            self,
            "agent_role",
            _normalize_identifier(self.agent_role, field_name="agent_role"),
        )
        object.__setattr__(
            self,
            "sender_kind",
            _normalize_choice(
                self.sender_kind,
                field_name="sender_kind",
                allowed=VALID_AGENT_DM_MESSAGE_SENDER_KINDS,
            ),
        )
        object.__setattr__(
            self,
            "sender_role",
            _normalize_identifier(
                self.sender_role,
                field_name="sender_role",
            ),
        )
        if not isinstance(self.body, str) or not self.body.strip():
            raise ValueError("empty_body")
        object.__setattr__(self, "body", self.body.strip())
        object.__setattr__(
            self,
            "created_at",
            _normalize_timestamp(self.created_at, field_name="created_at"),
        )
        if self.sender_kind == "owner" and self.sender_role != "owner":
            raise ValueError(
                "owner_sender_role_must_be_owner:"
                f"{self.sender_role}"
            )
        if self.sender_kind == "agent" and self.sender_role != self.agent_role:
            raise ValueError(
                "agent_sender_role_must_match_agent_role:"
                f"{self.sender_role}!={self.agent_role}"
            )
