"""
core/agent_dm_reply.py

Typed owner-agent DM reply service.

Scope through roadmap step E4.4:
1. Activate a direct owner-agent DM reply path only for secondary private
   agent-bot threads with single-project owner-DM fallback.
2. Keep the flow non-pipeline: no hidden task execution, no task-history
   side effects, no project-runtime work.
3. Build contextual prompts from persistent owner-agent transcript windows.
4. Persist session activation plus the owner/agent text exchange in StateDB
   only after a successful direct reply.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, replace

from core.agent_dm_context import AgentDmContextService, AgentDmPromptContext
from core.agent_dm_models import AgentDmMessage, AgentDmSession
from core.agent_personas import PersonaRegistry
from core.coordinator_role import COORDINATOR_ROLE
from core.llm_dispatcher import (
    LLMDispatcher,
    LLMDispatchError,
    LLMRequest,
)
from core.model_tier import TierRegistry
from core.model_tier import default_registry as default_tier_registry
from core.owner_dm_routing import OwnerDmRoutingService
from core.project_registry import ProjectSnapshot
from core.state_db import StateDB
from core.telegram_bridge import BridgeReply, IncomingMessage

_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VALID_PROJECT_CONTEXT_SOURCES = frozenset(
    {
        "owner_dm_single_project",
        "agent_dm_explicit_project",
        "agent_dm_active_session",
        "agent_dm_single_candidate",
    }
)


def _normalize_role(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid_{field_name}_type:{type(value).__name__}")
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError(f"empty_{field_name}")
    if not normalized.isascii():
        raise ValueError(f"non_ascii_{field_name}:{normalized}")
    if not _ROLE_RE.fullmatch(normalized):
        raise ValueError(f"invalid_{field_name}:{normalized}")
    return normalized


def _validate_positive_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"invalid_{field_name}:{value!r}")
    return value


def _normalize_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"empty_{field_name}")
    return value.strip()


def _normalize_timestamp(value: float, *, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"invalid_{field_name}:{value!r}")
    return float(value)


class _DirectReplyUnavailable(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("empty_direct_reply_unavailable_code")
        self.code = code.strip()
        self.detail = detail.strip() if isinstance(detail, str) else ""
        super().__init__(
            self.code if not self.detail else f"{self.code}:{self.detail}"
        )


@dataclass(frozen=True)
class AgentDmSingleReplyContext:
    snapshot: ProjectSnapshot
    owner_user_id: int
    dm_chat_id: int
    agent_role: str
    thread_bot_role: str
    owner_text: str
    project_context_source: str

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, ProjectSnapshot):
            raise ValueError(
                "invalid_project_snapshot_type:"
                f"{type(self.snapshot).__name__}"
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
            "dm_chat_id",
            _validate_positive_int(self.dm_chat_id, field_name="dm_chat_id"),
        )
        object.__setattr__(
            self,
            "agent_role",
            _normalize_role(self.agent_role, field_name="agent_role"),
        )
        object.__setattr__(
            self,
            "thread_bot_role",
            _normalize_role(
                self.thread_bot_role,
                field_name="thread_bot_role",
            ),
        )
        object.__setattr__(
            self,
            "owner_text",
            _normalize_text(self.owner_text, field_name="owner_text"),
        )
        normalized_source = _normalize_role(
            self.project_context_source,
            field_name="project_context_source",
        )
        if normalized_source not in _VALID_PROJECT_CONTEXT_SOURCES:
            raise ValueError(
                "invalid_project_context_source:"
                f"{normalized_source}"
            )
        object.__setattr__(
            self,
            "project_context_source",
            normalized_source,
        )
        if self.dm_chat_id != self.owner_user_id:
            raise ValueError(
                "owner_dm_requires_private_chat_shape:"
                f"{self.dm_chat_id}!={self.owner_user_id}"
            )
        if self.snapshot.project.owner_user_id != self.owner_user_id:
            raise ValueError(
                "owner_project_mismatch:"
                f"{self.snapshot.project.owner_user_id}!={self.owner_user_id}"
            )
        if self.agent_role != self.thread_bot_role:
            raise ValueError(
                "agent_role_thread_bot_role_mismatch:"
                f"{self.agent_role}!={self.thread_bot_role}"
            )


@dataclass(frozen=True)
class AgentDmSingleReplyResult:
    session: AgentDmSession
    owner_message: AgentDmMessage
    agent_message: AgentDmMessage
    reply: BridgeReply

    def __post_init__(self) -> None:
        if not isinstance(self.session, AgentDmSession):
            raise ValueError(
                "invalid_agent_dm_session_type:"
                f"{type(self.session).__name__}"
            )
        if not isinstance(self.owner_message, AgentDmMessage):
            raise ValueError(
                "invalid_owner_message_type:"
                f"{type(self.owner_message).__name__}"
            )
        if not isinstance(self.agent_message, AgentDmMessage):
            raise ValueError(
                "invalid_agent_message_type:"
                f"{type(self.agent_message).__name__}"
            )
        if not isinstance(self.reply, BridgeReply):
            raise ValueError(
                "invalid_bridge_reply_type:"
                f"{type(self.reply).__name__}"
            )
        if self.owner_message.sender_kind != "owner":
            raise ValueError("owner_message_sender_kind_must_be_owner")
        if self.agent_message.sender_kind != "agent":
            raise ValueError("agent_message_sender_kind_must_be_agent")
        if self.reply.persona_role != self.session.agent_role:
            raise ValueError(
                "reply_persona_role_session_agent_role_mismatch:"
                f"{self.reply.persona_role}!={self.session.agent_role}"
            )
        if (
            self.session.owner_user_id != self.owner_message.owner_user_id
            or self.session.owner_user_id != self.agent_message.owner_user_id
        ):
            raise ValueError("session_owner_user_id_mismatch")
        if (
            self.session.project_id != self.owner_message.project_id
            or self.session.project_id != self.agent_message.project_id
        ):
            raise ValueError("session_project_id_mismatch")
        if (
            self.session.agent_role != self.owner_message.agent_role
            or self.session.agent_role != self.agent_message.agent_role
        ):
            raise ValueError("session_agent_role_mismatch")


class AgentDmSingleReplyService:
    def __init__(
        self,
        dispatcher: LLMDispatcher | None,
        state_db: StateDB,
        personas: PersonaRegistry,
        clock: Callable[[], float] | None = None,
        tier_registry: TierRegistry | None = None,
        context_service: AgentDmContextService | None = None,
    ) -> None:
        if dispatcher is not None and not isinstance(dispatcher, LLMDispatcher):
            raise ValueError(
                "invalid_dispatcher_type:"
                f"{type(dispatcher).__name__}"
            )
        if not isinstance(state_db, StateDB):
            raise ValueError(
                f"invalid_state_db_type:{type(state_db).__name__}"
            )
        if not isinstance(personas, PersonaRegistry):
            raise ValueError(
                f"invalid_persona_registry_type:{type(personas).__name__}"
            )
        if clock is not None and not callable(clock):
            raise ValueError("clock_not_callable")
        if tier_registry is not None and not isinstance(tier_registry, TierRegistry):
            raise ValueError(
                "invalid_tier_registry_type:"
                f"{type(tier_registry).__name__}"
            )
        if (
            context_service is not None
            and not isinstance(context_service, AgentDmContextService)
        ):
            raise ValueError(
                "invalid_agent_dm_context_service_type:"
                f"{type(context_service).__name__}"
            )
        self._dispatcher = dispatcher
        self._state_db = state_db
        self._personas = personas
        self._clock = clock if clock is not None else _default_clock
        self._tier_registry = (
            tier_registry if tier_registry is not None else default_tier_registry()
        )
        self._context_service = (
            context_service
            if context_service is not None
            else AgentDmContextService(state_db)
        )
        self._owner_dm_routing = OwnerDmRoutingService()

    def is_direct_reply_candidate(self, msg: IncomingMessage) -> bool:
        if not isinstance(msg, IncomingMessage):
            return False
        if not self._owner_dm_routing.is_owner_dm_message(msg):
            return False
        if msg.incoming_bot_role is None:
            return False
        if msg.incoming_bot_role == COORDINATOR_ROLE:
            return False
        if msg.project_context_source not in _VALID_PROJECT_CONTEXT_SOURCES:
            return False
        if msg.text is None or not msg.text.strip():
            return False
        try:
            self._personas.for_role(msg.incoming_bot_role)
        except KeyError:
            return False
        return True

    def build_context(
        self,
        msg: IncomingMessage,
        snapshot: ProjectSnapshot,
    ) -> AgentDmSingleReplyContext:
        if not isinstance(msg, IncomingMessage):
            raise ValueError(
                "invalid_incoming_message_type:"
                f"{type(msg).__name__}"
            )
        if msg.incoming_bot_role is None:
            raise ValueError("missing_incoming_bot_role")
        try:
            self._personas.for_role(msg.incoming_bot_role)
        except KeyError as exc:
            raise ValueError(
                f"unknown_agent_role:{msg.incoming_bot_role}"
            ) from exc
        return AgentDmSingleReplyContext(
            snapshot=snapshot,
            owner_user_id=msg.user_id,
            dm_chat_id=msg.chat_id,
            agent_role=msg.incoming_bot_role,
            thread_bot_role=msg.incoming_bot_role,
            owner_text=msg.text or "",
            project_context_source=msg.project_context_source or "",
        )

    def reply_once(
        self,
        context: AgentDmSingleReplyContext,
        *,
        tier_name: str,
    ) -> AgentDmSingleReplyResult:
        if not isinstance(context, AgentDmSingleReplyContext):
            raise ValueError(
                "invalid_agent_dm_single_reply_context_type:"
                f"{type(context).__name__}"
            )
        if not isinstance(tier_name, str) or not tier_name.strip():
            raise ValueError("empty_tier_name")
        if self._dispatcher is None:
            raise _DirectReplyUnavailable("dispatcher_unavailable")
        try:
            tier = self._tier_registry.get(tier_name.strip())
        except KeyError as exc:
            raise ValueError(f"unknown_tier:{tier_name.strip()}") from exc

        prompt_context = self._context_service.build_prompt_context(context)
        request = self._build_request(prompt_context)
        try:
            response = self._dispatcher.dispatch(request, tier)
        except LLMDispatchError as exc:
            raise _DirectReplyUnavailable(
                "dispatch_failed",
                f"{exc.code}:{exc.detail}" if exc.detail else exc.code,
            ) from exc

        reply_body = response.text.strip()
        if not reply_body:
            raise _DirectReplyUnavailable("empty_llm_response")

        now = _normalize_timestamp(
            self._clock(),
            field_name="reply_timestamp",
        )
        session = self._activate_session(context, now=now)
        owner_message = AgentDmMessage(
            owner_user_id=context.owner_user_id,
            project_id=context.snapshot.project.project_id,
            agent_role=context.agent_role,
            sender_kind="owner",
            sender_role="owner",
            body=context.owner_text,
            created_at=now,
        )
        agent_message = AgentDmMessage(
            owner_user_id=context.owner_user_id,
            project_id=context.snapshot.project.project_id,
            agent_role=context.agent_role,
            sender_kind="agent",
            sender_role=context.agent_role,
            body=reply_body,
            created_at=now,
        )
        self._state_db.record_agent_dm_message(owner_message)
        self._state_db.record_agent_dm_message(agent_message)
        reply = BridgeReply(
            persona_role=context.agent_role,
            body=reply_body,
        )
        return AgentDmSingleReplyResult(
            session=session,
            owner_message=owner_message,
            agent_message=agent_message,
            reply=reply,
        )

    def ensure_active_session(
        self,
        context: AgentDmSingleReplyContext,
    ) -> AgentDmSession:
        if not isinstance(context, AgentDmSingleReplyContext):
            raise ValueError(
                "invalid_agent_dm_single_reply_context_type:"
                f"{type(context).__name__}"
            )
        now = _normalize_timestamp(
            self._clock(),
            field_name="session_activation_timestamp",
        )
        return self._activate_session(context, now=now)

    def reply_or_unavailable(
        self,
        context: AgentDmSingleReplyContext,
        *,
        tier_name: str,
    ) -> AgentDmSingleReplyResult | BridgeReply:
        try:
            return self.reply_once(context, tier_name=tier_name)
        except _DirectReplyUnavailable as exc:
            return BridgeReply(
                persona_role=context.agent_role,
                body=self._format_unavailable_body(exc),
            )

    def _build_request(
        self,
        prompt_context: AgentDmPromptContext,
    ) -> LLMRequest:
        persona = self._personas.for_role(prompt_context.agent_role)
        base_messages = self._context_service.build_dispatch_messages(prompt_context)
        system_message = {
            "role": base_messages[0]["role"],
            "content": (
                f"{base_messages[0]['content']}\n"
                f"Persona label: {persona.human_name} / {persona.title}."
            ),
        }
        messages = (system_message, *base_messages[1:])
        return LLMRequest(
            agent_role=prompt_context.agent_role,
            messages=messages,
            max_tokens=900,
            temperature=0.2,
        )

    def _activate_session(
        self,
        context: AgentDmSingleReplyContext,
        *,
        now: float,
    ) -> AgentDmSession:
        self._close_other_agent_sessions(context)
        existing = self._state_db.get_agent_dm_session(
            context.owner_user_id,
            context.snapshot.project.project_id,
            context.agent_role,
        )
        created_at = existing.created_at if existing is not None else now
        session = AgentDmSession(
            owner_user_id=context.owner_user_id,
            project_id=context.snapshot.project.project_id,
            agent_role=context.agent_role,
            thread_bot_role=context.thread_bot_role,
            dm_chat_id=context.dm_chat_id,
            status="active",
            created_at=created_at,
            last_interaction_at=now,
        )
        self._state_db.upsert_agent_dm_session(session)
        return session

    def _close_other_agent_sessions(
        self,
        context: AgentDmSingleReplyContext,
    ) -> None:
        for session in self._state_db.list_agent_dm_sessions_for_owner(
            context.owner_user_id
        ):
            if session.agent_role != context.agent_role:
                continue
            if session.project_id == context.snapshot.project.project_id:
                continue
            if session.status == "closed":
                continue
            self._state_db.upsert_agent_dm_session(
                replace(session, status="closed")
            )

    def _format_unavailable_body(
        self,
        exc: _DirectReplyUnavailable,
    ) -> str:
        if exc.code == "dispatcher_unavailable":
            return (
                "Сейчас не могу дать личный single-shot ответ: direct DM "
                "модель не подключена.\n"
                "\n"
                "Это не запускало project pipeline и ничего не меняло в "
                "проекте.\n"
                "\n"
                "Для реального выполнения используй coordinator / project chat."
            )
        return (
            "Сейчас не могу дать личный single-shot ответ: модель агента "
            "временно недоступна.\n"
            "\n"
            "Это не запускало project pipeline и ничего не меняло в проекте."
            + (
                f"\n\nТехническая причина: `{exc.detail}`"
                if exc.detail
                else ""
            )
            + "\n\nДля реального выполнения используй coordinator / project chat."
        )


def _default_clock() -> float:
    import time

    return time.time()
