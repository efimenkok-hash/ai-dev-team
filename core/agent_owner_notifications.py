"""
core/agent_owner_notifications.py

Typed owner-facing agent notification queue for personal Telegram DMs.

Scope through roadmap step E4.6:
1. Persist owner-facing agent-first DM notifications as queued rows.
2. Allow direct personal delivery only when an active DM session already
   exists for the same (owner, project, agent, thread bot).
3. Provide a truthful Coordinator fallback when personal DM is not yet open.
4. Mark delivered notifications and append them to the DM transcript only
   after a successful send.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.agent_dm_models import (
    DEFAULT_AGENT_DM_MESSAGE_MAXLEN,
    AgentDmMessage,
    AgentDmSession,
)
from core.coordinator_role import COORDINATOR_ROLE

if TYPE_CHECKING:
    from core.state_db import StateDB
    from core.telegram_bridge import BridgeReply

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VALID_CHAT_PROVIDERS = frozenset({"telegram"})
_VALID_NOTIFICATION_STATUSES = frozenset({"queued", "delivered"})
_VALID_DISPATCH_STATUSES = frozenset(
    {"direct_dm_ready", "queued_requires_coordinator"}
)


def _validate_positive_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"invalid_{field_name}:{value!r}")
    return value


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


def _normalize_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"empty_{field_name}")
    return value.strip()


def _normalize_timestamp(
    value: float | None,
    *,
    field_name: str,
) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"invalid_{field_name}:{value!r}")
    return float(value)


@dataclass(frozen=True)
class AgentOwnerNotification:
    notification_id: int | None
    owner_user_id: int
    project_id: str
    agent_role: str
    thread_bot_role: str
    body: str
    chat_provider: str = "telegram"
    status: str = "queued"
    created_at: float = 0.0
    delivered_at: float | None = None

    def __post_init__(self) -> None:
        if self.notification_id is not None:
            object.__setattr__(
                self,
                "notification_id",
                _validate_positive_int(
                    self.notification_id,
                    field_name="notification_id",
                ),
            )
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
            "body",
            _normalize_text(self.body, field_name="body"),
        )
        object.__setattr__(
            self,
            "chat_provider",
            _normalize_choice(
                self.chat_provider,
                field_name="chat_provider",
                allowed=_VALID_CHAT_PROVIDERS,
            ),
        )
        object.__setattr__(
            self,
            "status",
            _normalize_choice(
                self.status,
                field_name="notification_status",
                allowed=_VALID_NOTIFICATION_STATUSES,
            ),
        )
        object.__setattr__(
            self,
            "created_at",
            _normalize_timestamp(self.created_at, field_name="created_at"),
        )
        object.__setattr__(
            self,
            "delivered_at",
            _normalize_timestamp(
                self.delivered_at,
                field_name="delivered_at",
            ),
        )
        if self.status == "delivered" and self.delivered_at is None:
            raise ValueError("delivered_notification_requires_delivered_at")
        if self.status == "queued" and self.delivered_at is not None:
            raise ValueError("queued_notification_forbids_delivered_at")


@dataclass(frozen=True)
class AgentOwnerNotificationRequest:
    owner_user_id: int
    project_id: str
    agent_role: str
    thread_bot_role: str
    body: str

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
            "body",
            _normalize_text(self.body, field_name="body"),
        )
        if self.agent_role != self.thread_bot_role:
            raise ValueError(
                "agent_role_thread_bot_role_mismatch:"
                f"{self.agent_role}!={self.thread_bot_role}"
            )


@dataclass(frozen=True)
class AgentOwnerNotificationDispatchResult:
    status: str
    notification: AgentOwnerNotification
    session: AgentDmSession | None
    coordinator_fallback_reply: BridgeReply | None

    def __post_init__(self) -> None:
        from core.telegram_bridge import BridgeReply

        if not isinstance(self.status, str) or self.status not in _VALID_DISPATCH_STATUSES:
            raise ValueError(f"invalid_dispatch_status:{self.status!r}")
        if not isinstance(self.notification, AgentOwnerNotification):
            raise ValueError(
                "invalid_agent_owner_notification_type:"
                f"{type(self.notification).__name__}"
            )
        if self.session is not None and not isinstance(self.session, AgentDmSession):
            raise ValueError(
                "invalid_agent_dm_session_type:"
                f"{type(self.session).__name__}"
            )
        if (
            self.coordinator_fallback_reply is not None
            and not isinstance(self.coordinator_fallback_reply, BridgeReply)
        ):
            raise ValueError(
                "invalid_bridge_reply_type:"
                f"{type(self.coordinator_fallback_reply).__name__}"
            )
        if self.status == "direct_dm_ready":
            if self.session is None:
                raise ValueError("direct_dm_ready_requires_session")
            if self.coordinator_fallback_reply is not None:
                raise ValueError("direct_dm_ready_forbids_coordinator_fallback")
        if self.status == "queued_requires_coordinator":
            if self.session is not None:
                raise ValueError("queued_requires_coordinator_forbids_session")
            if self.coordinator_fallback_reply is None:
                raise ValueError(
                    "queued_requires_coordinator_requires_fallback_reply"
                )
        if self.session is not None:
            if self.notification.owner_user_id != self.session.owner_user_id:
                raise ValueError("dispatch_result_owner_user_id_mismatch")
            if self.notification.project_id != self.session.project_id:
                raise ValueError("dispatch_result_project_id_mismatch")
            if self.notification.agent_role != self.session.agent_role:
                raise ValueError("dispatch_result_agent_role_mismatch")


class AgentOwnerNotificationService:
    def __init__(
        self,
        state_db: StateDB,
        clock: Callable[[], float] | None = None,
    ) -> None:
        from core.state_db import StateDB

        if not isinstance(state_db, StateDB):
            raise ValueError(
                f"invalid_state_db_type:{type(state_db).__name__}"
            )
        if clock is not None and not callable(clock):
            raise ValueError("clock_not_callable")
        self._state_db = state_db
        self._clock = clock if clock is not None else _default_clock

    def dispatch_or_queue(
        self,
        request: AgentOwnerNotificationRequest,
    ) -> AgentOwnerNotificationDispatchResult:
        if not isinstance(request, AgentOwnerNotificationRequest):
            raise ValueError(
                "invalid_agent_owner_notification_request_type:"
                f"{type(request).__name__}"
            )
        now = _require_timestamp(
            self._clock(),
            field_name="notification_created_at",
        )
        notification = self._state_db.insert_agent_owner_notification(
            AgentOwnerNotification(
                notification_id=None,
                owner_user_id=request.owner_user_id,
                project_id=request.project_id,
                agent_role=request.agent_role,
                thread_bot_role=request.thread_bot_role,
                body=request.body,
                status="queued",
                created_at=now,
                delivered_at=None,
            )
        )
        session = self._state_db.get_agent_dm_session(
            request.owner_user_id,
            request.project_id,
            request.agent_role,
        )
        if (
            session is not None
            and session.status == "active"
            and session.thread_bot_role == request.thread_bot_role
        ):
            return AgentOwnerNotificationDispatchResult(
                status="direct_dm_ready",
                notification=notification,
                session=session,
                coordinator_fallback_reply=None,
            )
        return AgentOwnerNotificationDispatchResult(
            status="queued_requires_coordinator",
            notification=notification,
            session=None,
            coordinator_fallback_reply=self.build_coordinator_fallback_reply(
                request
            ),
        )

    def list_pending_for_session(
        self,
        session: AgentDmSession,
    ) -> tuple[AgentOwnerNotification, ...]:
        if not isinstance(session, AgentDmSession):
            raise ValueError(
                "invalid_agent_dm_session_type:"
                f"{type(session).__name__}"
            )
        if session.status != "active":
            raise ValueError(
                f"inactive_agent_dm_session:{session.status}"
            )
        return self._state_db.list_queued_agent_owner_notifications(
            session.owner_user_id,
            session.project_id,
            session.agent_role,
            session.thread_bot_role,
        )

    def build_agent_reply(
        self,
        notification: AgentOwnerNotification,
    ) -> BridgeReply:
        from core.telegram_bridge import BridgeReply

        if not isinstance(notification, AgentOwnerNotification):
            raise ValueError(
                "invalid_agent_owner_notification_type:"
                f"{type(notification).__name__}"
            )
        return BridgeReply(
            persona_role=notification.agent_role,
            body=notification.body,
        )

    def build_coordinator_fallback_reply(
        self,
        request: AgentOwnerNotificationRequest,
    ) -> BridgeReply:
        from core.telegram_bridge import BridgeReply

        if not isinstance(request, AgentOwnerNotificationRequest):
            raise ValueError(
                "invalid_agent_owner_notification_request_type:"
                f"{type(request).__name__}"
            )
        return BridgeReply(
            persona_role=COORDINATOR_ROLE,
            body=(
                "Личный DM этого агента для данного проекта ещё не активирован.\n"
                "\n"
                "Уведомление сохранено в очередь и появится, когда owner "
                "откроет личку с этим агентом.\n"
                "\n"
                "Это не запускало project pipeline."
            ),
        )

    def ack_delivered(
        self,
        notification: AgentOwnerNotification,
    ) -> AgentOwnerNotification:
        if not isinstance(notification, AgentOwnerNotification):
            raise ValueError(
                "invalid_agent_owner_notification_type:"
                f"{type(notification).__name__}"
            )
        if notification.notification_id is None:
            raise ValueError("notification_id_required_for_delivery_ack")
        delivered_at = _require_timestamp(
            self._clock(),
            field_name="notification_delivered_at",
        )
        updated_holder: dict[str, AgentOwnerNotification] = {}

        def _write(conn) -> None:
            delivered = self._state_db._mark_agent_owner_notification_delivered_conn(
                conn,
                notification.notification_id,
                delivered_at=delivered_at,
            )
            self._state_db._record_agent_dm_message_conn(
                conn,
                AgentDmMessage(
                    owner_user_id=delivered.owner_user_id,
                    project_id=delivered.project_id,
                    agent_role=delivered.agent_role,
                    sender_kind="agent",
                    sender_role=delivered.agent_role,
                    body=delivered.body,
                    created_at=delivered_at,
                ),
                max_entries=DEFAULT_AGENT_DM_MESSAGE_MAXLEN,
            )
            updated_holder["notification"] = delivered

        self._state_db._run_write_transaction(_write)
        return updated_holder["notification"]


def _default_clock() -> float:
    import time

    return time.time()


def _require_timestamp(value: float, *, field_name: str) -> float:
    normalized = _normalize_timestamp(value, field_name=field_name)
    if normalized is None:
        raise ValueError(f"invalid_{field_name}:None")
    return normalized
