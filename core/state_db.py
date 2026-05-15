"""
core/state_db.py

SQLite-backed persistent state for chat tier selection, completed task
history, per-chat budget, the AI Office project model, and owner-agent
DM session/message state, queued agent-owner notifications, and durable
backend agent-bus threads/messages.

Design goals:
1. Single-file stdlib-only storage (`sqlite3`), safe for bot restarts.
2. WAL mode enabled so future read-heavy surfaces (web dashboard) can query
   while the bot keeps writing.
3. API mirrors the current in-memory stores closely, so migration can be
   incremental: TierSessionStore / TaskHistory / budget can swap internals
   without changing their public contracts.
4. Project persistence remains runtime-agnostic: no TelegramBridge,
   bot_runner, registry, or onboarding flow wiring lives here.

CONTRACTS:
1. StateDB(path) requires a Path; constructor creates parent directories and
   initializes/migrates the schema eagerly.
2. Current schema version is 11. Unknown future versions raise ValueError.
3. Every public method validates arguments via isinstance/ValueError.
4. Writes are serialized with a process-local lock; reads use independent
   SQLite connections and remain safe under concurrent access.
5. task_history keeps an append-only audit log. get_task(task_id) returns the
   newest record for that task_id; recent_tasks(n) returns newest-last order.
6. schema migration supports v1 -> v2 -> v3 -> v4 -> v5 -> v6 -> v7 -> v8
   -> v9 -> v10 -> v11:
   - v1 -> v2: tier_sessions.tier_name -> active_tier
   - v1 -> v2: task_history gains AUTOINCREMENT id for stable append-order
     semantics
   - v2 -> v3: adds projects, project_policies, project_members,
     project_chat_bindings
   - v3 -> v4: adds project_runtime_bindings
   - v4 -> v5: adds task_history.project_id for project-aware task identity
   - v5 -> v6: adds agent_dm_sessions for typed owner-agent DM session state
   - v6 -> v7: adds agent_dm_messages for typed owner-agent DM transcripts
   - v7 -> v8: adds agent_owner_notifications for queued personal DM
     notifications that can be delivered later into an active agent thread
   - v8 -> v9: adds project_threads and agent_bus_messages for durable
     backend agent-bus history
   - v9 -> v10: adds project_specialist_roster for persisted per-project
     optional specialist assignments
   - v10 -> v11: adds project_hire_requests for persisted pending owner
     approval workflow around sensitive logical hires
7. Identity model is explicit:
   - Project.owner_user_id is a positive Telegram owner user id stored in
     projects.owner_user_id.
   - ProjectMembership.member_id is a logical stable ASCII identifier, unique
     within (project_id, member_id), and is never treated as a chat id or
     Telegram user id.
   - ProjectChatBinding.chat_id is an external transport chat id and may be
     negative for Telegram supergroups; project/chat binding is one-to-one.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from core.adapter import ProjectCommand, ProjectRule
from core.agent_bus_models import AgentMessage, AgentMessageRef, ProjectThread
from core.agent_dm_models import (
    DEFAULT_AGENT_DM_MESSAGE_MAXLEN,
    AgentDmMessage,
    AgentDmSession,
)
from core.agent_owner_notifications import AgentOwnerNotification
from core.hire_approval import PendingHireRequest
from core.project_models import (
    VALID_CHAT_PROVIDERS,
    Project,
    ProjectChatBinding,
    ProjectMembership,
    ProjectPolicy,
)
from core.project_runtime import ProjectRuntimeBinding
from core.project_team_state import (
    ProjectSpecialistAssignment,
    ProjectSpecialistRoster,
)
from core.task_history import TaskSummary

_CURRENT_SCHEMA_VERSION = 11
_SQLITE_TIMEOUT_SECONDS = 30.0
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_T = TypeVar("_T")

_CREATE_SCHEMA_META = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_CREATE_TIER_SESSIONS = """
CREATE TABLE IF NOT EXISTS tier_sessions (
    chat_id INTEGER PRIMARY KEY,
    active_tier TEXT NOT NULL,
    last_changed_at REAL NOT NULL
)
"""

_CREATE_TASK_HISTORY = """
CREATE TABLE IF NOT EXISTS task_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    branch TEXT NOT NULL,
    commit_sha TEXT,
    final_state TEXT NOT NULL,
    failure_reason TEXT,
    tier_name TEXT NOT NULL,
    finished_at REAL NOT NULL,
    project_id TEXT
)
"""

_CREATE_BUDGET = """
CREATE TABLE IF NOT EXISTS budget (
    chat_id INTEGER PRIMARY KEY,
    usd REAL NOT NULL
)
"""

_CREATE_PROJECTS = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    owner_user_id INTEGER NOT NULL CHECK(owner_user_id > 0),
    status TEXT NOT NULL
)
"""

_CREATE_PROJECT_POLICIES = """
CREATE TABLE IF NOT EXISTS project_policies (
    project_id TEXT PRIMARY KEY,
    allow_hiring INTEGER NOT NULL CHECK(allow_hiring IN (0, 1)),
    allow_agent_dm INTEGER NOT NULL CHECK(allow_agent_dm IN (0, 1)),
    require_owner_approval_for_hires INTEGER NOT NULL
        CHECK(require_owner_approval_for_hires IN (0, 1))
)
"""

_CREATE_PROJECT_MEMBERS = """
CREATE TABLE IF NOT EXISTS project_members (
    project_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    member_type TEXT NOT NULL,
    role_name TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY(project_id, member_id)
)
"""

_CREATE_PROJECT_SPECIALIST_ROSTER = """
CREATE TABLE IF NOT EXISTS project_specialist_roster (
    project_id TEXT NOT NULL,
    specialist_role TEXT NOT NULL,
    PRIMARY KEY(project_id, specialist_role)
)
"""

_CREATE_PROJECT_HIRE_REQUESTS = """
CREATE TABLE IF NOT EXISTS project_hire_requests (
    request_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    specialist_role TEXT NOT NULL,
    reason TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    decided_at REAL NULL,
    decided_by_user_id INTEGER NULL
)
"""

_CREATE_PROJECT_CHAT_BINDINGS = """
CREATE TABLE IF NOT EXISTS project_chat_bindings (
    project_id TEXT PRIMARY KEY,
    chat_provider TEXT NOT NULL,
    chat_id INTEGER NOT NULL CHECK(chat_id != 0),
    UNIQUE(chat_provider, chat_id)
)
"""

_CREATE_PROJECT_RUNTIME_BINDINGS = """
CREATE TABLE IF NOT EXISTS project_runtime_bindings (
    project_id TEXT PRIMARY KEY,
    adapter_name TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    worktree_root TEXT,
    base_branch TEXT NOT NULL,
    branch_prefix TEXT NOT NULL,
    language TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    commands_json TEXT NOT NULL,
    forbidden_paths_json TEXT NOT NULL,
    forbidden_tokens_json TEXT NOT NULL
)
"""

_CREATE_AGENT_DM_SESSIONS = """
CREATE TABLE IF NOT EXISTS agent_dm_sessions (
    owner_user_id INTEGER NOT NULL,
    project_id TEXT NOT NULL,
    agent_role TEXT NOT NULL,
    thread_bot_role TEXT NOT NULL,
    dm_chat_id INTEGER NOT NULL,
    chat_provider TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_interaction_at REAL NOT NULL,
    PRIMARY KEY(owner_user_id, project_id, agent_role)
)
"""

_CREATE_AGENT_DM_MESSAGES = """
CREATE TABLE IF NOT EXISTS agent_dm_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    project_id TEXT NOT NULL,
    agent_role TEXT NOT NULL,
    sender_kind TEXT NOT NULL,
    sender_role TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at REAL NOT NULL
)
"""

_CREATE_AGENT_OWNER_NOTIFICATIONS = """
CREATE TABLE IF NOT EXISTS agent_owner_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    project_id TEXT NOT NULL,
    agent_role TEXT NOT NULL,
    thread_bot_role TEXT NOT NULL,
    body TEXT NOT NULL,
    chat_provider TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    delivered_at REAL NULL
)
"""

_CREATE_PROJECT_THREADS = """
CREATE TABLE IF NOT EXISTS project_threads (
    project_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    opened_by_role TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_message_at REAL NOT NULL,
    task_id TEXT NULL,
    PRIMARY KEY(project_id, thread_id)
)
"""

_CREATE_AGENT_BUS_MESSAGES = """
CREATE TABLE IF NOT EXISTS agent_bus_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    sender_role TEXT NOT NULL,
    recipient_role TEXT NOT NULL,
    message_kind TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at REAL NOT NULL,
    in_reply_to_project_id TEXT NULL,
    in_reply_to_thread_id TEXT NULL,
    in_reply_to_message_id TEXT NULL,
    UNIQUE(project_id, thread_id, message_id)
)
"""

_CREATE_INDEX_TASK_HISTORY_TASK_ID = """
CREATE INDEX IF NOT EXISTS idx_task_history_task_id_id
ON task_history(task_id, id DESC)
"""

_CREATE_INDEX_TASK_HISTORY_FINISHED = """
CREATE INDEX IF NOT EXISTS idx_task_history_finished_id
ON task_history(finished_at DESC, id DESC)
"""

_CREATE_INDEX_AGENT_DM_MESSAGES_TRANSCRIPT_ID = """
CREATE INDEX IF NOT EXISTS idx_agent_dm_messages_transcript_id_desc
ON agent_dm_messages(owner_user_id, project_id, agent_role, id DESC)
"""

_CREATE_INDEX_AGENT_OWNER_NOTIFICATIONS_QUEUE = """
CREATE INDEX IF NOT EXISTS idx_agent_owner_notifications_queue
ON agent_owner_notifications(
    owner_user_id,
    project_id,
    agent_role,
    thread_bot_role,
    status,
    id ASC
)
"""

_CREATE_INDEX_PROJECT_THREADS_PROJECT_ACTIVITY = """
CREATE INDEX IF NOT EXISTS idx_project_threads_project_activity
ON project_threads(project_id, last_message_at DESC, thread_id ASC)
"""

_CREATE_INDEX_AGENT_BUS_MESSAGES_THREAD_ID = """
CREATE INDEX IF NOT EXISTS idx_agent_bus_messages_thread_id_asc
ON agent_bus_messages(project_id, thread_id, id ASC)
"""

_CREATE_INDEX_AGENT_BUS_MESSAGES_INBOX_ID = """
CREATE INDEX IF NOT EXISTS idx_agent_bus_messages_inbox_id_asc
ON agent_bus_messages(project_id, recipient_role, id ASC)
"""

