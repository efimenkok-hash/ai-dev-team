"""
core/state_db.py

SQLite-backed persistent state for chat tier selection, completed task
history, and per-chat budget.

Design goals:
1. Single-file stdlib-only storage (`sqlite3`), safe for bot restarts.
2. WAL mode enabled so future read-heavy surfaces (web dashboard) can query
   while the bot keeps writing.
3. API mirrors the current in-memory stores closely, so migration can be
   incremental: TierSessionStore / TaskHistory / budget can swap internals
   without changing their public contracts.

CONTRACTS:
1. StateDB(path) requires a Path; constructor creates parent directories and
   initializes/migrates the schema eagerly.
2. Current schema version is 2. Unknown future versions raise ValueError.
3. Every public method validates arguments via isinstance/ValueError.
4. Writes are serialized with a process-local lock; reads use independent
   SQLite connections and remain safe under concurrent access.
5. task_history keeps an append-only audit log. get_task(task_id) returns the
   newest record for that task_id; recent_tasks(n) returns newest-last order.
6. schema migration supports v1 -> v2:
   - tier_sessions.tier_name -> active_tier
   - task_history gains AUTOINCREMENT id for stable append-order semantics
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from core.task_history import TaskSummary

_CURRENT_SCHEMA_VERSION = 2
_SQLITE_TIMEOUT_SECONDS = 30.0

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
    # Schema management
    # ------------------------------------------------------------------

    def _initialize_schema(self) -> None:
        with self._lock, self._connect() as conn:
            version = self._detect_schema_version(conn)
            if version == 0:
                self._create_v2_schema(conn)
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
                raise ValueError(f"unsupported_schema_version:{version}")

    def _create_v2_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(_CREATE_SCHEMA_META)
        conn.execute(_CREATE_TIER_SESSIONS)
        conn.execute(_CREATE_TASK_HISTORY)
        conn.execute(_CREATE_BUDGET)
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
