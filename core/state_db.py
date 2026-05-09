"""
core/state_db.py

SQLite-backed persistent state for chat tier selection, completed task
history, per-chat budget, and the AI Office project model.

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
2. Current schema version is 4. Unknown future versions raise ValueError.
3. Every public method validates arguments via isinstance/ValueError.
4. Writes are serialized with a process-local lock; reads use independent
   SQLite connections and remain safe under concurrent access.
5. task_history keeps an append-only audit log. get_task(task_id) returns the
   newest record for that task_id; recent_tasks(n) returns newest-last order.
6. schema migration supports v1 -> v2 -> v3 -> v4:
   - v1 -> v2: tier_sessions.tier_name -> active_tier
   - v1 -> v2: task_history gains AUTOINCREMENT id for stable append-order
     semantics
   - v2 -> v3: adds projects, project_policies, project_members,
     project_chat_bindings
   - v3 -> v4: adds project_runtime_bindings
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
from core.project_models import (
    VALID_CHAT_PROVIDERS,
    Project,
    ProjectChatBinding,
    ProjectMembership,
    ProjectPolicy,
)
from core.project_runtime import ProjectRuntimeBinding
from core.task_history import TaskSummary

_CURRENT_SCHEMA_VERSION = 4
_SQLITE_TIMEOUT_SECONDS = 30.0
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
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
    finished_at REAL NOT NULL
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

_CREATE_INDEX_TASK_HISTORY_TASK_ID = """
CREATE INDEX IF NOT EXISTS idx_task_history_task_id_id
ON task_history(task_id, id DESC)
"""

_CREATE_INDEX_TASK_HISTORY_FINISHED = """
CREATE INDEX IF NOT EXISTS idx_task_history_finished_id
ON task_history(finished_at DESC, id DESC)
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
                    finished_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary.task_id,
                    summary.branch,
                    summary.commit_sha,
                    summary.final_state,
                    summary.failure_reason,
                    summary.tier_name,
                    float(summary.finished_at),
                ),
            )

    def get_task(self, task_id: str) -> TaskSummary | None:
        self._validate_non_empty_text(task_id, "task_id")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT task_id, branch, commit_sha, final_state,
                       failure_reason, tier_name, finished_at
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
                       failure_reason, tier_name, finished_at
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
    # Schema management
    # ------------------------------------------------------------------

    def _initialize_schema(self) -> None:
        with self._lock, self._connect() as conn:
            version = self._detect_schema_version(conn)
            if version == 0:
                self._create_v4_schema(conn)
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
                raise ValueError(f"unsupported_schema_version:{version}")

    def _create_v4_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(_CREATE_SCHEMA_META)
        conn.execute(_CREATE_TIER_SESSIONS)
        conn.execute(_CREATE_TASK_HISTORY)
        conn.execute(_CREATE_BUDGET)
        conn.execute(_CREATE_PROJECTS)
        conn.execute(_CREATE_PROJECT_POLICIES)
        conn.execute(_CREATE_PROJECT_MEMBERS)
        conn.execute(_CREATE_PROJECT_CHAT_BINDINGS)
        conn.execute(_CREATE_PROJECT_RUNTIME_BINDINGS)
        conn.execute(_CREATE_INDEX_TASK_HISTORY_TASK_ID)
        conn.execute(_CREATE_INDEX_TASK_HISTORY_FINISHED)
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
        )
        if columns == desired:
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
        return TaskSummary(
            task_id=str(row["task_id"]),
            branch=str(row["branch"]),
            commit_sha=row["commit_sha"],
            final_state=str(row["final_state"]),
            failure_reason=row["failure_reason"],
            tier_name=str(row["tier_name"]),
            finished_at=float(row["finished_at"]),
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
            or chat_id <= 0
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
