"""Tests for core.background_runner (Step 14b-2: parallel pipeline execution)."""

import threading
import time

import pytest

from core.background_runner import (
    BackgroundTaskRunner,
    CancellationToken,
    RunnerBusyError,
    TaskHandle,
)

# ---------------------------------------------------------------------------
# CancellationToken
# ---------------------------------------------------------------------------


def test_token_starts_unset():
    t = CancellationToken()
    assert t.is_set() is False


def test_token_becomes_set_after_request():
    t = CancellationToken()
    t.request()
    assert t.is_set() is True


def test_token_request_is_idempotent():
    t = CancellationToken()
    t.request()
    t.request()
    assert t.is_set() is True


def test_token_thread_safe_concurrent_requests():
    t = CancellationToken()
    threads = [threading.Thread(target=t.request) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert t.is_set() is True


# ---------------------------------------------------------------------------
# TaskHandle
# ---------------------------------------------------------------------------


def test_handle_happy_path():
    h = TaskHandle(task_id="t-1", raw_task="hello", started_at=time.time())
    assert h.task_id == "t-1"


def test_handle_is_frozen():
    h = TaskHandle(task_id="t-1", raw_task="hi", started_at=time.time())
    with pytest.raises(Exception):
        h.task_id = "t-2"  # type: ignore[misc]


@pytest.mark.parametrize("bad", ["", "  "])
def test_handle_rejects_empty_task_id(bad):
    with pytest.raises(ValueError, match="empty_task_id"):
        TaskHandle(task_id=bad, raw_task="hi", started_at=time.time())


def test_handle_rejects_empty_raw_task():
    with pytest.raises(ValueError, match="empty_raw_task"):
        TaskHandle(task_id="t", raw_task="", started_at=time.time())


@pytest.mark.parametrize("bad", [0, -1, True])
def test_handle_rejects_invalid_started_at(bad):
    with pytest.raises(ValueError, match="invalid_started_at"):
        TaskHandle(task_id="t", raw_task="hi", started_at=bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# BackgroundTaskRunner — basic submit / completion
# ---------------------------------------------------------------------------


def test_runner_starts_idle():
    runner = BackgroundTaskRunner()
    try:
        assert runner.is_busy() is False
        assert runner.active_handle() is None
    finally:
        runner.shutdown()


def test_submit_returns_handle_immediately():
    runner = BackgroundTaskRunner()
    try:
        completed = threading.Event()

        def run_fn(_token):
            time.sleep(0.05)  # short blocking work
            return "done"

        def on_complete(handle, result, error):
            assert handle.task_id.startswith("task-")
            assert result == "done"
            assert error is None
            completed.set()

        handle = runner.submit(
            raw_task="hello",
            run_fn=run_fn,
            on_complete=on_complete,
        )
        assert isinstance(handle, TaskHandle)
        assert handle.raw_task == "hello"
        assert runner.is_busy() is True

        assert completed.wait(timeout=5)
        # After completion, runner is no longer busy
        time.sleep(0.05)
        assert runner.is_busy() is False
    finally:
        runner.shutdown()


def test_submit_with_explicit_task_id():
    runner = BackgroundTaskRunner()
    try:
        completed = threading.Event()
        captured = []

        def on_complete(handle, *_):
            captured.append(handle.task_id)
            completed.set()

        runner.submit(
            task_id="my-task-42",
            raw_task="hi",
            run_fn=lambda _t: "ok",
            on_complete=on_complete,
        )
        assert completed.wait(timeout=5)
        assert captured == ["my-task-42"]
    finally:
        runner.shutdown()


def test_submit_rejects_when_busy():
    runner = BackgroundTaskRunner()
    try:
        block = threading.Event()
        completed = threading.Event()

        def slow_run(_token):
            block.wait(timeout=5)
            return "done"

        runner.submit(
            raw_task="first",
            run_fn=slow_run,
            on_complete=lambda *_: completed.set(),
        )

        # Second submit while first is still running
        with pytest.raises(RunnerBusyError) as exc_info:
            runner.submit(
                raw_task="second",
                run_fn=lambda _t: "x",
                on_complete=lambda *_: None,
            )
        assert exc_info.value.current_handle.raw_task == "first"

        # Unblock first task
        block.set()
        assert completed.wait(timeout=5)
    finally:
        runner.shutdown()


def test_submit_after_completion_works():
    runner = BackgroundTaskRunner()
    try:
        first_done = threading.Event()
        second_done = threading.Event()

        runner.submit(
            raw_task="one",
            run_fn=lambda _t: "1",
            on_complete=lambda *_: first_done.set(),
        )
        assert first_done.wait(timeout=5)
        time.sleep(0.05)  # let runner release slot

        runner.submit(
            raw_task="two",
            run_fn=lambda _t: "2",
            on_complete=lambda *_: second_done.set(),
        )
        assert second_done.wait(timeout=5)
    finally:
        runner.shutdown()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_run_fn_exception_passed_to_on_complete():
    runner = BackgroundTaskRunner()
    try:
        completed = threading.Event()
        captured: list = []

        def bad_run(_token):
            raise ValueError("kaboom")

        def on_complete(handle, result, error):
            captured.append((handle, result, error))
            completed.set()

        runner.submit(
            raw_task="failing",
            run_fn=bad_run,
            on_complete=on_complete,
        )
        assert completed.wait(timeout=5)
        _h, r, err = captured[0]
        assert r is None
        assert isinstance(err, ValueError)
        assert "kaboom" in str(err)
    finally:
        runner.shutdown()


def test_runner_recovers_after_run_fn_failure():
    """After a failed task, the runner should accept a new submission."""
    runner = BackgroundTaskRunner()
    try:
        first_done = threading.Event()
        second_done = threading.Event()

        runner.submit(
            raw_task="bad",
            run_fn=lambda _t: (_ for _ in ()).throw(RuntimeError("fail")),
            on_complete=lambda *_: first_done.set(),
        )
        assert first_done.wait(timeout=5)
        time.sleep(0.05)

        runner.submit(
            raw_task="good",
            run_fn=lambda _t: "ok",
            on_complete=lambda *_: second_done.set(),
        )
        assert second_done.wait(timeout=5)
    finally:
        runner.shutdown()


def test_on_complete_failure_does_not_break_runner():
    """If on_complete raises, runner must still be ready for next task."""
    runner = BackgroundTaskRunner()
    try:
        def boom_complete(*_):
            raise RuntimeError("on_complete broken")

        runner.submit(
            raw_task="task1",
            run_fn=lambda _t: "ok",
            on_complete=boom_complete,
        )
        # Wait for the worker to process; we cannot use Event because
        # on_complete itself fails. Poll busy flag.
        deadline = time.time() + 5
        while runner.is_busy() and time.time() < deadline:
            time.sleep(0.05)
        assert runner.is_busy() is False

        # Runner must accept new task
        done = threading.Event()
        runner.submit(
            raw_task="task2",
            run_fn=lambda _t: "ok",
            on_complete=lambda *_: done.set(),
        )
        assert done.wait(timeout=5)
    finally:
        runner.shutdown()


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_cancel_returns_false_when_idle():
    runner = BackgroundTaskRunner()
    try:
        assert runner.cancel() is False
    finally:
        runner.shutdown()


def test_cancel_sets_token_for_active_task():
    runner = BackgroundTaskRunner()
    try:
        token_seen: list[CancellationToken] = []
        block = threading.Event()
        completed = threading.Event()

        def cooperative(token):
            token_seen.append(token)
            for _ in range(50):
                if token.is_set():
                    return "cancelled"
                time.sleep(0.01)
            return "finished"

        runner.submit(
            raw_task="cancelable",
            run_fn=cooperative,
            on_complete=lambda h, r, e: (block.set(), completed.set()),
        )
        # Give the worker time to register the token
        time.sleep(0.05)
        assert runner.cancel() is True
        assert completed.wait(timeout=5)
        assert token_seen and token_seen[0].is_set()
    finally:
        runner.shutdown()


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


def test_construction_rejects_empty_thread_name_prefix():
    with pytest.raises(ValueError, match="empty_thread_name_prefix"):
        BackgroundTaskRunner(thread_name_prefix="")


def test_submit_rejects_empty_raw_task():
    runner = BackgroundTaskRunner()
    try:
        with pytest.raises(ValueError, match="empty_raw_task"):
            runner.submit(
                raw_task="  ",
                run_fn=lambda _t: "x",
                on_complete=lambda *_: None,
            )
    finally:
        runner.shutdown()


def test_submit_rejects_non_callable_run_fn():
    runner = BackgroundTaskRunner()
    try:
        with pytest.raises(ValueError, match="run_fn_not_callable"):
            runner.submit(
                raw_task="hi",
                run_fn="not callable",  # type: ignore[arg-type]
                on_complete=lambda *_: None,
            )
    finally:
        runner.shutdown()


def test_submit_rejects_non_callable_on_complete():
    runner = BackgroundTaskRunner()
    try:
        with pytest.raises(ValueError, match="on_complete_not_callable"):
            runner.submit(
                raw_task="hi",
                run_fn=lambda _t: "x",
                on_complete="not callable",  # type: ignore[arg-type]
            )
    finally:
        runner.shutdown()


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def test_shutdown_is_idempotent():
    runner = BackgroundTaskRunner()
    runner.shutdown()
    runner.shutdown()  # should not raise


def test_submit_after_shutdown_raises():
    runner = BackgroundTaskRunner()
    runner.shutdown()
    with pytest.raises(RuntimeError, match="runner_shutdown"):
        runner.submit(
            raw_task="hi",
            run_fn=lambda _t: "x",
            on_complete=lambda *_: None,
        )


def test_shutdown_waits_for_active_task():
    runner = BackgroundTaskRunner()
    completed = threading.Event()

    def slow(_token):
        time.sleep(0.1)
        completed.set()
        return "done"

    runner.submit(
        raw_task="slow",
        run_fn=slow,
        on_complete=lambda *_: None,
    )
    runner.shutdown(wait=True)
    assert completed.is_set()
