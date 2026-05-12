"""
core/agent_dm_context.py

Typed contextual DM prompt assembly for owner-agent direct replies.

Scope for roadmap step E4.4:
1. Load persistent transcript windows from StateDB for one
   (owner, project, agent) conversation.
2. Build deterministic prompt context for direct owner-agent DM replies.
3. Keep the layer prompt-only: no runtime routing, no transcript writes,
   no project execution side effects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.agent_dm_models import (
    DEFAULT_AGENT_DM_MESSAGE_MAXLEN,
    AgentDmMessage,
    AgentDmSession,
)
from core.project_registry import ProjectSnapshot
from core.state_db import StateDB

if TYPE_CHECKING:
    from core.agent_dm_reply import AgentDmSingleReplyContext

_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _normalize_identifier(value: str, *, field_name: str) -> str:
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


def _role_label(role: str) -> str:
    normalized = _normalize_identifier(role, field_name="agent_role")
    return " ".join(part.capitalize() for part in normalized.split("_"))


@dataclass(frozen=True)
class AgentDmTranscriptWindow:
    owner_user_id: int
    project_id: str
    agent_role: str
    messages: tuple[AgentDmMessage, ...]

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
        if not isinstance(self.messages, tuple):
            raise ValueError("messages_must_be_tuple")
        previous_timestamp: float | None = None
        for message in self.messages:
            if not isinstance(message, AgentDmMessage):
                raise ValueError(
                    "invalid_agent_dm_message_type:"
                    f"{type(message).__name__}"
                )
            if message.owner_user_id != self.owner_user_id:
                raise ValueError(
                    "transcript_owner_user_id_mismatch:"
                    f"{message.owner_user_id}!={self.owner_user_id}"
                )
            if message.project_id != self.project_id:
                raise ValueError(
                    "transcript_project_id_mismatch:"
                    f"{message.project_id}!={self.project_id}"
                )
            if message.agent_role != self.agent_role:
                raise ValueError(
                    "transcript_agent_role_mismatch:"
                    f"{message.agent_role}!={self.agent_role}"
                )
            if (
                previous_timestamp is not None
                and message.created_at < previous_timestamp
            ):
                raise ValueError("transcript_messages_not_chronological")
            previous_timestamp = message.created_at


@dataclass(frozen=True)
class AgentDmPromptContext:
    snapshot: ProjectSnapshot
    session: AgentDmSession | None
    transcript: AgentDmTranscriptWindow
    owner_text: str
    agent_role: str
    thread_bot_role: str

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, ProjectSnapshot):
            raise ValueError(
                "invalid_project_snapshot_type:"
                f"{type(self.snapshot).__name__}"
            )
        if self.session is not None and not isinstance(self.session, AgentDmSession):
            raise ValueError(
                "invalid_agent_dm_session_type:"
                f"{type(self.session).__name__}"
            )
        if not isinstance(self.transcript, AgentDmTranscriptWindow):
            raise ValueError(
                "invalid_agent_dm_transcript_window_type:"
                f"{type(self.transcript).__name__}"
            )
        object.__setattr__(
            self,
            "owner_text",
            _normalize_text(self.owner_text, field_name="owner_text"),
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
        project_id = self.snapshot.project.project_id
        if self.transcript.project_id != project_id:
            raise ValueError(
                "prompt_context_transcript_project_id_mismatch:"
                f"{self.transcript.project_id}!={project_id}"
            )
        if self.transcript.agent_role != self.agent_role:
            raise ValueError(
                "prompt_context_transcript_agent_role_mismatch:"
                f"{self.transcript.agent_role}!={self.agent_role}"
            )
        if self.transcript.owner_user_id != self.snapshot.project.owner_user_id:
            raise ValueError(
                "prompt_context_transcript_owner_user_id_mismatch:"
                f"{self.transcript.owner_user_id}!="
                f"{self.snapshot.project.owner_user_id}"
            )
        if self.session is not None:
            if self.session.project_id != project_id:
                raise ValueError(
                    "prompt_context_session_project_id_mismatch:"
                    f"{self.session.project_id}!={project_id}"
                )
            if self.session.agent_role != self.agent_role:
                raise ValueError(
                    "prompt_context_session_agent_role_mismatch:"
                    f"{self.session.agent_role}!={self.agent_role}"
                )
            if self.session.thread_bot_role != self.thread_bot_role:
                raise ValueError(
                    "prompt_context_session_thread_bot_role_mismatch:"
                    f"{self.session.thread_bot_role}!={self.thread_bot_role}"
                )


class AgentDmContextService:
    def __init__(
        self,
        state_db: StateDB,
        transcript_limit: int = DEFAULT_AGENT_DM_MESSAGE_MAXLEN,
    ) -> None:
        if not isinstance(state_db, StateDB):
            raise ValueError(
                f"invalid_state_db_type:{type(state_db).__name__}"
            )
        self._validate_limit(transcript_limit, field_name="transcript_limit")
        self._state_db = state_db
        self._transcript_limit = transcript_limit

    def load_transcript(
        self,
        owner_user_id: int,
        project_id: str,
        agent_role: str,
        *,
        limit: int | None = None,
    ) -> AgentDmTranscriptWindow:
        normalized_owner_user_id = _validate_positive_int(
            owner_user_id,
            field_name="owner_user_id",
        )
        normalized_project_id = _normalize_identifier(
            project_id,
            field_name="project_id",
        )
        normalized_agent_role = _normalize_identifier(
            agent_role,
            field_name="agent_role",
        )
        resolved_limit = (
            self._transcript_limit
            if limit is None
            else self._validate_limit(limit, field_name="limit")
        )
        return AgentDmTranscriptWindow(
            owner_user_id=normalized_owner_user_id,
            project_id=normalized_project_id,
            agent_role=normalized_agent_role,
            messages=self._state_db.list_agent_dm_messages(
                normalized_owner_user_id,
                normalized_project_id,
                normalized_agent_role,
                limit=resolved_limit,
            ),
        )

    def build_prompt_context(
        self,
        context: AgentDmSingleReplyContext,
    ) -> AgentDmPromptContext:
        from core.agent_dm_reply import AgentDmSingleReplyContext

        if not isinstance(context, AgentDmSingleReplyContext):
            raise ValueError(
                "invalid_agent_dm_single_reply_context_type:"
                f"{type(context).__name__}"
            )
        project_id = context.snapshot.project.project_id
        session = self._state_db.get_agent_dm_session(
            context.owner_user_id,
            project_id,
            context.agent_role,
        )
        transcript = self.load_transcript(
            context.owner_user_id,
            project_id,
            context.agent_role,
        )
        return AgentDmPromptContext(
            snapshot=context.snapshot,
            session=session,
            transcript=transcript,
            owner_text=context.owner_text,
            agent_role=context.agent_role,
            thread_bot_role=context.thread_bot_role,
        )

    def build_dispatch_messages(
        self,
        prompt_context: AgentDmPromptContext,
    ) -> tuple[dict[str, str], ...]:
        if not isinstance(prompt_context, AgentDmPromptContext):
            raise ValueError(
                "invalid_agent_dm_prompt_context_type:"
                f"{type(prompt_context).__name__}"
            )
        project = prompt_context.snapshot.project
        agent_label = _role_label(prompt_context.agent_role)
        system_message = {
            "role": "system",
            "content": (
                "Ты отвечаешь owner'у в личке как конкретный агент AI Office.\n"
                f"Твоя роль: {agent_label} ({prompt_context.agent_role}).\n"
                "Это private owner-agent DM.\n"
                f"Проект: {project.slug} ({project.project_id}).\n"
                "Используй историю последних сообщений ниже, чтобы сохранять "
                "контекст разговора по этому проекту.\n"
                "Не утверждай, что уже изменил код, запустил тесты, создал "
                "ветку, commit, PR или выполнил project pipeline work.\n"
                "Если owner просит реальное выполнение, честно скажи, что "
                "execution идёт через coordinator / project chat.\n"
                "Отвечай по-русски, коротко, по делу и без подписи имени."
            ),
        }
        history = tuple(
            self._message_to_dispatch_entry(message, agent_label)
            for message in prompt_context.transcript.messages
        )
        current_message = {
            "role": "user",
            "content": f"Owner: {prompt_context.owner_text}",
        }
        return (system_message, *history, current_message)

    @staticmethod
    def _validate_limit(value: int, *, field_name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"invalid_{field_name}:{value!r}")
        return value

    @staticmethod
    def _message_to_dispatch_entry(
        message: AgentDmMessage,
        agent_label: str,
    ) -> dict[str, str]:
        if message.sender_kind == "owner":
            return {
                "role": "user",
                "content": f"Owner: {message.body}",
            }
        return {
            "role": "assistant",
            "content": f"{agent_label}: {message.body}",
        }
