"""Tests for core.state_db."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from core.state_db import StateDB
from core.task_history import TaskSummary


def _summary(
    task_id: str = "task-001",
    branch: str = "feature/task-001",
    commit_sha: str | None = "abc123def456789",
    final_state: str = "SUCCESS",
    failure_reason: str | None = None,
    tier_name: str = "ECONOMY",
    finished_at: float | None = None,
) -> TaskSummary:
    return TaskSummary(
        task_id=task_id,
        branch=branch,
        commit_sha=commit_sha,
        final_state=final_state,
        failure_reason=failure_reason,
        tier_name=tier_name,
        finished_at=finished_at if finished_at is not None else time.time(),
    )


def _make_db(tmp_path: Path) -> StateDB:
    return StateDB(tmp_path / "state.db")


def _build_v1_db(path: Path, *, with_schema_meta: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        if with_schema_meta:
            conn.execute(
                "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES ('schema_version', '1')"
            )
        conn.execute(
            """
            CREATE TABLE tier_sessions (
                chat_id INTEGER PRIMARY KEY,
                tier_name TEXT NOT NULL,
                last_changed_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE task_history (
                task_id TEXT PRIMARY KEY,
                branch TEXT NOT NULL,
                commit_sha TEXT,
                final_state TEXT NOT NULL,
                failure_reason TEXT,
                tier_name TEXT NOT NULL,
                finished_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE budget (
                chat_id INTEGER PRIMARY KEY,
                usd REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO tier_sessions(chat_id, tier_name, last_changed_at)
            VALUES (1, 'STANDARD', 1234.5)
            """
        )
        conn.execute(
            """
            INSERT INTO task_history(
                task_id, branch, commit_sha, final_state,
                failure_reason, tier_name, finished_at
            )
            VALUES (
                'task-v1',
                'feature/task-v1',
                'deadbeef',
                'SUCCESS',
                NULL,
                'STANDARD',
                2222.5
            )
            """
        )
        conn.execute(
            """
            INSERT INTO budget(chat_id, usd)
            VALUES (1, 7.5)
            """
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Construction / schema
# ---------------------------------------------------------------------------


def test_constructor_rejects_non_path():
    with pytest.raises(ValueError, match="invalid_path_type"):
        StateDB("not-a-path")  # type: ignore[arg-type]


def test_constructor_rejects_empty_path():
    with pytest.raises(ValueError, match="empty_path"):
        StateDB(Path(""))


def test_constructor_creates_parent_directory(tmp_path: Path):
    db_path = tmp_path / "nested" / "deeper" / "state.db"
    StateDB(db_path)
    assert db_path.parent.exists()


def test_constructor_creates_database_file(tmp_path: Path):
    db_path = tmp_path / "state.db"
    StateDB(db_path)
    assert db_path.exists()


def test_schema_version_is_current(tmp_path: Path):
    db = _make_db(tmp_path)
    assert db.schema_version() == 2


def test_wal_mode_enabled(tmp_path: Path):
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db.path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert str(mode).lower() == "wal"


def test_path_property_returns_normalized_path(tmp_path: Path):
    db_path = tmp_path / "state.db"
    db = StateDB(db_path)
    assert db.path == db_path.expanduser()


def test_constructor_rejects_partial_legacy_schema(tmp_path: Path):
    db_path = tmp_path / "broken.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE tier_sessions (chat_id INTEGER PRIMARY KEY, tier_name TEXT, last_changed_at REAL)"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="partial_legacy_schema"):
        StateDB(db_path)


def test_constructor_rejects_future_schema_version(tmp_path: Path):
    db_path = tmp_path / "future.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', '999')"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="unsupported_schema_version:999"):
        StateDB(db_path)


def test_constructor_rejects_invalid_schema_version_value(tmp_path: Path):
    db_path = tmp_path / "invalid.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', 'oops')"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="invalid_schema_version"):
        StateDB(db_path)


# ---------------------------------------------------------------------------
# Tier sessions
# ---------------------------------------------------------------------------


def test_get_tier_returns_none_for_unknown_chat(tmp_path: Path):
    db = _make_db(tmp_path)
    assert db.get_tier(1) is None


def test_set_tier_and_get_tier_round_trip(tmp_path: Path):
    db = _make_db(tmp_path)
    db.set_tier(42, "STANDARD")
    assert db.get_tier(42) == "STANDARD"


def test_list_tiers_returns_sorted_rows(tmp_path: Path):
    db = _make_db(tmp_path)
    db.set_tier(7, "STANDARD", last_changed_at=7.0)
    db.set_tier(3, "ECONOMY", last_changed_at=3.0)
    db.set_tier(9, "PREMIUM", last_changed_at=9.0)

    assert db.list_tiers() == (
        (3, "ECONOMY", 3.0),
        (7, "STANDARD", 7.0),
        (9, "PREMIUM", 9.0),
    )


def test_set_tier_strips_whitespace(tmp_path: Path):
    db = _make_db(tmp_path)
    db.set_tier(5, "  PREMIUM  ")
    assert db.get_tier(5) == "PREMIUM"


def test_set_tier_overwrites_existing_value(tmp_path: Path):
    db = _make_db(tmp_path)
    db.set_tier(8, "ECONOMY")
    db.set_tier(8, "STANDARD")
    assert db.get_tier(8) == "STANDARD"


def test_reset_tier_deletes_entry(tmp_path: Path):
    db = _make_db(tmp_path)
    db.set_tier(9, "ECONOMY")
    db.reset_tier(9)
    assert db.get_tier(9) is None


def test_reset_tier_is_idempotent(tmp_path: Path):
    db = _make_db(tmp_path)
    db.reset_tier(123)
    assert db.get_tier(123) is None


@pytest.mark.parametrize("bad", [0, -1, True, "1"])
def test_tier_methods_reject_invalid_chat_id(tmp_path: Path, bad):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_chat_id"):
        db.get_tier(bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid_chat_id"):
        db.set_tier(bad, "STANDARD")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid_chat_id"):
        db.reset_tier(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["", "   ", 123])
def test_set_tier_rejects_invalid_tier_name(tmp_path: Path, bad):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="empty_tier_name"):
        db.set_tier(1, bad)  # type: ignore[arg-type]


def test_concurrent_tier_reads_and_writes_do_not_crash(tmp_path: Path):
    db = _make_db(tmp_path)
    errors: list[Exception] = []

    def writer(chat_id: int) -> None:
        try:
            for _ in range(20):
                db.set_tier(chat_id, "STANDARD")
        except Exception as exc:
            errors.append(exc)

    def reader(chat_id: int) -> None:
        try:
            for _ in range(20):
                _ = db.get_tier(chat_id)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(1, 6)]
    threads += [threading.Thread(target=reader, args=(i,)) for i in range(1, 6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    for chat_id in range(1, 6):
        assert db.get_tier(chat_id) == "STANDARD"


# ---------------------------------------------------------------------------
# Task history
# ---------------------------------------------------------------------------


def test_record_task_and_get_task_round_trip(tmp_path: Path):
    db = _make_db(tmp_path)
    summary = _summary(task_id="task-a")
    db.record_task(summary)
    assert db.get_task("task-a") == summary


def test_get_task_returns_none_for_unknown_task(tmp_path: Path):
    db = _make_db(tmp_path)
    assert db.get_task("missing-task") is None


def test_record_task_allows_duplicate_task_id_and_gets_newest(tmp_path: Path):
    db = _make_db(tmp_path)
    older = _summary(task_id="task-x", final_state="FAIL")
    newer = _summary(task_id="task-x", final_state="SUCCESS")
    db.record_task(older)
    db.record_task(newer)
    assert db.get_task("task-x") == newer


def test_recent_tasks_returns_newest_last(tmp_path: Path):
    db = _make_db(tmp_path)
    for i in range(4):
        db.record_task(_summary(task_id=f"task-{i}"))
    recent = db.recent_tasks(3)
    assert [item.task_id for item in recent] == ["task-1", "task-2", "task-3"]


def test_recent_tasks_returns_all_when_fewer_than_requested(tmp_path: Path):
    db = _make_db(tmp_path)
    db.record_task(_summary(task_id="task-1"))
    db.record_task(_summary(task_id="task-2"))
    assert len(db.recent_tasks(10)) == 2


def test_recent_tasks_empty_when_no_rows(tmp_path: Path):
    db = _make_db(tmp_path)
    assert db.recent_tasks(5) == []


def test_recent_tasks_keeps_duplicate_entries_in_audit_log(tmp_path: Path):
    db = _make_db(tmp_path)
    db.record_task(_summary(task_id="task-z", final_state="FAIL"))
    db.record_task(_summary(task_id="task-z", final_state="SUCCESS"))
    recent = db.recent_tasks(2)
    assert len(recent) == 2
    assert [item.final_state for item in recent] == ["FAIL", "SUCCESS"]


def test_record_task_rejects_non_summary(tmp_path: Path):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_summary_type"):
        db.record_task("bad")  # type: ignore[arg-type]


def test_trim_task_history_keeps_newest_rows(tmp_path: Path):
    db = _make_db(tmp_path)
    for i in range(4):
        db.record_task(_summary(task_id=f"task-{i}"))

    db.trim_task_history(2)

    recent = db.recent_tasks(10)
    assert [item.task_id for item in recent] == ["task-2", "task-3"]
    assert db.get_task("task-0") is None
    assert db.get_task("task-3") is not None


@pytest.mark.parametrize("bad", ["", "   ", 123])
def test_get_task_rejects_invalid_task_id(tmp_path: Path, bad):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="empty_task_id"):
        db.get_task(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0, -1, True, "3"])
def test_recent_tasks_rejects_invalid_n(tmp_path: Path, bad):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_n"):
        db.recent_tasks(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0, -1, True, "3"])
def test_trim_task_history_rejects_invalid_max_entries(tmp_path: Path, bad):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_max_entries"):
        db.trim_task_history(bad)  # type: ignore[arg-type]


def test_concurrent_task_history_reads_and_writes_do_not_crash(tmp_path: Path):
    db = _make_db(tmp_path)
    errors: list[Exception] = []

    def writer(worker_id: int) -> None:
        try:
            for i in range(10):
                db.record_task(_summary(task_id=f"task-{worker_id}-{i}"))
        except Exception as exc:
            errors.append(exc)

    def reader() -> None:
        try:
            for _ in range(20):
                _ = db.recent_tasks(5)
                _ = db.get_task("task-0-0")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    threads += [threading.Thread(target=reader) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert db.get_task("task-0-0") is not None


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_get_budget_returns_none_for_unknown_chat(tmp_path: Path):
    db = _make_db(tmp_path)
    assert db.get_budget(1) is None


def test_set_budget_and_get_budget_round_trip(tmp_path: Path):
    db = _make_db(tmp_path)
    db.set_budget(7, 12.5)
    assert db.get_budget(7) == pytest.approx(12.5)


def test_set_budget_overwrites_existing_value(tmp_path: Path):
    db = _make_db(tmp_path)
    db.set_budget(3, 5.0)
    db.set_budget(3, 7.25)
    assert db.get_budget(3) == pytest.approx(7.25)


def test_set_budget_allows_zero(tmp_path: Path):
    db = _make_db(tmp_path)
    db.set_budget(11, 0.0)
    assert db.get_budget(11) == pytest.approx(0.0)


@pytest.mark.parametrize("bad", [0, -1, True, "1"])
def test_budget_methods_reject_invalid_chat_id(tmp_path: Path, bad):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_chat_id"):
        db.get_budget(bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid_chat_id"):
        db.set_budget(bad, 1.0)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [-1.0, True, "5"])
def test_set_budget_rejects_invalid_amount(tmp_path: Path, bad):
    db = _make_db(tmp_path)
    with pytest.raises(ValueError, match="invalid_budget"):
        db.set_budget(1, bad)  # type: ignore[arg-type]


def test_concurrent_budget_reads_and_writes_do_not_crash(tmp_path: Path):
    db = _make_db(tmp_path)
    errors: list[Exception] = []

    def writer(chat_id: int) -> None:
        try:
            for i in range(20):
                db.set_budget(chat_id, float(i))
        except Exception as exc:
            errors.append(exc)

    def reader(chat_id: int) -> None:
        try:
            for _ in range(20):
                _ = db.get_budget(chat_id)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(1,))]
    threads += [threading.Thread(target=reader, args=(1,)) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert db.get_budget(1) == pytest.approx(19.0)


# ---------------------------------------------------------------------------
# Migration v1 -> v2
# ---------------------------------------------------------------------------


def test_migrates_v1_schema_with_schema_meta(tmp_path: Path):
    db_path = tmp_path / "v1.db"
    _build_v1_db(db_path, with_schema_meta=True)

    db = StateDB(db_path)

    assert db.schema_version() == 2
    assert db.get_tier(1) == "STANDARD"
    task = db.get_task("task-v1")
    assert task is not None
    assert task.branch == "feature/task-v1"
    assert db.get_budget(1) == pytest.approx(7.5)


def test_migrates_v1_schema_without_schema_meta(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    _build_v1_db(db_path, with_schema_meta=False)

    db = StateDB(db_path)

    assert db.schema_version() == 2
    assert db.get_tier(1) == "STANDARD"
    assert db.get_budget(1) == pytest.approx(7.5)


def test_v1_task_history_migration_preserves_data(tmp_path: Path):
    db_path = tmp_path / "tasks.db"
    _build_v1_db(db_path, with_schema_meta=True)

    db = StateDB(db_path)
    task = db.get_task("task-v1")

    assert task is not None
    assert task.commit_sha == "deadbeef"
    assert task.final_state == "SUCCESS"
    assert task.tier_name == "STANDARD"


def test_v1_migration_renames_tier_name_column_to_active_tier(tmp_path: Path):
    db_path = tmp_path / "tiers.db"
    _build_v1_db(db_path, with_schema_meta=True)

    StateDB(db_path)

    conn = sqlite3.connect(db_path)
    try:
        columns = [
            row[1]
            for row in conn.execute("PRAGMA table_info(tier_sessions)").fetchall()
        ]
    finally:
        conn.close()

    assert "active_tier" in columns
    assert "tier_name" not in columns


def test_v1_migration_adds_autoincrement_id_to_task_history(tmp_path: Path):
    db_path = tmp_path / "history.db"
    _build_v1_db(db_path, with_schema_meta=True)

    StateDB(db_path)

    conn = sqlite3.connect(db_path)
    try:
        columns = [
            row[1]
            for row in conn.execute("PRAGMA table_info(task_history)").fetchall()
        ]
    finally:
        conn.close()

    assert columns[0] == "id"
