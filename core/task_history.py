"""
core/task_history.py

Step 14c-1: thread-safe ring buffer of completed task summaries.

TaskHistory is shared between:
  - make_real_task_handler (writes one record per on_complete)
  - make_push_handler       (reads by task_id to find the branch to push)
  - make_log_handler         (reads recent N records for display — 14c-3)
  - StateDB-backed restarts  (optional persistence across bot restarts)

CONTRACTS:
1. TaskSummary is frozen; all string fields are non-empty on construction
   except commit_sha / failure_reason which may be None.
2. TaskHistory is thread-safe: record() and get() are guarded by a lock.
   They may be called concurrently from the BackgroundTaskRunner worker
   thread and the PTB async dispatch thread.
3. When maxlen is exceeded the oldest record is evicted (deque semantics).
   The by-id lookup dict is kept in sync with the deque on every eviction.
4. maxlen must be a positive int; __init__ raises ValueError otherwise.
5. record() replaces an existing entry for the same task_id in the dict
   (newer record wins), but the deque entry is appended — the old entry
   may still lurk in the deque until it is naturally evicted.  get()
   always returns the most recent record via the dict, so the dict is the
   source of truth.
6. When state_db is provided, every record() is mirrored into SQLite and
   the persisted history is trimmed to maxlen so restart semantics match
   the in-memory ring buffer.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.state_db import StateDB

_DEFAULT_MAXLEN = 50
_PROJECT_ID_MAX_LEN = 64


@dataclass(frozen=True)
class TaskSummary:
    """Immutable record of a completed (or failed) pipeline task.

    Fields:
        task_id:        unique task identifier (matches sandbox branch).
        branch:         git branch name (e.g. 'feature/task-abc-001').
        commit_sha:     full SHA if pipeline reached SUCCESS, else None.
        final_state:    'SUCCESS', 'FAIL', 'BLOCKED', etc.
        failure_reason: human-readable reason string or None on SUCCESS.
        project_id:     logical project identifier or None for legacy records
                        created before project-aware history persistence.
        tier_name:      tier used (e.g. 'ECONOMY', 'PREMIUM').
        finished_at:    time.time() at on_complete invocation.
    """

    task_id: str
    branch: str
    commit_sha: str | None
    final_state: str
    failure_reason: str | None
    tier_name: str
    finished_at: float
    project_id: str | None = None

    def __post_init__(self) -> None:
        for field, val in (
            ("task_id", self.task_id),
            ("branch", self.branch),
            ("final_state", self.final_state),
            ("tier_name", self.tier_name),
        ):
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"empty_{field}")
        if self.commit_sha is not None and not isinstance(self.commit_sha, str):
            raise ValueError("commit_sha_must_be_str_or_none")
        if self.failure_reason is not None and not isinstance(self.failure_reason, str):
            raise ValueError("failure_reason_must_be_str_or_none")
        if self.project_id is not None:
            if not isinstance(self.project_id, str) or not self.project_id.strip():
                raise ValueError("project_id_must_be_str_or_none")
            normalized_project_id = self.project_id.strip().lower()
            if not normalized_project_id.isascii():
                raise ValueError(f"non_ascii_project_id:{normalized_project_id}")
            if (
                len(normalized_project_id) > _PROJECT_ID_MAX_LEN
                or not normalized_project_id[0].isalpha()
                or any(
                    not (char.islower() or char.isdigit() or char == "_")
                    for char in normalized_project_id
                )
            ):
                raise ValueError(f"invalid_project_id:{normalized_project_id}")
            object.__setattr__(self, "project_id", normalized_project_id)
        if (
            isinstance(self.finished_at, bool)
            or not isinstance(self.finished_at, (int, float))
            or self.finished_at <= 0
        ):
            raise ValueError(f"invalid_finished_at:{self.finished_at!r}")


class TaskHistory:
    """Thread-safe ring buffer of TaskSummary records.

    Usage::

        history = TaskHistory()
        history.record(TaskSummary(...))
        summary = history.get("task-abc-001")   # None if not found
        last_ten = history.recent(10)           # list, newest last
    """

    def __init__(
        self,
        maxlen: int = _DEFAULT_MAXLEN,
        *,
        state_db: StateDB | None = None,
    ) -> None:
        if (
            isinstance(maxlen, bool)
            or not isinstance(maxlen, int)
            or maxlen <= 0
        ):
            raise ValueError(f"invalid_maxlen:{maxlen!r}")
        if state_db is not None:
            from core.state_db import StateDB as _StateDB

            if not isinstance(state_db, _StateDB):
                raise ValueError(
                    f"state_db_must_be_state_db_or_none:{type(state_db).__name__}"
                )
        self._maxlen = maxlen
        self._deque: deque[TaskSummary] = deque(maxlen=maxlen)
        self._by_id: dict[str, TaskSummary] = {}
        self._lock = threading.Lock()
        self._state_db = state_db
        if self._state_db is not None:
            self._load_from_state_db()

    @property
    def maxlen(self) -> int:
        return self._maxlen

    @property
    def state_db(self) -> StateDB | None:
        return self._state_db

    def _load_from_state_db(self) -> None:
        if self._state_db is None:
            return
        persisted = self._state_db.recent_tasks(self._maxlen)
        with self._lock:
            self._deque.clear()
            self._by_id.clear()
            for summary in persisted:
                self._append_locked(summary)

    def _append_locked(self, summary: TaskSummary) -> None:
        if len(self._deque) == self._maxlen:
            evicted = self._deque[0]
            if self._by_id.get(evicted.task_id) is evicted:
                del self._by_id[evicted.task_id]
        self._deque.append(summary)
        self._by_id[summary.task_id] = summary

    def record(self, summary: TaskSummary) -> None:
        """Append summary to history. Thread-safe.

        If maxlen is reached, the oldest entry is evicted from the deque.
        The by-id dict is updated: the evicted entry is removed (unless
        a newer entry for the same task_id already replaced it), and the
        new entry is inserted.
        """
        if not isinstance(summary, TaskSummary):
            raise ValueError(f"invalid_summary_type:{type(summary).__name__}")
        with self._lock:
            if self._state_db is not None:
                self._state_db.record_task(summary)
                self._state_db.trim_task_history(self._maxlen)
            self._append_locked(summary)

    def get(self, task_id: str) -> TaskSummary | None:
        """Return the most recent summary for task_id, or None. Thread-safe."""
        with self._lock:
            return self._by_id.get(task_id)

    def recent(self, n: int = 10) -> list[TaskSummary]:
        """Return the last `n` summaries in insertion order (newest last).

        Returns fewer than n if the history has fewer entries.
        Thread-safe.
        """
        if (
            isinstance(n, bool)
            or not isinstance(n, int)
            or n <= 0
        ):
            raise ValueError(f"invalid_n:{n!r}")
        with self._lock:
            items = list(self._deque)
        return items[-n:] if n < len(items) else items

    def __len__(self) -> int:
        with self._lock:
            return len(self._deque)
