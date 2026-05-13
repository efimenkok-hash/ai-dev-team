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
import sqlite3
from dataclasses import replace

from core.agent_bus_models import (
    AgentMessage,
    AgentMessageRef,
    AgentReply,
    AgentRequest,
    ProjectThread,
)
from core.state_db import StateDB

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_THREAD_ID_PREFIX = "thread_"
_MESSAGE_ID_PREFIX = "msg_"


def _normalize_identifier(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"empty_{field_name}")
    normalized = value.strip().lower()
    if not normalized.isascii():
        raise ValueError(f"non_ascii_{field_name}")
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"invalid_{field_name}:{normalized}")
    return normalized


def _next_sequenced_identifier(
    existing_ids: tuple[str, ...],
    *,
    prefix: str,
    field_name: str,
) -> str:
    next_number = 1
    for existing_id in existing_ids:
        normalized_id = _normalize_identifier(
            existing_id,
            field_name=field_name,
        )
        if not normalized_id.startswith(prefix):
            raise ValueError(
                f"invalid_persisted_{field_name}:{normalized_id}"
            )
        suffix = normalized_id[len(prefix):]
        if not suffix or not suffix.isdigit():
            raise ValueError(
                f"invalid_persisted_{field_name}:{normalized_id}"
            )
        next_number = max(next_number, int(suffix) + 1)
    return f"{prefix}{next_number:06d}"


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