_CREATE_INDEX_PROJECT_HIRE_REQUESTS_PENDING = """
CREATE INDEX IF NOT EXISTS idx_project_hire_requests_pending_created
ON project_hire_requests(project_id, status, created_at ASC, request_id ASC)
"""

_CREATE_INDEX_PROJECT_HIRE_REQUESTS_PENDING_UNIQUE = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_hire_requests_pending_unique
ON project_hire_requests(project_id, specialist_role, source)
WHERE status = 'pending'
"""


class StateDB:
    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise ValueError(f"invalid_path_type:{type(path).__name__}")
        if path == Path(".") or not str(path).strip():
            raise ValueError("empty_path")
        self._path = path.expanduser()
        if self._path.exists() and self._path.is_dir():
            raise ValueError("path_is_directory")
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    @property
    def path(self) -> Path:
        return self._path

    def schema_version(self) -> int:
        with self._connect() as conn:
            return self._detect_schema_version(conn)

    # ------------------------------------------------------------------
    # Tier sessions
    # ------------------------------------------------------------------

    def get_tier(self, chat_id: int) -> str | None:
        self._validate_chat_id(chat_id)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT active_tier
                FROM tier_sessions
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
        if row is None:
            return None
        return str(row["active_tier"])

    def list_tiers(self) -> tuple[tuple[int, str, float], ...]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT chat_id, active_tier, last_changed_at
                FROM tier_sessions
                ORDER BY chat_id
                """
            ).fetchall()
        return tuple(
            (
                int(row["chat_id"]),
                str(row["active_tier"]),
                float(row["last_changed_at"]),
            )
            for row in rows
        )

    def set_tier(
        self,
        chat_id: int,
        tier_name: str,
        *,
        last_changed_at: float | None = None,
    ) -> None:
        self._validate_chat_id(chat_id)
        self._validate_non_empty_text(tier_name, "tier_name")
        changed_at = self._normalise_timestamp(
            last_changed_at if last_changed_at is not None else time.time(),
            field_name="last_changed_at",
            allow_zero=False,
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tier_sessions(chat_id, active_tier, last_changed_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    active_tier = excluded.active_tier,
                    last_changed_at = excluded.last_changed_at
                """,
                (chat_id, tier_name.strip(), changed_at),
            )

    def reset_tier(self, chat_id: int) -> None:
        self._validate_chat_id(chat_id)
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM tier_sessions WHERE chat_id = ?",
                (chat_id,),
            )

    # ------------------------------------------------------------------
    # Task history
    # ------------------------------------------------------------------

    def record_task(self, summary: TaskSummary) -> None:
        if not isinstance(summary, TaskSummary):
            raise ValueError(f"invalid_summary_type:{type(summary).__name__}")
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_history(
                    task_id,
                    branch,
                    commit_sha,
                    final_state,
                    failure_reason,
                    tier_name,
                    finished_at,
                    project_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary.task_id,
                    summary.branch,
                    summary.commit_sha,
                    summary.final_state,
                    summary.failure_reason,
                    summary.tier_name,
                    float(summary.finished_at),
                    summary.project_id,
                ),
            )

    def get_task(self, task_id: str) -> TaskSummary | None:
        self._validate_non_empty_text(task_id, "task_id")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT task_id, branch, commit_sha, final_state,
                       failure_reason, tier_name, finished_at, project_id
                FROM task_history
                WHERE task_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (task_id.strip(),),
            ).fetchone()
        return self._row_to_task_summary(row)

    def recent_tasks(self, n: int = 10) -> list[TaskSummary]:
        self._validate_positive_int(n, "n")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT task_id, branch, commit_sha, final_state,
                       failure_reason, tier_name, finished_at, project_id
                FROM task_history
                ORDER BY id DESC
                LIMIT ?
                """,
                (n,),
            ).fetchall()
        summaries = [
            self._row_to_task_summary(row)
            for row in reversed(rows)
        ]
        return [summary for summary in summaries if summary is not None]

    def trim_task_history(self, max_entries: int) -> None:
        self._validate_positive_int(max_entries, "max_entries")
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                DELETE FROM task_history
                WHERE id NOT IN (
                    SELECT id
                    FROM task_history
                    ORDER BY id DESC
                    LIMIT ?
                )
                """,
                (max_entries,),
            )

    # ------------------------------------------------------------------
    # Budget
    # ------------------------------------------------------------------

    def get_budget(self, chat_id: int) -> float | None:
        self._validate_chat_id(chat_id)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT usd
                FROM budget
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
        if row is None:
            return None
        return float(row["usd"])

    def set_budget(self, chat_id: int, usd: float) -> None:
        self._validate_chat_id(chat_id)
        self._validate_budget(usd)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO budget(chat_id, usd)
                VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    usd = excluded.usd
                """,
                (chat_id, float(usd)),
            )

    # ------------------------------------------------------------------
    # Project model
    # ------------------------------------------------------------------

    def upsert_project(self, project: Project) -> None:
        if not isinstance(project, Project):
            raise ValueError(f"invalid_project_type:{type(project).__name__}")
        self._run_write_transaction(
            lambda conn: self._upsert_project_conn(conn, project)
        )

    def get_project(self, project_id: str) -> Project | None:
        normalized_project_id = self._normalize_project_identifier(
            project_id,
            field_name="project_id",
        )
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT project_id, slug, name, description, owner_user_id, status
                FROM projects
                WHERE project_id = ?
                """,
                (normalized_project_id,),
            ).fetchone()
        return self._row_to_project(row)

    def get_project_by_slug(self, slug: str) -> Project | None:
        normalized_slug = self._normalize_project_slug(slug, field_name="slug")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT project_id, slug, name, description, owner_user_id, status
                FROM projects
                WHERE slug = ?
                """,
                (normalized_slug,),
            ).fetchone()
        return self._row_to_project(row)

    def list_projects(self) -> list[Project]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT project_id, slug, name, description, owner_user_id, status
                FROM projects
                ORDER BY project_id
                """
            ).fetchall()
        return [self._row_to_project(row) for row in rows if row is not None]

    def set_project_policy(self, policy: ProjectPolicy) -> None:
        if not isinstance(policy, ProjectPolicy):
            raise ValueError(
                f"invalid_project_policy_type:{type(policy).__name__}"
            )
        self._run_write_transaction(
            lambda conn: self._set_project_policy_conn(conn, policy)
        )

    def get_project_policy(self, project_id: str) -> ProjectPolicy | None:
        normalized_project_id = self._normalize_project_identifier(
            project_id,
            field_name="project_id",
        )
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT project_id, allow_hiring, allow_agent_dm,
                       require_owner_approval_for_hires
                FROM project_policies
                WHERE project_id = ?
                """,
                (normalized_project_id,),
            ).fetchone()
        return self._row_to_project_policy(row)

    def upsert_project_membership(
        self,
        membership: ProjectMembership,
    ) -> None:
        if not isinstance(membership, ProjectMembership):
            raise ValueError(
                f"invalid_project_membership_type:{type(membership).__name__}"
            )
        self._run_write_transaction(
            lambda conn: self._upsert_project_membership_conn(conn, membership)
        )

    def get_project_membership(
        self,
        project_id: str,
        member_id: str,
    ) -> ProjectMembership | None:
        normalized_project_id = self._normalize_project_identifier(
            project_id,
            field_name="project_id",
        )
        normalized_member_id = self._normalize_project_identifier(
            member_id,
            field_name="member_id",
        )
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT project_id, member_id, member_type, role_name, status
                FROM project_members
                WHERE project_id = ? AND member_id = ?
                """,
                (normalized_project_id, normalized_member_id),
            ).fetchone()
        return self._row_to_project_membership(row)

    def list_project_memberships(
        self,
        project_id: str,
    ) -> list[ProjectMembership]:
        normalized_project_id = self._normalize_project_identifier(
            project_id,
            field_name="project_id",
        )
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT project_id, member_id, member_type, role_name, status
                FROM project_members
                WHERE project_id = ?
                ORDER BY member_id
                """,
                (normalized_project_id,),
            ).fetchall()
        return [
            self._row_to_project_membership(row)
            for row in rows
            if row is not None
        ]

    def list_project_specialists(
        self,
        project_id: str,
    ) -> tuple[str, ...]:
        return self.get_project_specialist_roster(project_id).specialist_roles

    def add_project_specialist(
        self,
        project_id: str,
        specialist_role: str,
    ) -> None:
        assignment = ProjectSpecialistAssignment(
            project_id=project_id,
            specialist_role=specialist_role,
        )
        self._run_write_transaction(
            lambda conn: self._add_project_specialist_conn(conn, assignment)
        )

    def remove_project_specialist(
        self,
        project_id: str,
        specialist_role: str,
    ) -> None:
        assignment = ProjectSpecialistAssignment(
            project_id=project_id,
            specialist_role=specialist_role,
        )
        self._run_write_transaction(
            lambda conn: self._remove_project_specialist_conn(conn, assignment)
        )

    def get_project_specialist_roster(
        self,
        project_id: str,
    ) -> ProjectSpecialistRoster:
        normalized_project_id = self._normalize_project_identifier(
            project_id,
            field_name="project_id",
        )
        with self._connect() as conn:
            self._ensure_project_exists(conn, normalized_project_id)
            specialist_roles = self._list_project_specialist_roles_conn(
                conn,
                normalized_project_id,
            )
        return ProjectSpecialistRoster(
            project_id=normalized_project_id,
            specialist_roles=specialist_roles,
        )

    def create_hire_request(
        self,
        request: PendingHireRequest,
    ) -> PendingHireRequest:
        if not isinstance(request, PendingHireRequest):
            raise ValueError(
                "invalid_pending_hire_request_type:"
                f"{type(request).__name__}"
            )
        if request.status != "pending":
            raise ValueError("hire_request_must_start_pending")
        return self._run_write_transaction(
            lambda conn: self._create_hire_request_conn(conn, request)
        )

    def get_hire_request(
        self,
        request_id: str,
    ) -> PendingHireRequest | None:
        normalized_request_id = self._normalize_hire_request_id(request_id)
        with self._connect() as conn:
            row = self._get_hire_request_row(conn, normalized_request_id)
        return self._row_to_pending_hire_request(row)

    def list_pending_hire_requests(
        self,
        project_id: str,
    ) -> tuple[PendingHireRequest, ...]:
        normalized_project_id = self._normalize_project_identifier(
            project_id,
            field_name="project_id",
        )
        with self._connect() as conn:
            self._ensure_project_exists(conn, normalized_project_id)
            rows = conn.execute(
                """
                SELECT request_id, project_id, specialist_role, reason, source,
                       status, created_at, decided_at, decided_by_user_id
                FROM project_hire_requests
                WHERE project_id = ? AND status = 'pending'
                ORDER BY created_at ASC, request_id ASC
                """,
                (normalized_project_id,),
            ).fetchall()
        requests: list[PendingHireRequest] = []
        for row in rows:
            request = self._row_to_pending_hire_request(row)
            if request is None:
                continue
            requests.append(request)
        return tuple(requests)

    def mark_hire_request_approved(
        self,
        request_id: str,
        actor_user_id: int,
    ) -> PendingHireRequest:
        normalized_request_id = self._normalize_hire_request_id(request_id)
        self._validate_positive_int(actor_user_id, "hire_approval_actor_user_id")
        return self._run_write_transaction(
            lambda conn: self._mark_hire_request_approved_conn(
                conn,
                normalized_request_id,
                actor_user_id=actor_user_id,
            )
        )

    def mark_hire_request_rejected(
        self,
        request_id: str,
        actor_user_id: int,
    ) -> PendingHireRequest:
        normalized_request_id = self._normalize_hire_request_id(request_id)
        self._validate_positive_int(actor_user_id, "hire_approval_actor_user_id")
        return self._run_write_transaction(
            lambda conn: self._mark_hire_request_rejected_conn(
                conn,
                normalized_request_id,
                actor_user_id=actor_user_id,
            )
        )

    def bind_project_chat(self, binding: ProjectChatBinding) -> None:
        if not isinstance(binding, ProjectChatBinding):
            raise ValueError(
                f"invalid_project_chat_binding_type:{type(binding).__name__}"
            )
        self._run_write_transaction(
            lambda conn: self._bind_project_chat_conn(conn, binding)
        )

    def get_project_chat_binding(
        self,
        project_id: str,
    ) -> ProjectChatBinding | None:
        normalized_project_id = self._normalize_project_identifier(
            project_id,
            field_name="project_id",
        )
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT project_id, chat_provider, chat_id
                FROM project_chat_bindings
                WHERE project_id = ?
                """,
                (normalized_project_id,),
            ).fetchone()
        return self._row_to_project_chat_binding(row)

    def get_project_for_chat(
        self,
        chat_provider: str,
        chat_id: int,
    ) -> ProjectChatBinding | None:
        normalized_chat_provider = self._normalize_chat_provider(chat_provider)
        self._validate_transport_chat_id(chat_id)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT project_id, chat_provider, chat_id
                FROM project_chat_bindings
                WHERE chat_provider = ? AND chat_id = ?
                """,
                (normalized_chat_provider, chat_id),
            ).fetchone()
        return self._row_to_project_chat_binding(row)

    def upsert_project_runtime_binding(
        self,
        binding: ProjectRuntimeBinding,
    ) -> None:
        if not isinstance(binding, ProjectRuntimeBinding):
            raise ValueError(
                "invalid_project_runtime_binding_type:"
                f"{type(binding).__name__}"
            )
        self._run_write_transaction(
            lambda conn: self._upsert_project_runtime_binding_conn(conn, binding)
        )

    def get_project_runtime_binding(
        self,
        project_id: str,
    ) -> ProjectRuntimeBinding | None:
        normalized_project_id = self._normalize_project_identifier(
            project_id,
            field_name="project_id",
        )
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT project_id, adapter_name, repo_path, worktree_root,
                       base_branch, branch_prefix, language, rules_json,
                       commands_json, forbidden_paths_json, forbidden_tokens_json
                FROM project_runtime_bindings
                WHERE project_id = ?
                """,
                (normalized_project_id,),
            ).fetchone()
        return self._row_to_project_runtime_binding(row)

    # ------------------------------------------------------------------
    # Agent bus threads/messages
    # ------------------------------------------------------------------

    def upsert_project_thread(self, thread: ProjectThread) -> None:
        if not isinstance(thread, ProjectThread):
            raise ValueError(
                "invalid_project_thread_type:"
                f"{type(thread).__name__}"
            )
        self._run_write_transaction(
            lambda conn: self._upsert_project_thread_conn(conn, thread)
        )

    def get_project_thread(
        self,
        project_id: str,
        thread_id: str,
    ) -> ProjectThread | None:
        normalized_project_id = self._normalize_project_identifier(
            project_id,
            field_name="project_id",
        )
        normalized_thread_id = self._normalize_project_identifier(
            thread_id,
            field_name="thread_id",
        )
        with self._connect() as conn:
            row = self._get_project_thread_row(
                conn,
                normalized_project_id,
                normalized_thread_id,
            )
        return self._row_to_project_thread(row)

    def get_project_thread_by_task(
        self,
        project_id: str,
        task_id: str,
    ) -> ProjectThread | None:
        normalized_project_id = self._normalize_project_identifier(
            project_id,
            field_name="project_id",
        )
        normalized_task_id = self._normalize_task_identifier(
            task_id,
            field_name="task_id",
        )
        with self._connect() as conn:
            row = self._get_project_thread_by_task_row(
                conn,
                normalized_project_id,
                normalized_task_id,
            )
        return self._row_to_project_thread(row)

    def list_project_threads(
        self,
        project_id: str,
    ) -> tuple[ProjectThread, ...]:
        normalized_project_id = self._normalize_project_identifier(
            project_id,
            field_name="project_id",
        )
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT project_id, thread_id, opened_by_role, status, created_at,
                       last_message_at, task_id
                FROM project_threads
                WHERE project_id = ?
                ORDER BY last_message_at DESC, thread_id ASC
                """,
                (normalized_project_id,),
            ).fetchall()
        return tuple(
            thread
            for row in rows
            if (thread := self._row_to_project_thread(row)) is not None
        )

    def insert_agent_bus_message(
        self,
        message: AgentMessage,
    ) -> None:
        if not isinstance(message, AgentMessage):
            raise ValueError(
                "invalid_agent_bus_message_type:"
                f"{type(message).__name__}"
            )
        self._run_write_transaction(
            lambda conn: self._insert_agent_bus_message_conn(conn, message)
        )

    def get_agent_bus_message(
        self,
        project_id: str,
        thread_id: str,
        message_id: str,
    ) -> AgentMessage | None:
        normalized_project_id = self._normalize_project_identifier(
            project_id,
            field_name="project_id",
        )
        normalized_thread_id = self._normalize_project_identifier(
            thread_id,
            field_name="thread_id",
        )
        normalized_message_id = self._normalize_project_identifier(
            message_id,
            field_name="message_id",
        )
        with self._connect() as conn:
            row = self._get_agent_bus_message_row(
                conn,
                normalized_project_id,
                normalized_thread_id,
                normalized_message_id,
            )
        return self._row_to_agent_bus_message(row)

    def list_agent_bus_messages(
        self,
        project_id: str,
        thread_id: str,
    ) -> tuple[AgentMessage, ...]:
        normalized_project_id = self._normalize_project_identifier(
            project_id,
            field_name="project_id",
        )
        normalized_thread_id = self._normalize_project_identifier(
            thread_id,
            field_name="thread_id",
        )
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT project_id, thread_id, message_id, sender_role,
                       recipient_role, message_kind, body, created_at,
                       in_reply_to_project_id, in_reply_to_thread_id,
                       in_reply_to_message_id
                FROM agent_bus_messages
                WHERE project_id = ? AND thread_id = ?
                ORDER BY id ASC
                """,
                (
                    normalized_project_id,
                    normalized_thread_id,
                ),
            ).fetchall()
        return tuple(
            message
            for row in rows
            if (message := self._row_to_agent_bus_message(row)) is not None
        )

    def list_agent_bus_inbox(
        self,
        project_id: str,
        recipient_role: str,
    ) -> tuple[AgentMessage, ...]:
        normalized_project_id = self._normalize_project_identifier(
            project_id,
            field_name="project_id",
        )
        normalized_recipient_role = self._normalize_project_identifier(
            recipient_role,
            field_name="recipient_role",
        )
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT project_id, thread_id, message_id, sender_role,
                       recipient_role, message_kind, body, created_at,
                       in_reply_to_project_id, in_reply_to_thread_id,
                       in_reply_to_message_id
                FROM agent_bus_messages
                WHERE project_id = ? AND recipient_role = ?
                ORDER BY created_at ASC, id ASC
                """,
                (
                    normalized_project_id,
                    normalized_recipient_role,
                ),
            ).fetchall()
        return tuple(
            message
            for row in rows
            if (message := self._row_to_agent_bus_message(row)) is not None
        )

    # ------------------------------------------------------------------
    # Agent DM sessions
    # ------------------------------------------------------------------

    def upsert_agent_dm_session(self, session: AgentDmSession) -> None:
        if not isinstance(session, AgentDmSession):
            raise ValueError(
                "invalid_agent_dm_session_type:"
                f"{type(session).__name__}"
            )
        self._run_write_transaction(
            lambda conn: self._upsert_agent_dm_session_conn(conn, session)
        )

    def get_agent_dm_session(
        self,
        owner_user_id: int,
        project_id: str,
        agent_role: str,
    ) -> AgentDmSession | None:
        self._validate_positive_int(owner_user_id, "owner_user_id")
        normalized_project_id = self._normalize_project_identifier(
            project_id,
            field_name="project_id",
        )
        normalized_agent_role = self._normalize_project_identifier(
            agent_role,
            field_name="agent_role",
        )
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT owner_user_id, project_id, agent_role, thread_bot_role,
                       dm_chat_id, chat_provider, status, created_at,
                       last_interaction_at
                FROM agent_dm_sessions
                WHERE owner_user_id = ? AND project_id = ? AND agent_role = ?
                """,
                (
                    owner_user_id,
                    normalized_project_id,
                    normalized_agent_role,
                ),
            ).fetchone()
        return self._row_to_agent_dm_session(row)

    def list_agent_dm_sessions_for_owner(
        self,
        owner_user_id: int,
    ) -> tuple[AgentDmSession, ...]:
        self._validate_positive_int(owner_user_id, "owner_user_id")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT owner_user_id, project_id, agent_role, thread_bot_role,
                       dm_chat_id, chat_provider, status, created_at,
                       last_interaction_at
                FROM agent_dm_sessions
                WHERE owner_user_id = ?
                ORDER BY project_id ASC, agent_role ASC
                """,
                (owner_user_id,),
            ).fetchall()
        return tuple(
            session
            for row in rows
            if (session := self._row_to_agent_dm_session(row)) is not None
        )

    # ------------------------------------------------------------------
    # Agent DM messages
    # ------------------------------------------------------------------

    def insert_agent_owner_notification(
        self,
        notification: AgentOwnerNotification,
    ) -> AgentOwnerNotification:
        if not isinstance(notification, AgentOwnerNotification):
            raise ValueError(
                "invalid_agent_owner_notification_type:"
                f"{type(notification).__name__}"
            )
        return self._run_write_transaction(
            lambda conn: self._insert_agent_owner_notification_conn(
                conn,
                notification,
            )
        )

    def list_queued_agent_owner_notifications(
        self,
        owner_user_id: int,
        project_id: str,
        agent_role: str,
        thread_bot_role: str,
    ) -> tuple[AgentOwnerNotification, ...]:
        self._validate_positive_int(owner_user_id, "owner_user_id")
        normalized_project_id = self._normalize_project_identifier(
            project_id,
            field_name="project_id",
        )
        normalized_agent_role = self._normalize_project_identifier(
            agent_role,
            field_name="agent_role",
        )
        normalized_thread_bot_role = self._normalize_project_identifier(
            thread_bot_role,
            field_name="thread_bot_role",
        )
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, owner_user_id, project_id, agent_role, thread_bot_role,
                       body, chat_provider, status, created_at, delivered_at
                FROM agent_owner_notifications
                WHERE owner_user_id = ?
                  AND project_id = ?
                  AND agent_role = ?
                  AND thread_bot_role = ?
                  AND status = 'queued'
                ORDER BY id ASC
                """,
                (
                    owner_user_id,
                    normalized_project_id,
                    normalized_agent_role,
                    normalized_thread_bot_role,
                ),
            ).fetchall()
        return tuple(
            notification
            for row in rows
            if (
                notification := self._row_to_agent_owner_notification(row)
            )
            is not None
        )

    def mark_agent_owner_notification_delivered(
        self,
        notification_id: int,
        *,
        delivered_at: float,
    ) -> AgentOwnerNotification:
        self._validate_positive_int(notification_id, "notification_id")
        normalized_delivered_at = self._normalise_timestamp(
            delivered_at,
            field_name="delivered_at",
            allow_zero=False,
        )
        return self._run_write_transaction(
            lambda conn: self._mark_agent_owner_notification_delivered_conn(
                conn,
                notification_id,
                delivered_at=normalized_delivered_at,
            )
        )

    def record_agent_dm_message(
        self,
        message: AgentDmMessage,
        *,
        max_entries: int = DEFAULT_AGENT_DM_MESSAGE_MAXLEN,
    ) -> None:
        if not isinstance(message, AgentDmMessage):
            raise ValueError(
                "invalid_agent_dm_message_type:"
                f"{type(message).__name__}"
            )
        self._validate_positive_int(max_entries, "max_entries")
        self._run_write_transaction(
            lambda conn: self._record_agent_dm_message_conn(
                conn,
                message,
                max_entries=max_entries,
            )
        )

    def list_agent_dm_messages(
        self,
        owner_user_id: int,
        project_id: str,
        agent_role: str,
        *,
        limit: int = DEFAULT_AGENT_DM_MESSAGE_MAXLEN,
    ) -> tuple[AgentDmMessage, ...]:
        self._validate_positive_int(owner_user_id, "owner_user_id")
        normalized_project_id = self._normalize_project_identifier(
            project_id,
            field_name="project_id",
        )
        normalized_agent_role = self._normalize_project_identifier(
            agent_role,
            field_name="agent_role",
        )
        self._validate_positive_int(limit, "limit")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, owner_user_id, project_id, agent_role, sender_kind,
                       sender_role, body, created_at
                FROM (
                    SELECT id, owner_user_id, project_id, agent_role, sender_kind,
                           sender_role, body, created_at
                    FROM agent_dm_messages
                    WHERE owner_user_id = ? AND project_id = ? AND agent_role = ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id ASC
                """,
                (
                    owner_user_id,
                    normalized_project_id,
                    normalized_agent_role,
                    limit,
                ),
            ).fetchall()
        return tuple(
            message
            for row in rows
            if (message := self._row_to_agent_dm_message(row)) is not None
        )

    def trim_agent_dm_messages(
        self,
        owner_user_id: int,
        project_id: str,
        agent_role: str,
        max_entries: int,
    ) -> None:
        self._validate_positive_int(owner_user_id, "owner_user_id")
        normalized_project_id = self._normalize_project_identifier(
            project_id,
            field_name="project_id",
        )
        normalized_agent_role = self._normalize_project_identifier(
            agent_role,
            field_name="agent_role",
        )
        self._validate_positive_int(max_entries, "max_entries")
        self._run_write_transaction(
            lambda conn: self._trim_agent_dm_messages_conn(
                conn,
                owner_user_id,
                normalized_project_id,
                normalized_agent_role,
                max_entries,
            )
        )

    # ------------------------------------------------------------------
    # Schema management
    # ------------------------------------------------------------------

    def _initialize_schema(self) -> None:
        with self._lock, self._connect() as conn:
            version = self._detect_schema_version(conn)
            if version == 0:
                self._create_v11_schema(conn)
                return
            if version > _CURRENT_SCHEMA_VERSION:
                raise ValueError(
                    f"unsupported_schema_version:{version}"
                )
            while version < _CURRENT_SCHEMA_VERSION:
                if version == 1:
                    self._migrate_v1_to_v2(conn)
                    version = 2
                    continue
                if version == 2:
                    self._migrate_v2_to_v3(conn)
                    version = 3
                    continue
                if version == 3:
                    self._migrate_v3_to_v4(conn)
                    version = 4
                    continue
                if version == 4:
                    self._migrate_v4_to_v5(conn)
                    version = 5
                    continue
                if version == 5:
                    self._migrate_v5_to_v6(conn)
                    version = 6
                    continue
                if version == 6:
                    self._migrate_v6_to_v7(conn)
                    version = 7
                    continue
                if version == 7:
                    self._migrate_v7_to_v8(conn)
                    version = 8
                    continue
                if version == 8:
                    self._migrate_v8_to_v9(conn)
                    version = 9
                    continue
                if version == 9:
                    self._migrate_v9_to_v10(conn)
                    version = 10
                    continue
                if version == 10:
                    self._migrate_v10_to_v11(conn)
                    version = 11
                    continue
                raise ValueError(f"unsupported_schema_version:{version}")

    def _create_v11_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(_CREATE_SCHEMA_META)
        conn.execute(_CREATE_TIER_SESSIONS)
        conn.execute(_CREATE_TASK_HISTORY)
        conn.execute(_CREATE_BUDGET)
        conn.execute(_CREATE_PROJECTS)
        conn.execute(_CREATE_PROJECT_POLICIES)
        conn.execute(_CREATE_PROJECT_MEMBERS)
        conn.execute(_CREATE_PROJECT_SPECIALIST_ROSTER)
        conn.execute(_CREATE_PROJECT_HIRE_REQUESTS)
        conn.execute(_CREATE_PROJECT_CHAT_BINDINGS)
        conn.execute(_CREATE_PROJECT_RUNTIME_BINDINGS)
        conn.execute(_CREATE_AGENT_DM_SESSIONS)
        conn.execute(_CREATE_AGENT_DM_MESSAGES)
        conn.execute(_CREATE_AGENT_OWNER_NOTIFICATIONS)
        conn.execute(_CREATE_PROJECT_THREADS)
        conn.execute(_CREATE_AGENT_BUS_MESSAGES)
        conn.execute(_CREATE_INDEX_TASK_HISTORY_TASK_ID)
        conn.execute(_CREATE_INDEX_TASK_HISTORY_FINISHED)
        conn.execute(_CREATE_INDEX_AGENT_DM_MESSAGES_TRANSCRIPT_ID)
        conn.execute(_CREATE_INDEX_AGENT_OWNER_NOTIFICATIONS_QUEUE)
        conn.execute(_CREATE_INDEX_PROJECT_THREADS_PROJECT_ACTIVITY)
        conn.execute(_CREATE_INDEX_AGENT_BUS_MESSAGES_THREAD_ID)
        conn.execute(_CREATE_INDEX_AGENT_BUS_MESSAGES_INBOX_ID)
        conn.execute(_CREATE_INDEX_PROJECT_HIRE_REQUESTS_PENDING)
        conn.execute(_CREATE_INDEX_PROJECT_HIRE_REQUESTS_PENDING_UNIQUE)
        self._set_schema_version(conn, _CURRENT_SCHEMA_VERSION)

    def _migrate_v1_to_v2(self, conn: sqlite3.Connection) -> None:
        self._migrate_tier_sessions_to_v2(conn)
        self._migrate_task_history_to_v2(conn)
        conn.execute(_CREATE_BUDGET)
        conn.execute(_CREATE_SCHEMA_META)
        conn.execute(_CREATE_INDEX_TASK_HISTORY_TASK_ID)
        conn.execute(_CREATE_INDEX_TASK_HISTORY_FINISHED)
        self._set_schema_version(conn, 2)

    def _migrate_v2_to_v3(self, conn: sqlite3.Connection) -> None:
        conn.execute(_CREATE_PROJECTS)
        conn.execute(_CREATE_PROJECT_POLICIES)
        conn.execute(_CREATE_PROJECT_MEMBERS)
        conn.execute(_CREATE_PROJECT_CHAT_BINDINGS)
        self._set_schema_version(conn, 3)

    def _migrate_v3_to_v4(self, conn: sqlite3.Connection) -> None:
        conn.execute(_CREATE_PROJECT_RUNTIME_BINDINGS)
        self._set_schema_version(conn, 4)

    def _migrate_v4_to_v5(self, conn: sqlite3.Connection) -> None:
        columns = self._table_columns(conn, "task_history")
        if "project_id" not in columns:
            conn.execute("ALTER TABLE task_history ADD COLUMN project_id TEXT")
        self._set_schema_version(conn, 5)

    def _migrate_v5_to_v6(self, conn: sqlite3.Connection) -> None:
        conn.execute(_CREATE_AGENT_DM_SESSIONS)
        self._set_schema_version(conn, 6)

    def _migrate_v6_to_v7(self, conn: sqlite3.Connection) -> None:
        conn.execute(_CREATE_AGENT_DM_MESSAGES)
        conn.execute(_CREATE_INDEX_AGENT_DM_MESSAGES_TRANSCRIPT_ID)
        self._set_schema_version(conn, 7)

    def _migrate_v7_to_v8(self, conn: sqlite3.Connection) -> None:
        conn.execute(_CREATE_AGENT_OWNER_NOTIFICATIONS)
        conn.execute(_CREATE_INDEX_AGENT_OWNER_NOTIFICATIONS_QUEUE)
        self._set_schema_version(conn, 8)

    def _migrate_v8_to_v9(self, conn: sqlite3.Connection) -> None:
        conn.execute(_CREATE_PROJECT_THREADS)
        conn.execute(_CREATE_AGENT_BUS_MESSAGES)
        conn.execute(_CREATE_INDEX_PROJECT_THREADS_PROJECT_ACTIVITY)
        conn.execute(_CREATE_INDEX_AGENT_BUS_MESSAGES_THREAD_ID)
        conn.execute(_CREATE_INDEX_AGENT_BUS_MESSAGES_INBOX_ID)
        self._set_schema_version(conn, 9)

    def _migrate_v9_to_v10(self, conn: sqlite3.Connection) -> None:
        conn.execute(_CREATE_PROJECT_SPECIALIST_ROSTER)
        self._set_schema_version(conn, 10)

    def _migrate_v10_to_v11(self, conn: sqlite3.Connection) -> None:
        conn.execute(_CREATE_PROJECT_HIRE_REQUESTS)
        conn.execute(_CREATE_INDEX_PROJECT_HIRE_REQUESTS_PENDING)
        conn.execute(_CREATE_INDEX_PROJECT_HIRE_REQUESTS_PENDING_UNIQUE)
        self._set_schema_version(conn, 11)

    def _migrate_tier_sessions_to_v2(self, conn: sqlite3.Connection) -> None:
        columns = self._table_columns(conn, "tier_sessions")
        if columns == ("chat_id", "active_tier", "last_changed_at"):
            conn.execute(_CREATE_TIER_SESSIONS)
            return
        if "chat_id" not in columns or "last_changed_at" not in columns:
            raise ValueError("invalid_tier_sessions_v1_schema")
        tier_column = "active_tier" if "active_tier" in columns else "tier_name"
        if tier_column not in columns:
            raise ValueError("missing_tier_column_in_v1")
        conn.execute("ALTER TABLE tier_sessions RENAME TO tier_sessions_v1")
        conn.execute(_CREATE_TIER_SESSIONS)
        conn.execute(
            f"""
            INSERT INTO tier_sessions(chat_id, active_tier, last_changed_at)
            SELECT chat_id, {tier_column}, last_changed_at
            FROM tier_sessions_v1
            """
        )
        conn.execute("DROP TABLE tier_sessions_v1")

    def _migrate_task_history_to_v2(self, conn: sqlite3.Connection) -> None:
        columns = self._table_columns(conn, "task_history")
        desired = (
            "id",
            "task_id",
            "branch",
            "commit_sha",
            "final_state",
            "failure_reason",
            "tier_name",
            "finished_at",
            "project_id",
        )
        if columns == desired:
            conn.execute(_CREATE_TASK_HISTORY)
            return
        if columns == desired[:-1]:
            conn.execute(_CREATE_TASK_HISTORY)
            return
        required_v1 = (
            "task_id",
            "branch",
            "commit_sha",
            "final_state",
            "failure_reason",
            "tier_name",
            "finished_at",
        )
        if any(column not in columns for column in required_v1):
            raise ValueError("invalid_task_history_v1_schema")
        conn.execute("ALTER TABLE task_history RENAME TO task_history_v1")
        conn.execute(_CREATE_TASK_HISTORY)
        conn.execute(
            """
            INSERT INTO task_history(
                task_id,
                branch,
                commit_sha,
                final_state,
                failure_reason,
                tier_name,
                finished_at
            )
            SELECT task_id, branch, commit_sha, final_state,
                   failure_reason, tier_name, finished_at
            FROM task_history_v1
            ORDER BY rowid
            """
        )
        conn.execute("DROP TABLE task_history_v1")

    def _detect_schema_version(self, conn: sqlite3.Connection) -> int:
        if self._table_exists(conn, "schema_meta"):
            row = conn.execute(
                """
                SELECT value
                FROM schema_meta
                WHERE key = 'schema_version'
                """
            ).fetchone()
            if row is None:
                raise ValueError("missing_schema_version")
            try:
                return int(row["value"])
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid_schema_version") from exc

        legacy_tables = tuple(
            table_name
            for table_name in ("tier_sessions", "task_history", "budget")
            if self._table_exists(conn, table_name)
        )
        if not legacy_tables:
            return 0
        if set(legacy_tables) == {"tier_sessions", "task_history", "budget"}:
            return 1
        raise ValueError("partial_legacy_schema")

    def _set_schema_version(self, conn: sqlite3.Connection, version: int) -> None:
        conn.execute(_CREATE_SCHEMA_META)
        conn.execute(
            """
            INSERT INTO schema_meta(key, value)
            VALUES ('schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value
            """,
            (str(version),),
        )

    def _table_exists(self, conn: sqlite3.Connection, table_name: str) -> bool:
        row = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table_name,),
        ).fetchone()
        return row is not None

    def _table_columns(
        self,
        conn: sqlite3.Connection,
        table_name: str,
    ) -> tuple[str, ...]:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return tuple(str(row["name"]) for row in rows)

    def _run_write_transaction(
        self,
        operation: Callable[[sqlite3.Connection], _T],
    ) -> _T:
        if not callable(operation):
            raise ValueError("operation_not_callable")
        with self._lock, self._connect() as conn:
            return operation(conn)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._path,
            timeout=_SQLITE_TIMEOUT_SECONDS,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @staticmethod
    def _row_to_task_summary(row: sqlite3.Row | None) -> TaskSummary | None:
        if row is None:
            return None
        try:
            project_id = row["project_id"]
        except IndexError:
            project_id = None
        return TaskSummary(
            task_id=str(row["task_id"]),
            branch=str(row["branch"]),
            commit_sha=row["commit_sha"],
            final_state=str(row["final_state"]),
            failure_reason=row["failure_reason"],
            tier_name=str(row["tier_name"]),
            finished_at=float(row["finished_at"]),
            project_id=str(project_id) if project_id is not None else None,
        )

    @staticmethod
    def _row_to_project(row: sqlite3.Row | None) -> Project | None:
        if row is None:
            return None
        return Project(
            project_id=str(row["project_id"]),
            slug=str(row["slug"]),
            name=str(row["name"]),
            description=str(row["description"]),
            owner_user_id=int(row["owner_user_id"]),
            status=str(row["status"]),
        )

    @staticmethod
    def _row_to_project_policy(row: sqlite3.Row | None) -> ProjectPolicy | None:
        if row is None:
            return None
        return ProjectPolicy(
            project_id=str(row["project_id"]),
            allow_hiring=bool(row["allow_hiring"]),
            allow_agent_dm=bool(row["allow_agent_dm"]),
            require_owner_approval_for_hires=bool(
                row["require_owner_approval_for_hires"]
            ),
        )

    @staticmethod
    def _row_to_pending_hire_request(
        row: sqlite3.Row | None,
    ) -> PendingHireRequest | None:
        if row is None:
            return None
        return PendingHireRequest(
            request_id=str(row["request_id"]),
            project_id=str(row["project_id"]),
            specialist_role=str(row["specialist_role"]),
            reason=str(row["reason"]),
            source=str(row["source"]),
            status=str(row["status"]),
            created_at=float(row["created_at"]),
            decided_at=(
                None if row["decided_at"] is None else float(row["decided_at"])
            ),
            decided_by_user_id=(
                None
                if row["decided_by_user_id"] is None
                else int(row["decided_by_user_id"])
            ),
        )

    @staticmethod
    def _row_to_project_membership(
        row: sqlite3.Row | None,
    ) -> ProjectMembership | None:
        if row is None:
            return None
        return ProjectMembership(
            project_id=str(row["project_id"]),
            member_id=str(row["member_id"]),
            member_type=str(row["member_type"]),
            role_name=str(row["role_name"]),
            status=str(row["status"]),
        )

    @staticmethod
    def _row_to_project_chat_binding(
        row: sqlite3.Row | None,
    ) -> ProjectChatBinding | None:
        if row is None:
            return None
        return ProjectChatBinding(
            project_id=str(row["project_id"]),
            chat_provider=str(row["chat_provider"]),
            chat_id=int(row["chat_id"]),
        )

    @staticmethod
    def _row_to_project_runtime_binding(
        row: sqlite3.Row | None,
    ) -> ProjectRuntimeBinding | None:
        if row is None:
            return None
        try:
            rules_data = json.loads(str(row["rules_json"]))
            commands_data = json.loads(str(row["commands_json"]))
            forbidden_paths_data = json.loads(str(row["forbidden_paths_json"]))
            forbidden_tokens_data = json.loads(str(row["forbidden_tokens_json"]))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid_project_runtime_binding_json") from exc

        return ProjectRuntimeBinding(
            project_id=str(row["project_id"]),
            adapter_name=str(row["adapter_name"]),
            repo_path=Path(str(row["repo_path"])),
            worktree_root=(
                Path(str(row["worktree_root"]))
                if row["worktree_root"] is not None
                else None
            ),
            base_branch=str(row["base_branch"]),
            branch_prefix=str(row["branch_prefix"]),
            language=str(row["language"]),
            rules=tuple(
                ProjectRule(
                    name=str(item["name"]),
                    description=str(item["description"]),
                    severity=str(item.get("severity", "error")),
                )
                for item in rules_data
            ),
            commands=tuple(
                ProjectCommand(
                    name=str(item["name"]),
                    cmd=tuple(str(token) for token in item["cmd"]),
                    timeout_seconds=int(item.get("timeout_seconds", 120)),
                )
                for item in commands_data
            ),
            forbidden_paths=tuple(str(item) for item in forbidden_paths_data),
            forbidden_tokens=tuple(str(item) for item in forbidden_tokens_data),
        )

    @staticmethod
    def _row_to_agent_dm_session(
        row: sqlite3.Row | None,
    ) -> AgentDmSession | None:
        if row is None:
            return None
        return AgentDmSession(
            owner_user_id=int(row["owner_user_id"]),
            project_id=str(row["project_id"]),
            agent_role=str(row["agent_role"]),
            thread_bot_role=str(row["thread_bot_role"]),
            dm_chat_id=int(row["dm_chat_id"]),
            chat_provider=str(row["chat_provider"]),
            status=str(row["status"]),
            created_at=float(row["created_at"]),
            last_interaction_at=float(row["last_interaction_at"]),
        )

    @staticmethod
    def _row_to_agent_dm_message(
        row: sqlite3.Row | None,
    ) -> AgentDmMessage | None:
        if row is None:
            return None
        return AgentDmMessage(
            owner_user_id=int(row["owner_user_id"]),
            project_id=str(row["project_id"]),
            agent_role=str(row["agent_role"]),
            sender_kind=str(row["sender_kind"]),
            sender_role=str(row["sender_role"]),
            body=str(row["body"]),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _row_to_agent_owner_notification(
        row: sqlite3.Row | None,
    ) -> AgentOwnerNotification | None:
        if row is None:
            return None
        return AgentOwnerNotification(
            notification_id=int(row["id"]),
            owner_user_id=int(row["owner_user_id"]),
            project_id=str(row["project_id"]),
            agent_role=str(row["agent_role"]),
            thread_bot_role=str(row["thread_bot_role"]),
            body=str(row["body"]),
            chat_provider=str(row["chat_provider"]),
            status=str(row["status"]),
            created_at=float(row["created_at"]),
            delivered_at=(
                None
                if row["delivered_at"] is None
                else float(row["delivered_at"])
            ),
        )

    @staticmethod
    def _row_to_project_thread(
        row: sqlite3.Row | None,
    ) -> ProjectThread | None:
        if row is None:
            return None
        return ProjectThread(
            project_id=str(row["project_id"]),
            thread_id=str(row["thread_id"]),
            opened_by_role=str(row["opened_by_role"]),
            status=str(row["status"]),
            created_at=float(row["created_at"]),
            last_message_at=float(row["last_message_at"]),
            task_id=(
                None
                if row["task_id"] is None
                else str(row["task_id"])
            ),
        )

    @staticmethod
    def _row_to_agent_bus_message(
        row: sqlite3.Row | None,
    ) -> AgentMessage | None:
        if row is None:
            return None
        in_reply_to_project_id = row["in_reply_to_project_id"]
        in_reply_to_thread_id = row["in_reply_to_thread_id"]
        in_reply_to_message_id = row["in_reply_to_message_id"]
        if (
            (in_reply_to_project_id is None)
            != (in_reply_to_thread_id is None)
            or (in_reply_to_project_id is None)
            != (in_reply_to_message_id is None)
        ):
            raise ValueError("invalid_agent_bus_message_reference_columns")
        in_reply_to = None
        if in_reply_to_project_id is not None:
            in_reply_to = AgentMessageRef(
                project_id=str(in_reply_to_project_id),
                thread_id=str(in_reply_to_thread_id),
                message_id=str(in_reply_to_message_id),
            )
        return AgentMessage(
            project_id=str(row["project_id"]),
            thread_id=str(row["thread_id"]),
            message_id=str(row["message_id"]),
            sender_role=str(row["sender_role"]),
            recipient_role=str(row["recipient_role"]),
            message_kind=str(row["message_kind"]),
            body=str(row["body"]),
            created_at=float(row["created_at"]),
            in_reply_to=in_reply_to,
        )

    def _upsert_project_conn(
        self,
        conn: sqlite3.Connection,
        project: Project,
    ) -> None:
        self._ensure_project_slug_available(
            conn,
            project_id=project.project_id,
            slug=project.slug,
        )
        try:
            conn.execute(
                """
                INSERT INTO projects(
                    project_id,
                    slug,
                    name,
                    description,
                    owner_user_id,
                    status
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    slug = excluded.slug,
                    name = excluded.name,
                    description = excluded.description,
                    owner_user_id = excluded.owner_user_id,
                    status = excluded.status
                """,
                (
                    project.project_id,
                    project.slug,
                    project.name,
                    project.description,
                    project.owner_user_id,
                    project.status,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                f"project_slug_already_exists:{project.slug}"
            ) from exc

    def _set_project_policy_conn(
        self,
        conn: sqlite3.Connection,
        policy: ProjectPolicy,
    ) -> None:
        self._ensure_project_exists(conn, policy.project_id)
        conn.execute(
            """
            INSERT INTO project_policies(
                project_id,
                allow_hiring,
                allow_agent_dm,
                require_owner_approval_for_hires
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                allow_hiring = excluded.allow_hiring,
                allow_agent_dm = excluded.allow_agent_dm,
                require_owner_approval_for_hires =
                    excluded.require_owner_approval_for_hires
            """,
            (
                policy.project_id,
                int(policy.allow_hiring),
                int(policy.allow_agent_dm),
                int(policy.require_owner_approval_for_hires),
            ),
        )

    def _upsert_project_membership_conn(
        self,
        conn: sqlite3.Connection,
        membership: ProjectMembership,
    ) -> None:
        self._ensure_project_exists(conn, membership.project_id)
        conn.execute(
            """
            INSERT INTO project_members(
                project_id,
                member_id,
                member_type,
                role_name,
                status
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_id, member_id) DO UPDATE SET
                member_type = excluded.member_type,
                role_name = excluded.role_name,
                status = excluded.status
            """,
            (
                membership.project_id,
                membership.member_id,
                membership.member_type,
                membership.role_name,
                membership.status,
            ),
        )

    def _add_project_specialist_conn(
        self,
        conn: sqlite3.Connection,
        assignment: ProjectSpecialistAssignment,
    ) -> None:
        self._ensure_project_exists(conn, assignment.project_id)
        try:
            conn.execute(
                """
                INSERT INTO project_specialist_roster(
                    project_id,
                    specialist_role
                )
                VALUES (?, ?)
                """,
                (
                    assignment.project_id,
                    assignment.specialist_role,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                "duplicate_project_specialist:"
                f"{assignment.project_id}:{assignment.specialist_role}"
            ) from exc

    def _remove_project_specialist_conn(
        self,
        conn: sqlite3.Connection,
        assignment: ProjectSpecialistAssignment,
    ) -> None:
        self._ensure_project_exists(conn, assignment.project_id)
        cursor = conn.execute(
            """
            DELETE FROM project_specialist_roster
            WHERE project_id = ? AND specialist_role = ?
            """,
            (
                assignment.project_id,
                assignment.specialist_role,
            ),
        )
        if cursor.rowcount == 0:
            raise ValueError(
                "unknown_project_specialist:"
                f"{assignment.project_id}:{assignment.specialist_role}"
            )

    def _create_hire_request_conn(
        self,
        conn: sqlite3.Connection,
        request: PendingHireRequest,
    ) -> PendingHireRequest:
        self._ensure_project_exists(conn, request.project_id)
        if request.specialist_role in self._list_project_specialist_roles_conn(
            conn,
            request.project_id,
        ):
            raise ValueError(
                "project_specialist_already_present:"
                f"{request.project_id}:{request.specialist_role}"
            )
        existing_pending = self._get_pending_hire_request_row(
            conn,
            project_id=request.project_id,
            specialist_role=request.specialist_role,
            source=request.source,
        )
        if existing_pending is not None:
            persisted = self._row_to_pending_hire_request(existing_pending)
            if persisted is None:
                raise ValueError("existing_pending_hire_request_missing")
            return persisted
        try:
            conn.execute(
                """
                INSERT INTO project_hire_requests(
                    request_id,
                    project_id,
                    specialist_role,
                    reason,
                    source,
                    status,
                    created_at,
                    decided_at,
                    decided_by_user_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    request.project_id,
                    request.specialist_role,
                    request.reason,
                    request.source,
                    request.status,
                    request.created_at,
                    request.decided_at,
                    request.decided_by_user_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            existing_pending = self._get_pending_hire_request_row(
                conn,
                project_id=request.project_id,
                specialist_role=request.specialist_role,
                source=request.source,
            )
            if existing_pending is not None:
                persisted = self._row_to_pending_hire_request(existing_pending)
                if persisted is None:
                    raise ValueError("existing_pending_hire_request_missing") from exc
                return persisted
            raise ValueError(
                f"duplicate_hire_request_id:{request.request_id}"
            ) from exc
        persisted_row = self._get_hire_request_row(conn, request.request_id)
        persisted = self._row_to_pending_hire_request(persisted_row)
        if persisted is None:
            raise ValueError(
                f"inserted_hire_request_missing:{request.request_id}"
            )
        return persisted

    def _mark_hire_request_approved_conn(
        self,
        conn: sqlite3.Connection,
        request_id: str,
        *,
        actor_user_id: int,
    ) -> PendingHireRequest:
        row = self._get_hire_request_row(conn, request_id)
        request = self._row_to_pending_hire_request(row)
        if request is None:
            raise ValueError(f"unknown_hire_request:{request_id}")
        if request.status != "pending":
            return request
        with_duplicate_ok = ProjectSpecialistAssignment(
            project_id=request.project_id,
            specialist_role=request.specialist_role,
        )
        try:
            self._add_project_specialist_conn(conn, with_duplicate_ok)
        except ValueError as exc:
            if str(exc) != (
                "duplicate_project_specialist:"
                f"{request.project_id}:{request.specialist_role}"
            ):
                raise
        decided_at = self._normalise_timestamp(
            time.time(),
            field_name="hire_request_decided_at",
            allow_zero=False,
        )
        conn.execute(
            """
            UPDATE project_hire_requests
            SET status = 'approved',
                decided_at = ?,
                decided_by_user_id = ?
            WHERE request_id = ?
            """,
            (
                decided_at,
                actor_user_id,
                request_id,
            ),
        )
        updated = self._row_to_pending_hire_request(
            self._get_hire_request_row(conn, request_id)
        )
        if updated is None:
            raise ValueError(f"approved_hire_request_missing:{request_id}")
        return updated

    def _mark_hire_request_rejected_conn(
        self,
        conn: sqlite3.Connection,
        request_id: str,
        *,
        actor_user_id: int,
    ) -> PendingHireRequest:
        row = self._get_hire_request_row(conn, request_id)
        request = self._row_to_pending_hire_request(row)
        if request is None:
            raise ValueError(f"unknown_hire_request:{request_id}")
        if request.status != "pending":
            return request
        decided_at = self._normalise_timestamp(
            time.time(),
            field_name="hire_request_decided_at",
            allow_zero=False,
        )
        conn.execute(
            """
            UPDATE project_hire_requests
            SET status = 'rejected',
                decided_at = ?,
                decided_by_user_id = ?
            WHERE request_id = ?
            """,
            (
                decided_at,
                actor_user_id,
                request_id,
            ),
        )
        updated = self._row_to_pending_hire_request(
            self._get_hire_request_row(conn, request_id)
        )
        if updated is None:
            raise ValueError(f"rejected_hire_request_missing:{request_id}")
        return updated

    def _bind_project_chat_conn(
        self,
        conn: sqlite3.Connection,
        binding: ProjectChatBinding,
    ) -> None:
        self._ensure_project_exists(conn, binding.project_id)
        self._ensure_chat_binding_available(
            conn,
            project_id=binding.project_id,
            chat_provider=binding.chat_provider,
            chat_id=binding.chat_id,
        )
        try:
            conn.execute(
                """
                INSERT INTO project_chat_bindings(
                    project_id,
                    chat_provider,
                    chat_id
                )
                VALUES (?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    chat_provider = excluded.chat_provider,
                    chat_id = excluded.chat_id
                """,
                (
                    binding.project_id,
                    binding.chat_provider,
                    binding.chat_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                f"chat_binding_conflict:{binding.chat_provider}:{binding.chat_id}"
            ) from exc

    def _upsert_project_runtime_binding_conn(
        self,
        conn: sqlite3.Connection,
        binding: ProjectRuntimeBinding,
    ) -> None:
        self._ensure_project_exists(conn, binding.project_id)
        conn.execute(
            """
            INSERT INTO project_runtime_bindings(
                project_id,
                adapter_name,
                repo_path,
                worktree_root,
                base_branch,
                branch_prefix,
                language,
                rules_json,
                commands_json,
                forbidden_paths_json,
                forbidden_tokens_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                adapter_name = excluded.adapter_name,
                repo_path = excluded.repo_path,
                worktree_root = excluded.worktree_root,
                base_branch = excluded.base_branch,
                branch_prefix = excluded.branch_prefix,
                language = excluded.language,
                rules_json = excluded.rules_json,
                commands_json = excluded.commands_json,
                forbidden_paths_json = excluded.forbidden_paths_json,
                forbidden_tokens_json = excluded.forbidden_tokens_json
            """,
            (
                binding.project_id,
                binding.adapter_name,
                str(binding.repo_path),
                str(binding.worktree_root) if binding.worktree_root is not None else None,
                binding.base_branch,
                binding.branch_prefix,
                binding.language,
                json.dumps(
                    [
                        {
                            "name": rule.name,
                            "description": rule.description,
                            "severity": rule.severity,
                        }
                        for rule in binding.rules
                    ],
                    sort_keys=True,
                ),
                json.dumps(
                    [
                        {
                            "name": command.name,
                            "cmd": list(command.cmd),
                            "timeout_seconds": command.timeout_seconds,
                        }
                        for command in binding.commands
                    ],
                    sort_keys=True,
                ),
                json.dumps(list(binding.forbidden_paths)),
                json.dumps(list(binding.forbidden_tokens)),
            ),
        )

    def _upsert_project_thread_conn(
        self,
        conn: sqlite3.Connection,
        thread: ProjectThread,
    ) -> None:
        self._ensure_project_exists(conn, thread.project_id)
        if thread.task_id is not None:
            self._ensure_project_task_thread_available(
                conn,
                project_id=thread.project_id,
                task_id=thread.task_id,
                thread_id=thread.thread_id,
            )
        conn.execute(
            """
            INSERT INTO project_threads(
                project_id,
                thread_id,
                opened_by_role,
                status,
                created_at,
                last_message_at,
                task_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, thread_id) DO UPDATE SET
                opened_by_role = excluded.opened_by_role,
                status = excluded.status,
                created_at = excluded.created_at,
                last_message_at = excluded.last_message_at,
                task_id = excluded.task_id
            """,
            (
                thread.project_id,
                thread.thread_id,
                thread.opened_by_role,
                thread.status,
                thread.created_at,
                thread.last_message_at,
                thread.task_id,
            ),
        )

    def _insert_agent_bus_message_conn(
        self,
        conn: sqlite3.Connection,
        message: AgentMessage,
    ) -> None:
        self._ensure_project_exists(conn, message.project_id)
        thread_row = self._get_project_thread_row(
            conn,
            message.project_id,
            message.thread_id,
        )
        if thread_row is None:
            raise ValueError(
                "unknown_project_thread:"
                f"{message.project_id}:{message.thread_id}"
            )
        thread = self._row_to_project_thread(thread_row)
        if thread is None:
            raise ValueError(
                "unknown_project_thread:"
                f"{message.project_id}:{message.thread_id}"
            )
        if message.created_at < thread.last_message_at:
            raise ValueError(
                "message_created_at_before_thread_last_message_at:"
                f"{message.created_at}<{thread.last_message_at}"
            )
        if message.in_reply_to is not None:
            referenced_row = self._get_agent_bus_message_row(
                conn,
                message.in_reply_to.project_id,
                message.in_reply_to.thread_id,
                message.in_reply_to.message_id,
            )
            if referenced_row is None:
                raise ValueError(
                    f"unknown_in_reply_to:{message.in_reply_to.message_id}"
                )
            referenced_message = self._row_to_agent_bus_message(referenced_row)
            if referenced_message is None:
                raise ValueError(
                    f"unknown_in_reply_to:{message.in_reply_to.message_id}"
                )
            if referenced_message.message_kind != "request":
                raise ValueError(
                    "reply_target_must_be_request:"
                    f"{referenced_message.message_kind}"
                )
        try:
            conn.execute(
                """
                INSERT INTO agent_bus_messages(
                    project_id,
                    thread_id,
                    message_id,
                    sender_role,
                    recipient_role,
                    message_kind,
                    body,
                    created_at,
                    in_reply_to_project_id,
                    in_reply_to_thread_id,
                    in_reply_to_message_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.project_id,
                    message.thread_id,
                    message.message_id,
                    message.sender_role,
                    message.recipient_role,
                    message.message_kind,
                    message.body,
                    message.created_at,
                    (
                        None
                        if message.in_reply_to is None
                        else message.in_reply_to.project_id
                    ),
                    (
                        None
                        if message.in_reply_to is None
                        else message.in_reply_to.thread_id
                    ),
                    (
                        None
                        if message.in_reply_to is None
                        else message.in_reply_to.message_id
                    ),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                "duplicate_agent_bus_message:"
                f"{message.project_id}:{message.thread_id}:{message.message_id}"
            ) from exc
        conn.execute(
            """
            UPDATE project_threads
            SET last_message_at = ?
            WHERE project_id = ? AND thread_id = ?
            """,
            (
                message.created_at,
                message.project_id,
                message.thread_id,
            ),
        )

    def _upsert_agent_dm_session_conn(
        self,
        conn: sqlite3.Connection,
        session: AgentDmSession,
    ) -> None:
        project = self._get_project_row_by_id(conn, session.project_id)
        if project is None:
            raise ValueError(f"unknown_project_id:{session.project_id}")
        project_owner_user_id = int(project["owner_user_id"])
        if session.owner_user_id != project_owner_user_id:
            raise ValueError(
                "agent_dm_session_owner_project_mismatch:"
                f"{session.owner_user_id}!={project_owner_user_id}"
            )
        conn.execute(
            """
            INSERT INTO agent_dm_sessions(
                owner_user_id,
                project_id,
                agent_role,
                thread_bot_role,
                dm_chat_id,
                chat_provider,
                status,
                created_at,
                last_interaction_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_user_id, project_id, agent_role) DO UPDATE SET
                thread_bot_role = excluded.thread_bot_role,
                dm_chat_id = excluded.dm_chat_id,
                chat_provider = excluded.chat_provider,
                status = excluded.status,
                created_at = excluded.created_at,
                last_interaction_at = excluded.last_interaction_at
            """,
            (
                session.owner_user_id,
                session.project_id,
                session.agent_role,
                session.thread_bot_role,
                session.dm_chat_id,
                session.chat_provider,
                session.status,
                session.created_at,
                session.last_interaction_at,
            ),
        )

    def _record_agent_dm_message_conn(
        self,
        conn: sqlite3.Connection,
        message: AgentDmMessage,
        *,
        max_entries: int,
    ) -> None:
        session = self._get_agent_dm_session_row(
            conn,
            owner_user_id=message.owner_user_id,
            project_id=message.project_id,
            agent_role=message.agent_role,
        )
        if session is None:
            raise ValueError(
                "missing_agent_dm_session:"
                f"{message.owner_user_id}:{message.project_id}:{message.agent_role}"
            )
        session_status = str(session["status"])
        if session_status != "active":
            raise ValueError(
                "inactive_agent_dm_session:"
                f"{message.owner_user_id}:{message.project_id}:"
                f"{message.agent_role}:{session_status}"
            )
        conn.execute(
            """
            INSERT INTO agent_dm_messages(
                owner_user_id,
                project_id,
                agent_role,
                sender_kind,
                sender_role,
                body,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.owner_user_id,
                message.project_id,
                message.agent_role,
                message.sender_kind,
                message.sender_role,
                message.body,
                message.created_at,
            ),
        )
        self._trim_agent_dm_messages_conn(
            conn,
            message.owner_user_id,
            message.project_id,
            message.agent_role,
            max_entries,
        )

    def _insert_agent_owner_notification_conn(
        self,
        conn: sqlite3.Connection,
        notification: AgentOwnerNotification,
    ) -> AgentOwnerNotification:
        if notification.notification_id is not None:
            raise ValueError("notification_id_must_be_none_for_insert")
        project = self._get_project_row_by_id(conn, notification.project_id)
        if project is None:
            raise ValueError(f"unknown_project_id:{notification.project_id}")
        project_owner_user_id = int(project["owner_user_id"])
        if notification.owner_user_id != project_owner_user_id:
            raise ValueError(
                "agent_owner_notification_owner_project_mismatch:"
                f"{notification.owner_user_id}!={project_owner_user_id}"
            )
        session = self._get_agent_dm_session_row(
            conn,
            owner_user_id=notification.owner_user_id,
            project_id=notification.project_id,
            agent_role=notification.agent_role,
        )
        if (
            session is not None
            and str(session["thread_bot_role"]) != notification.thread_bot_role
        ):
            raise ValueError(
                "agent_owner_notification_thread_bot_role_mismatch:"
                f"{notification.thread_bot_role}!={session['thread_bot_role']}"
            )
        cursor = conn.execute(
            """
            INSERT INTO agent_owner_notifications(
                owner_user_id,
                project_id,
                agent_role,
                thread_bot_role,
                body,
                chat_provider,
                status,
                created_at,
                delivered_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                notification.owner_user_id,
                notification.project_id,
                notification.agent_role,
                notification.thread_bot_role,
                notification.body,
                notification.chat_provider,
                notification.status,
                notification.created_at,
                notification.delivered_at,
            ),
        )
        row = self._get_agent_owner_notification_row(
            conn,
            int(cursor.lastrowid),
        )
        persisted = self._row_to_agent_owner_notification(row)
        if persisted is None:
            raise ValueError("inserted_agent_owner_notification_missing")
        return persisted

    def _mark_agent_owner_notification_delivered_conn(
        self,
        conn: sqlite3.Connection,
        notification_id: int,
        *,
        delivered_at: float,
    ) -> AgentOwnerNotification:
        row = self._get_agent_owner_notification_row(conn, notification_id)
        if row is None:
            raise ValueError(f"unknown_notification_id:{notification_id}")
        notification = self._row_to_agent_owner_notification(row)
        if notification is None:
            raise ValueError(f"unknown_notification_id:{notification_id}")
        if notification.status != "queued":
            raise ValueError(
                "notification_not_queued:"
                f"{notification_id}:{notification.status}"
            )
        conn.execute(
            """
            UPDATE agent_owner_notifications
            SET status = 'delivered',
                delivered_at = ?
            WHERE id = ?
            """,
            (delivered_at, notification_id),
        )
        updated_row = self._get_agent_owner_notification_row(conn, notification_id)
        updated = self._row_to_agent_owner_notification(updated_row)
        if updated is None:
            raise ValueError(
                f"updated_agent_owner_notification_missing:{notification_id}"
            )
        return updated

    def _trim_agent_dm_messages_conn(
        self,
        conn: sqlite3.Connection,
        owner_user_id: int,
        project_id: str,
        agent_role: str,
        max_entries: int,
    ) -> None:
        conn.execute(
            """
            DELETE FROM agent_dm_messages
            WHERE owner_user_id = ? AND project_id = ? AND agent_role = ?
              AND id NOT IN (
                  SELECT id
                  FROM agent_dm_messages
                  WHERE owner_user_id = ? AND project_id = ? AND agent_role = ?
                  ORDER BY id DESC
                  LIMIT ?
              )
            """,
            (
                owner_user_id,
                project_id,
                agent_role,
                owner_user_id,
                project_id,
                agent_role,
                max_entries,
            ),
        )

    def _get_project_thread_row(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        thread_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT project_id, thread_id, opened_by_role, status, created_at,
                   last_message_at, task_id
            FROM project_threads
            WHERE project_id = ? AND thread_id = ?
            """,
            (project_id, thread_id),
        ).fetchone()

    def _get_project_thread_by_task_row(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        task_id: str,
    ) -> sqlite3.Row | None:
        rows = conn.execute(
            """
            SELECT project_id, thread_id, opened_by_role, status, created_at,
                   last_message_at, task_id
            FROM project_threads
            WHERE project_id = ? AND task_id = ?
            ORDER BY last_message_at DESC, thread_id ASC
            """,
            (project_id, task_id),
        ).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise ValueError(
                f"duplicate_project_task_thread:{project_id}:{task_id}"
            )
        return rows[0]

    def _get_agent_bus_message_row(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        thread_id: str,
        message_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT project_id, thread_id, message_id, sender_role,
                   recipient_role, message_kind, body, created_at,
                   in_reply_to_project_id, in_reply_to_thread_id,
                   in_reply_to_message_id
            FROM agent_bus_messages
            WHERE project_id = ? AND thread_id = ? AND message_id = ?
            """,
            (project_id, thread_id, message_id),
        ).fetchone()

    def _list_project_specialist_roles_conn(
        self,
        conn: sqlite3.Connection,
        project_id: str,
    ) -> tuple[str, ...]:
        rows = conn.execute(
            """
            SELECT specialist_role
            FROM project_specialist_roster
            WHERE project_id = ?
            ORDER BY specialist_role ASC
            """,
            (project_id,),
        ).fetchall()
        return tuple(str(row["specialist_role"]) for row in rows)

    def _get_hire_request_row(
        self,
        conn: sqlite3.Connection,
        request_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT request_id, project_id, specialist_role, reason, source,
                   status, created_at, decided_at, decided_by_user_id
            FROM project_hire_requests
            WHERE request_id = ?
            """,
            (request_id,),
        ).fetchone()

    def _get_pending_hire_request_row(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str,
        specialist_role: str,
        source: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT request_id, project_id, specialist_role, reason, source,
                   status, created_at, decided_at, decided_by_user_id
            FROM project_hire_requests
            WHERE project_id = ?
              AND specialist_role = ?
              AND source = ?
              AND status = 'pending'
            ORDER BY created_at ASC, request_id ASC
            LIMIT 1
            """,
            (
                project_id,
                specialist_role,
                source,
            ),
        ).fetchone()

    def _get_project_row_by_id(
        self,
        conn: sqlite3.Connection,
        project_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT project_id, slug, name, description, owner_user_id, status
            FROM projects
            WHERE project_id = ?
            """,
            (project_id,),
        ).fetchone()

    def _get_agent_dm_session_row(
        self,
        conn: sqlite3.Connection,
        *,
        owner_user_id: int,
        project_id: str,
        agent_role: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT owner_user_id, project_id, agent_role, thread_bot_role,
                   dm_chat_id, chat_provider, status, created_at,
                   last_interaction_at
            FROM agent_dm_sessions
            WHERE owner_user_id = ? AND project_id = ? AND agent_role = ?
            """,
            (owner_user_id, project_id, agent_role),
        ).fetchone()

    def _get_agent_owner_notification_row(
        self,
        conn: sqlite3.Connection,
        notification_id: int,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT id, owner_user_id, project_id, agent_role, thread_bot_role,
                   body, chat_provider, status, created_at, delivered_at
            FROM agent_owner_notifications
            WHERE id = ?
            """,
            (notification_id,),
        ).fetchone()

    def _project_exists_conn(
        self,
        conn: sqlite3.Connection,
        project_id: str,
    ) -> bool:
        row = conn.execute(
            """
            SELECT 1
            FROM projects
            WHERE project_id = ?
            """,
            (project_id,),
        ).fetchone()
        return row is not None

    def _ensure_project_exists(
        self,
        conn: sqlite3.Connection,
        project_id: str,
    ) -> None:
        row = conn.execute(
            """
            SELECT 1
            FROM projects
            WHERE project_id = ?
            """,
            (project_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown_project_id:{project_id}")

    def _ensure_project_slug_available(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str,
        slug: str,
    ) -> None:
        row = conn.execute(
            """
            SELECT project_id
            FROM projects
            WHERE slug = ?
            """,
            (slug,),
        ).fetchone()
        if row is not None and str(row["project_id"]) != project_id:
            raise ValueError(f"project_slug_already_exists:{slug}")

    def _ensure_project_task_thread_available(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str,
        task_id: str,
        thread_id: str,
    ) -> None:
        rows = conn.execute(
            """
            SELECT thread_id
            FROM project_threads
            WHERE project_id = ? AND task_id = ?
            ORDER BY thread_id ASC
            """,
            (project_id, task_id),
        ).fetchall()
        if not rows:
            return
        thread_ids = {str(row["thread_id"]) for row in rows}
        if len(thread_ids) > 1:
            raise ValueError(
                f"duplicate_project_task_thread:{project_id}:{task_id}"
            )
        existing_thread_id = next(iter(thread_ids))
        if existing_thread_id != thread_id:
            raise ValueError(
                f"duplicate_project_task_thread:{project_id}:{task_id}"
            )

    def _ensure_chat_binding_available(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str,
        chat_provider: str,
        chat_id: int,
    ) -> None:
        row = conn.execute(
            """
            SELECT project_id
            FROM project_chat_bindings
            WHERE chat_provider = ? AND chat_id = ?
            """,
            (chat_provider, chat_id),
        ).fetchone()
        if row is not None and str(row["project_id"]) != project_id:
            raise ValueError(
                f"chat_binding_conflict:{chat_provider}:{chat_id}"
            )

    @staticmethod
    def _validate_chat_id(chat_id: int) -> None:
        if (
            isinstance(chat_id, bool)
            or not isinstance(chat_id, int)
            or chat_id == 0
        ):
            raise ValueError(f"invalid_chat_id:{chat_id!r}")

    @staticmethod
    def _validate_positive_int(value: int, field_name: str) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
        ):
            raise ValueError(f"invalid_{field_name}:{value!r}")

    @staticmethod
    def _validate_non_empty_text(value: str, field_name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"empty_{field_name}")

    @staticmethod
    def _normalize_project_identifier(value: str, *, field_name: str) -> str:
        StateDB._validate_non_empty_text(value, field_name)
        normalized = value.strip().lower()
        if not normalized.isascii():
            raise ValueError(f"non_ascii_{field_name}")
        if not _IDENTIFIER_RE.fullmatch(normalized):
            raise ValueError(f"invalid_{field_name}:{normalized}")
        return normalized

    @staticmethod
    def _normalize_project_slug(value: str, *, field_name: str) -> str:
        StateDB._validate_non_empty_text(value, field_name)
        normalized = value.strip().lower()
        if not normalized.isascii():
            raise ValueError(f"non_ascii_{field_name}")
        if not _SLUG_RE.fullmatch(normalized):
            raise ValueError(f"invalid_{field_name}:{normalized}")
        return normalized

    @staticmethod
    def _normalize_task_identifier(value: str, *, field_name: str) -> str:
        StateDB._validate_non_empty_text(value, field_name)
        normalized = value.strip().lower()
        if not normalized.isascii():
            raise ValueError(f"non_ascii_{field_name}")
        if not _TASK_ID_RE.fullmatch(normalized):
            raise ValueError(f"invalid_{field_name}:{normalized}")
        return normalized

    @staticmethod
    def _normalize_hire_request_id(value: str) -> str:
        StateDB._validate_non_empty_text(value, "hire_request_id")
        normalized = value.strip().lower()
        if not normalized.isascii():
            raise ValueError(f"invalid_hire_request_id:{normalized}")
        if not re.fullmatch(r"^[a-z0-9][a-z0-9_-]{0,127}$", normalized):
            raise ValueError(f"invalid_hire_request_id:{normalized}")
        return normalized

    @staticmethod
    def _normalize_chat_provider(chat_provider: str) -> str:
        normalized = StateDB._normalize_project_identifier(
            chat_provider,
            field_name="chat_provider",
        )
        if normalized not in VALID_CHAT_PROVIDERS:
            raise ValueError(f"invalid_chat_provider:{normalized}")
        return normalized

    @staticmethod
    def _validate_transport_chat_id(chat_id: int) -> None:
        if (
            isinstance(chat_id, bool)
            or not isinstance(chat_id, int)
            or chat_id == 0
        ):
            raise ValueError(f"invalid_chat_id:{chat_id!r}")

    @staticmethod
    def _validate_budget(usd: float) -> None:
        if (
            isinstance(usd, bool)
            or not isinstance(usd, (int, float))
            or usd < 0
        ):
            raise ValueError(f"invalid_budget:{usd!r}")

    @staticmethod
    def _normalise_timestamp(
        value: float,
        *,
        field_name: str,
        allow_zero: bool,
    ) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value < 0
            or (value == 0 and not allow_zero)
        ):
            raise ValueError(f"invalid_{field_name}:{value!r}")
        return float(value)
