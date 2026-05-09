"""Tests for core.task_history (Step 14c-1)."""

import threading
import time

import pytest

from core.state_db import StateDB
from core.task_history import TaskHistory, TaskSummary

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _summary(
    task_id: str = "task-001",
    branch: str = "feature/task-001",
    commit_sha: str | None = "abc123def456789",
    final_state: str = "SUCCESS",
    failure_reason: str | None = None,
    tier_name: str = "ECONOMY",
    finished_at: float | None = None,
    project_id: str | None = None,
) -> TaskSummary:
    return TaskSummary(
        task_id=task_id,
        branch=branch,
        commit_sha=commit_sha,
        final_state=final_state,
        failure_reason=failure_reason,
        tier_name=tier_name,
        finished_at=finished_at if finished_at is not None else time.time(),
        project_id=project_id,
    )


# ---------------------------------------------------------------------------
# TaskSummary validation
# ---------------------------------------------------------------------------


def test_summary_happy_path():
    s = _summary()
    assert s.task_id == "task-001"
    assert s.commit_sha == "abc123def456789"


def test_summary_none_commit_sha_allowed():
    s = _summary(commit_sha=None, final_state="FAIL", failure_reason="ruff_error")
    assert s.commit_sha is None


def test_summary_none_failure_reason_allowed():
    s = _summary(failure_reason=None)
    assert s.failure_reason is None


def test_summary_project_id_round_trip():
    s = _summary(project_id="alpha_project")

    assert s.project_id == "alpha_project"


@pytest.mark.parametrize("bad", ["", "   "])
def test_summary_rejects_empty_task_id(bad):
    with pytest.raises(ValueError, match="empty_task_id"):
        _summary(task_id=bad)


@pytest.mark.parametrize("bad", ["", "   "])
def test_summary_rejects_empty_branch(bad):
    with pytest.raises(ValueError, match="empty_branch"):
        _summary(branch=bad)


@pytest.mark.parametrize("bad", ["", "   "])
def test_summary_rejects_empty_final_state(bad):
    with pytest.raises(ValueError, match="empty_final_state"):
        _summary(final_state=bad)


@pytest.mark.parametrize("bad", ["", "   "])
def test_summary_rejects_empty_tier_name(bad):
    with pytest.raises(ValueError, match="empty_tier_name"):
        _summary(tier_name=bad)


@pytest.mark.parametrize("bad", [0, -1.0, True, None])
def test_summary_rejects_invalid_finished_at(bad):
    # Must pass bad value directly — helper substitutes None with time.time().
    with pytest.raises(ValueError, match="invalid_finished_at"):
        TaskSummary(
            task_id="t",
            branch="b",
            commit_sha=None,
            final_state="FAIL",
            failure_reason=None,
            tier_name="ECONOMY",
            finished_at=bad,
        )


def test_summary_is_frozen():
    s = _summary()
    with pytest.raises(Exception):
        s.task_id = "mutated"  # type: ignore[misc]


def test_summary_rejects_invalid_project_id():
    with pytest.raises(ValueError, match="invalid_project_id"):
        _summary(project_id="bad-id")


# ---------------------------------------------------------------------------
# TaskHistory construction
# ---------------------------------------------------------------------------


def test_task_history_default_maxlen():
    h = TaskHistory()
    assert h.maxlen == 50


def test_task_history_custom_maxlen():
    h = TaskHistory(maxlen=10)
    assert h.maxlen == 10


def test_task_history_state_db_defaults_to_none():
    h = TaskHistory()
    assert h.state_db is None


def test_task_history_accepts_state_db(tmp_path):
    db = StateDB(tmp_path / "state.db")
    h = TaskHistory(state_db=db)
    assert h.state_db is db


@pytest.mark.parametrize("bad", [0, -1, True, "5", 1.5])
def test_task_history_rejects_invalid_maxlen(bad):
    with pytest.raises(ValueError, match="invalid_maxlen"):
        TaskHistory(maxlen=bad)  # type: ignore[arg-type]


def test_task_history_rejects_non_state_db():
    with pytest.raises(ValueError, match="state_db_must_be_state_db_or_none"):
        TaskHistory(state_db="bad")  # type: ignore[arg-type]


def test_task_history_starts_empty():
    h = TaskHistory()
    assert len(h) == 0


def test_task_history_loads_recent_entries_from_state_db(tmp_path):
    db = StateDB(tmp_path / "state.db")
    db.record_task(_summary(task_id="task-1", finished_at=1.0))
    db.record_task(_summary(task_id="task-2", finished_at=2.0))

    h = TaskHistory(state_db=db)

    assert len(h) == 2
    assert h.get("task-1") is not None
    assert [item.task_id for item in h.recent(5)] == ["task-1", "task-2"]


# ---------------------------------------------------------------------------
# record / get
# ---------------------------------------------------------------------------


def test_record_and_get_happy_path():
    h = TaskHistory()
    s = _summary(task_id="task-abc")
    h.record(s)
    assert h.get("task-abc") is s


def test_get_returns_none_for_unknown():
    h = TaskHistory()
    assert h.get("not-there") is None


def test_record_updates_len():
    h = TaskHistory()
    h.record(_summary(task_id="task-1"))
    h.record(_summary(task_id="task-2"))
    assert len(h) == 2


