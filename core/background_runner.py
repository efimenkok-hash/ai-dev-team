"""
core/background_runner.py

Step 14b-2 part 2: runs Orchestrator.run() in a background thread so the
Telegram bridge stays responsive. Enforces the spec's "one task at a time"
rule with a thread-safe lock.

A typical flow:
    runner = BackgroundTaskRunner()
    handle = runner.submit(
        task_id="t-42",
        raw_task="напиши парсер RSS",
        run_fn=lambda: orchestrator.run("t-42", "напиши парсер RSS"),
        on_complete=lambda h, result, error: ...,
    )
    # Bridge immediately tells the user "принял задачу"
    # Background thread runs orchestrator; on_complete fires when done.

CONTRACTS:
1. TaskHandle is frozen, snapshottable.
2. submit() raises RunnerBusyError if a task is already in flight.
3. After submit(), the runner is busy until on_complete fires (success or
   failure). Even if on_complete itself raises, the runner releases the
   slot — never gets stuck busy.
4. cancel() sets a thread-safe flag the run_fn can poll via the
   `cancellation_token` argument. Runner does NOT preempt — graceful
   cooperative cancellation only.
5. shutdown() waits for the active task (if any) and shuts down the
   executor. Idempotent.
6. Single executor, single worker — no parallel tasks per the 14b-MVP spec.
"""

import contextlib
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass


class RunnerBusyError(RuntimeError):
    """Raised when submit() is called while another task is already running."""

    def __init__(self, current_handle: "TaskHandle") -> None:
        super().__init__(f"runner_busy:active_task={current_handle.task_id}")
        self.current_handle = current_handle


class CancellationToken:
    """Cooperative cancellation flag. The run_fn can poll .is_set() between
    agent calls (or pass it down to wrappers) to abort gracefully.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._set = False

    def request(self) -> None:
        with self._lock:
            self._set = True

    def is_set(self) -> bool:
        with self._lock:
            return self._set


@dataclass(frozen=True)
class TaskHandle:
    task_id: str
    raw_task: str
    started_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise ValueError("empty_task_id")
        if not isinstance(self.raw_task, str) or not self.raw_task.strip():
            raise ValueError("empty_raw_task")
        if (
            isinstance(self.started_at, bool)
            or not isinstance(self.started_at, (int, float))
            or self.started_at <= 0
        ):
            raise ValueError(f"invalid_started_at:{self.started_at!r}")


# Type aliases for clarity.
RunFn = Callable[[CancellationToken], object]
CompletionCallback = Callable[[TaskHandle, object, BaseException | None], None]


class BackgroundTaskRunner:
    """Single-task background executor.

    The executor uses one worker thread; submit() rejects new tasks while
    the previous one is in flight (per spec: queue / parallel deferred to
    later phases).
    """

    def __init__(self, *, thread_name_prefix: str = "aidt-runner") -> None:
        if not isinstance(thread_name_prefix, str) or not thread_name_prefix.strip():
            raise ValueError("empty_thread_name_prefix")
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=thread_name_prefix.strip(),
        )
        self._lock = threading.Lock()
        self._active_handle: TaskHandle | None = None
        self._active_token: CancellationToken | None = None
        self._active_future: Future | None = None
        self._shutdown = False

    def is_busy(self) -> bool:
        with self._lock:
            return self._active_handle is not None

    def active_handle(self) -> TaskHandle | None:
        with self._lock:
            return self._active_handle

    def submit(
        self,
        *,
        raw_task: str,
        run_fn: RunFn,
        on_complete: CompletionCallback,
        task_id: str | None = None,
    ) -> TaskHandle:
        """Schedule run_fn on the background thread. Returns immediately.

        Raises:
          RunnerBusyError — if another task is in flight.
          ValueError — for invalid args.
          RuntimeError — if the runner has been shut down.
        """
        if not isinstance(raw_task, str) or not raw_task.strip():
            raise ValueError("empty_raw_task")
        if not callable(run_fn):
            raise ValueError(f"run_fn_not_callable:{type(run_fn).__name__}")
        if not callable(on_complete):
            raise ValueError(
                f"on_complete_not_callable:{type(on_complete).__name__}"
            )

        with self._lock:
            if self._shutdown:
                raise RuntimeError("runner_shutdown")
            if self._active_handle is not None:
                raise RunnerBusyError(self._active_handle)
            tid = task_id or f"task-{uuid.uuid4().hex[:12]}"
            if not isinstance(tid, str) or not tid.strip():
                raise ValueError("empty_task_id")
            handle = TaskHandle(
                task_id=tid,
                raw_task=raw_task,
                started_at=time.time(),
            )
            token = CancellationToken()
            self._active_handle = handle
            self._active_token = token

        future = self._executor.submit(
            self._run_wrapped, handle, token, run_fn, on_complete,
        )
        with self._lock:
            self._active_future = future
        return handle

    def _run_wrapped(
        self,
        handle: TaskHandle,
        token: CancellationToken,
        run_fn: RunFn,
        on_complete: CompletionCallback,
    ) -> None:
        result: object = None
        error: BaseException | None = None
        try:
            result = run_fn(token)
        except BaseException as exc:
            error = exc
        finally:
            # Always release the busy slot BEFORE calling on_complete so
            # the bridge can submit follow-up tasks even if on_complete
            # itself takes time.
            with self._lock:
                self._active_handle = None
                self._active_token = None
                self._active_future = None
            # Last-resort: don't let completion-callback failures break
            # the worker thread (it must remain alive for next task).
            with contextlib.suppress(Exception):
                on_complete(handle, result, error)

    def cancel(self) -> bool:
        """Requests cooperative cancellation of the active task.

        Returns True if a task was active (the flag was set); False if no
        task is running. Does NOT wait for cancellation to take effect —
        the run_fn is responsible for polling the token.
        """
        with self._lock:
            if self._active_token is None:
                return False
            self._active_token.request()
            return True

    def shutdown(self, wait: bool = True) -> None:
        """Shut down the executor. Idempotent.

        If `wait=True`, blocks until the active task (if any) finishes.
        Subsequent submit() calls raise RuntimeError.
        """
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        self._executor.shutdown(wait=wait)