class StateBackedAgentBus:
    def __init__(self, state_db: StateDB) -> None:
        if not isinstance(state_db, StateDB):
            raise ValueError(
                f"invalid_state_db_type:{type(state_db).__name__}"
            )
        self._state_db = state_db

    @property
    def state_db(self) -> StateDB:
        return self._state_db

    def open_thread(
        self,
        *,
        project_id: str,
        opened_by_role: str,
        created_at: float,
        task_id: str | None = None,
    ) -> ProjectThread:
        normalized_project_id = self._state_db._normalize_project_identifier(
            project_id,
            field_name="project_id",
        )
        normalized_opened_by_role = _normalize_identifier(
            opened_by_role,
            field_name="opened_by_role",
        )
        return self._state_db._run_write_transaction(
            lambda conn: self._open_thread_conn(
                conn,
                project_id=normalized_project_id,
                opened_by_role=normalized_opened_by_role,
                created_at=created_at,
                task_id=task_id,
            )
        )

    def publish_request(self, request: AgentRequest) -> AgentMessage:
        if not isinstance(request, AgentRequest):
            raise ValueError(
                f"invalid_request_type:{type(request).__name__}"
            )
        return self._state_db._run_write_transaction(
            lambda conn: self._publish_request_conn(conn, request)
        )

    def publish_reply(self, reply: AgentReply) -> AgentMessage:
        if not isinstance(reply, AgentReply):
            raise ValueError(f"invalid_reply_type:{type(reply).__name__}")
        return self._state_db._run_write_transaction(
            lambda conn: self._publish_reply_conn(conn, reply)
        )

    def get_thread(
        self,
        project_id: str,
        thread_id: str,
    ) -> ProjectThread | None:
        return self._state_db.get_project_thread(project_id, thread_id)

    def get_task_thread(
        self,
        project_id: str,
        task_id: str,
    ) -> ProjectThread | None:
        return self._state_db.get_project_thread_by_task(project_id, task_id)

    def get_or_open_task_thread(
        self,
        project_id: str,
        task_id: str,
        *,
        opened_by_role: str,
        created_at: float,
    ) -> ProjectThread:
        normalized_project_id = self._state_db._normalize_project_identifier(
            project_id,
            field_name="project_id",
        )
        normalized_task_id = self._state_db._normalize_task_identifier(
            task_id,
            field_name="task_id",
        )
        normalized_opened_by_role = _normalize_identifier(
            opened_by_role,
            field_name="opened_by_role",
        )
        return self._state_db._run_write_transaction(
            lambda conn: self._get_or_open_task_thread_conn(
                conn,
                project_id=normalized_project_id,
                task_id=normalized_task_id,
                opened_by_role=normalized_opened_by_role,
                created_at=created_at,
            )
        )

    def list_thread_messages(
        self,
        project_id: str,
        thread_id: str,
    ) -> tuple[AgentMessage, ...]:
        return self._state_db.list_agent_bus_messages(project_id, thread_id)

    def list_inbox(
        self,
        project_id: str,
        recipient_role: str,
    ) -> tuple[AgentMessage, ...]:
        return self._state_db.list_agent_bus_inbox(project_id, recipient_role)

    def _open_thread_conn(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str,
        opened_by_role: str,
        created_at: float,
        task_id: str | None,
    ) -> ProjectThread:
        self._state_db._ensure_project_exists(conn, project_id)
        thread = ProjectThread(
            project_id=project_id,
            thread_id=self._allocate_thread_id_conn(conn),
            opened_by_role=opened_by_role,
            status="open",
            created_at=created_at,
            last_message_at=created_at,
            task_id=task_id,
        )
        self._state_db._upsert_project_thread_conn(conn, thread)
        return thread

    def _publish_request_conn(
        self,
        conn: sqlite3.Connection,
        request: AgentRequest,
    ) -> AgentMessage:
        thread = self._require_open_thread_conn(
            conn,
            request.project_id,
            request.thread_id,
        )
        message = AgentMessage(
            project_id=request.project_id,
            thread_id=request.thread_id,
            message_id=self._allocate_message_id_conn(
                conn,
                thread.project_id,
                thread.thread_id,
            ),
            sender_role=request.sender_role,
            recipient_role=request.recipient_role,
            message_kind="request",
            body=request.body,
            created_at=request.created_at,
            in_reply_to=None,
        )
        self._state_db._insert_agent_bus_message_conn(conn, message)
        return message

    def _publish_reply_conn(
        self,
        conn: sqlite3.Connection,
        reply: AgentReply,
    ) -> AgentMessage:
        thread = self._require_open_thread_conn(
            conn,
            reply.project_id,
            reply.thread_id,
        )
        message = AgentMessage(
            project_id=reply.project_id,
            thread_id=reply.thread_id,
            message_id=self._allocate_message_id_conn(
                conn,
                thread.project_id,
                thread.thread_id,
            ),
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
        self._state_db._insert_agent_bus_message_conn(conn, message)
        return message

    def _get_or_open_task_thread_conn(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str,
        task_id: str,
        opened_by_role: str,
        created_at: float,
    ) -> ProjectThread:
        row = self._state_db._get_project_thread_by_task_row(
            conn,
            project_id,
            task_id,
        )
        thread = self._state_db._row_to_project_thread(row)
        if thread is not None:
            if thread.status != "open":
                raise ValueError(
                    f"project_task_thread_closed:{project_id}:{task_id}"
                )
            return thread
        return self._open_thread_conn(
            conn,
            project_id=project_id,
            opened_by_role=opened_by_role,
            created_at=created_at,
            task_id=task_id,
        )

    def _allocate_thread_id_conn(self, conn: sqlite3.Connection) -> str:
        rows = conn.execute(
            """
            SELECT thread_id
            FROM project_threads
            ORDER BY project_id ASC, thread_id ASC
            """
        ).fetchall()
        return _next_sequenced_identifier(
            tuple(str(row["thread_id"]) for row in rows),
            prefix=_THREAD_ID_PREFIX,
            field_name="thread_id",
        )

    def _allocate_message_id_conn(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        thread_id: str,
    ) -> str:
        rows = conn.execute(
            """
            SELECT message_id
            FROM agent_bus_messages
            WHERE project_id = ? AND thread_id = ?
            ORDER BY id ASC
            """,
            (project_id, thread_id),
        ).fetchall()
        return _next_sequenced_identifier(
            tuple(str(row["message_id"]) for row in rows),
            prefix=_MESSAGE_ID_PREFIX,
            field_name="message_id",
        )

    def _require_open_thread_conn(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        thread_id: str,
    ) -> ProjectThread:
        normalized_project_id = self._state_db._normalize_project_identifier(
            project_id,
            field_name="project_id",
        )
        normalized_thread_id = self._state_db._normalize_project_identifier(
            thread_id,
            field_name="thread_id",
        )
        row = self._state_db._get_project_thread_row(
            conn,
            normalized_project_id,
            normalized_thread_id,
        )
        thread = self._state_db._row_to_project_thread(row)
        if thread is None:
            raise ValueError(
                f"unknown_thread:{normalized_project_id}:{normalized_thread_id}"
            )
        if thread.status != "open":
            raise ValueError(
                "project_thread_closed:"
                f"{normalized_project_id}:{normalized_thread_id}"
            )
        return thread