def test_record_replaces_same_task_id():
    h = TaskHistory()
    old = _summary(task_id="task-x", final_state="FAIL")
    new = _summary(task_id="task-x", final_state="SUCCESS")
    h.record(old)
    h.record(new)
    result = h.get("task-x")
    assert result is new
    assert result.final_state == "SUCCESS"


def test_record_rejects_non_summary():
    h = TaskHistory()
    with pytest.raises(ValueError, match="invalid_summary_type"):
        h.record("not a summary")  # type: ignore[arg-type]


def test_record_persists_to_state_db(tmp_path):
    db = StateDB(tmp_path / "state.db")
    h = TaskHistory(state_db=db)
    summary = _summary(task_id="task-db", project_id="alpha_project")

    h.record(summary)

    assert db.get_task("task-db") == summary


# ---------------------------------------------------------------------------
# eviction (maxlen overflow)
# ---------------------------------------------------------------------------


def test_oldest_entry_evicted_when_full():
    h = TaskHistory(maxlen=3)
    for i in range(4):
        h.record(_summary(task_id=f"task-{i}"))
    assert len(h) == 3
    assert h.get("task-0") is None   # evicted
    assert h.get("task-1") is not None
    assert h.get("task-3") is not None


def test_dict_stays_in_sync_after_eviction():
    h = TaskHistory(maxlen=2)
    s0 = _summary(task_id="t-0")
    s1 = _summary(task_id="t-1")
    s2 = _summary(task_id="t-2")
    h.record(s0)
    h.record(s1)
    h.record(s2)  # evicts s0
    assert h.get("t-0") is None
    assert h.get("t-1") is s1
    assert h.get("t-2") is s2


def test_newer_record_same_id_not_evicted_from_dict():
    """If a task_id is re-recorded, the dict must not lose the new entry
    when the old deque position is evicted."""
    h = TaskHistory(maxlen=2)
    old = _summary(task_id="t-0", final_state="FAIL")
    h.record(old)                           # deque: [t-0]
    other = _summary(task_id="t-1")
    h.record(other)                          # deque: [t-0, t-1]
    new = _summary(task_id="t-0", final_state="SUCCESS")
    h.record(new)                            # deque: [t-1, t-0*] — t-0 (old) evicted
    # The dict must still have the *new* t-0 entry.
    result = h.get("t-0")
    assert result is new
    assert result.final_state == "SUCCESS"


def test_state_db_history_trims_to_maxlen(tmp_path):
    db = StateDB(tmp_path / "state.db")
    h = TaskHistory(maxlen=2, state_db=db)

    h.record(_summary(task_id="task-0", finished_at=1.0))
    h.record(_summary(task_id="task-1", finished_at=2.0))
    h.record(_summary(task_id="task-2", finished_at=3.0))

    assert [item.task_id for item in db.recent_tasks(10)] == ["task-1", "task-2"]
    assert db.get_task("task-0") is None


def test_state_db_restart_round_trip_preserves_newest_duplicate(tmp_path):
    db = StateDB(tmp_path / "state.db")
    h = TaskHistory(maxlen=3, state_db=db)
    h.record(_summary(task_id="task-0", final_state="FAIL", finished_at=1.0))
    h.record(_summary(task_id="task-1", finished_at=2.0))
    h.record(_summary(task_id="task-0", final_state="SUCCESS", finished_at=3.0))

    restarted = TaskHistory(maxlen=3, state_db=db)

    result = restarted.get("task-0")
    assert result is not None
    assert result.final_state == "SUCCESS"
    assert [item.task_id for item in restarted.recent(5)] == [
        "task-0",
        "task-1",
        "task-0",
    ]


# ---------------------------------------------------------------------------
# recent()
# ---------------------------------------------------------------------------


def test_recent_returns_last_n():
    h = TaskHistory()
    for i in range(5):
        h.record(_summary(task_id=f"task-{i}"))
    r = h.recent(3)
    assert len(r) == 3
    assert r[-1].task_id == "task-4"
    assert r[0].task_id == "task-2"


def test_recent_returns_all_when_fewer_than_n():
    h = TaskHistory()
    h.record(_summary(task_id="task-1"))
    h.record(_summary(task_id="task-2"))
    r = h.recent(10)
    assert len(r) == 2


def test_recent_returns_empty_list_when_history_empty():
    h = TaskHistory()
    assert h.recent(5) == []


@pytest.mark.parametrize("bad", [0, -1, True, "3"])
def test_recent_rejects_invalid_n(bad):
    h = TaskHistory()
    with pytest.raises(ValueError, match="invalid_n"):
        h.recent(bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# thread safety (smoke)
# ---------------------------------------------------------------------------


def test_concurrent_record_and_get_do_not_crash():
    """10 threads writing, 10 threads reading — no crash, no data race."""
    h = TaskHistory(maxlen=20)
    errors: list[Exception] = []

    def writer(n: int) -> None:
        try:
            for i in range(10):
                h.record(_summary(task_id=f"task-{n}-{i}"))
                time.sleep(0)
        except Exception as exc:
            errors.append(exc)

    def reader() -> None:
        try:
            for _ in range(20):
                h.recent(5)
                h.get("task-0-0")
                time.sleep(0)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    threads += [threading.Thread(target=reader) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not errors, f"thread errors: {errors}"
