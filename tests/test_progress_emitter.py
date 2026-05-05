"""Tests for core.progress_emitter (Step 14b-2: streaming progress events)."""

import time

import pytest

from core.model_tier import REQUIRED_ROLES
from core.progress_emitter import (
    EVENT_KINDS,
    ProgressEmitter,
    ProgressEvent,
    wrap_agent_with_progress,
    wrap_registry_with_progress,
)

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------


def test_event_kinds_complete():
    expected = {
        "task_started", "agent_started", "agent_finished", "agent_failed",
        "fsm_transition", "task_completed", "task_failed",
    }
    assert EVENT_KINDS == expected


# ---------------------------------------------------------------------------
# ProgressEvent
# ---------------------------------------------------------------------------


def test_event_happy_path():
    e = ProgressEvent(
        kind="agent_started",
        timestamp=time.time(),
        agent_role="architect_agent",
    )
    assert e.kind == "agent_started"
    assert e.agent_role == "architect_agent"


def test_event_is_frozen():
    e = ProgressEvent(kind="task_started", timestamp=1.0)
    with pytest.raises(Exception):
        e.kind = "task_completed"  # type: ignore[misc]


def test_event_rejects_unknown_kind():
    with pytest.raises(ValueError, match="invalid_event_kind"):
        ProgressEvent(kind="unknown_kind", timestamp=1.0)


def test_event_rejects_negative_timestamp():
    with pytest.raises(ValueError, match="invalid_timestamp"):
        ProgressEvent(kind="task_started", timestamp=-1.0)


def test_event_rejects_bool_timestamp():
    with pytest.raises(ValueError, match="invalid_timestamp"):
        ProgressEvent(kind="task_started", timestamp=True)  # type: ignore[arg-type]


def test_event_rejects_unknown_agent_role():
    with pytest.raises(ValueError, match="unknown_agent_role"):
        ProgressEvent(
            kind="agent_started",
            timestamp=1.0,
            agent_role="ceo_agent",
        )


def test_event_accepts_none_agent_role():
    e = ProgressEvent(kind="task_started", timestamp=1.0, agent_role=None)
    assert e.agent_role is None


def test_event_rejects_negative_duration():
    with pytest.raises(ValueError, match="invalid_duration_ms"):
        ProgressEvent(
            kind="agent_finished",
            timestamp=1.0,
            agent_role="architect_agent",
            duration_ms=-1,
        )


def test_event_accepts_zero_duration():
    e = ProgressEvent(
        kind="agent_finished",
        timestamp=1.0,
        agent_role="architect_agent",
        duration_ms=0,
    )
    assert e.duration_ms == 0


def test_event_rejects_non_string_detail():
    with pytest.raises(ValueError, match="non_string_detail"):
        ProgressEvent(kind="task_started", timestamp=1.0, detail=42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ProgressEmitter
# ---------------------------------------------------------------------------


def test_emitter_construction_rejects_non_callable():
    with pytest.raises(ValueError, match="callback_not_callable"):
        ProgressEmitter("not callable")  # type: ignore[arg-type]


def test_emit_passes_event_to_callback():
    captured: list[ProgressEvent] = []
    emitter = ProgressEmitter(captured.append)
    e = ProgressEvent(kind="task_started", timestamp=1.0)
    emitter.emit(e)
    assert captured == [e]


def test_emit_swallows_callback_exceptions():
    """Callback failures must never propagate — UI can break, pipeline cannot."""
    def bad_callback(_e):
        raise RuntimeError("UI exploded")

    emitter = ProgressEmitter(bad_callback)
    # Should not raise:
    emitter.emit(ProgressEvent(kind="task_started", timestamp=1.0))


def test_emit_ignores_non_event_input():
    captured: list = []
    emitter = ProgressEmitter(captured.append)
    emitter.emit("not an event")  # type: ignore[arg-type]
    emitter.emit(None)  # type: ignore[arg-type]
    emitter.emit(42)  # type: ignore[arg-type]
    assert captured == []


def test_emit_task_started():
    captured: list[ProgressEvent] = []
    emitter = ProgressEmitter(captured.append)
    emitter.emit_task_started(detail="building")
    assert len(captured) == 1
    assert captured[0].kind == "task_started"
    assert captured[0].detail == "building"


def test_emit_agent_started():
    captured: list[ProgressEvent] = []
    emitter = ProgressEmitter(captured.append)
    emitter.emit_agent_started("architect_agent")
    assert captured[0].kind == "agent_started"
    assert captured[0].agent_role == "architect_agent"


def test_emit_agent_finished_records_duration():
    captured: list[ProgressEvent] = []
    emitter = ProgressEmitter(captured.append)
    emitter.emit_agent_finished("writer_agent", 1234)
    assert captured[0].duration_ms == 1234


def test_emit_agent_failed_truncates_long_error():
    captured: list[ProgressEvent] = []
    emitter = ProgressEmitter(captured.append)
    long_error = "X" * 1000
    emitter.emit_agent_failed("writer_agent", 100, long_error)
    assert len(captured[0].detail) <= 300


def test_emit_fsm_transition_formats_states():
    captured: list[ProgressEvent] = []
    emitter = ProgressEmitter(captured.append)
    emitter.emit_fsm_transition("PLANNING", "PM")
    assert "PLANNING->PM" in captured[0].detail


def test_emit_task_completed():
    captured: list[ProgressEvent] = []
    emitter = ProgressEmitter(captured.append)
    emitter.emit_task_completed("ok")
    assert captured[0].kind == "task_completed"


def test_emit_task_failed():
    captured: list[ProgressEvent] = []
    emitter = ProgressEmitter(captured.append)
    emitter.emit_task_failed("budget_exceeded")
    assert captured[0].kind == "task_failed"
    assert "budget" in captured[0].detail


# ---------------------------------------------------------------------------
# wrap_agent_with_progress
# ---------------------------------------------------------------------------


def test_wrap_emits_started_then_finished_on_success():
    captured: list[ProgressEvent] = []
    emitter = ProgressEmitter(captured.append)

    def real_agent(arg):
        return f"hello {arg}"

    wrapped = wrap_agent_with_progress("architect_agent", real_agent, emitter)
    result = wrapped("world")
    assert result == "hello world"
    kinds = [e.kind for e in captured]
    assert kinds == ["agent_started", "agent_finished"]


def test_wrap_emits_started_then_failed_on_exception():
    captured: list[ProgressEvent] = []
    emitter = ProgressEmitter(captured.append)

    def bad_agent():
        raise RuntimeError("kaboom")

    wrapped = wrap_agent_with_progress("writer_agent", bad_agent, emitter)
    with pytest.raises(RuntimeError, match="kaboom"):
        wrapped()
    kinds = [e.kind for e in captured]
    assert kinds == ["agent_started", "agent_failed"]
    assert "RuntimeError" in captured[1].detail
    assert "kaboom" in captured[1].detail


def test_wrap_preserves_return_value():
    emitter = ProgressEmitter(lambda _e: None)
    wrapped = wrap_agent_with_progress(
        "writer_agent",
        lambda: "expected",
        emitter,
    )
    assert wrapped() == "expected"


def test_wrap_passes_through_args_and_kwargs():
    captured_args = []
    emitter = ProgressEmitter(lambda _e: None)

    def agent(a, b, c=None):
        captured_args.append((a, b, c))
        return "ok"

    wrapped = wrap_agent_with_progress("writer_agent", agent, emitter)
    wrapped(1, 2, c=3)
    assert captured_args == [(1, 2, 3)]


def test_wrap_rejects_unknown_role():
    emitter = ProgressEmitter(lambda _e: None)
    with pytest.raises(ValueError, match="invalid_agent_role"):
        wrap_agent_with_progress("ceo_agent", lambda: "x", emitter)


def test_wrap_rejects_non_callable_fn():
    emitter = ProgressEmitter(lambda _e: None)
    with pytest.raises(ValueError, match="agent_not_callable"):
        wrap_agent_with_progress("writer_agent", "not callable", emitter)  # type: ignore[arg-type]


def test_wrap_rejects_non_emitter():
    with pytest.raises(ValueError, match="invalid_emitter"):
        wrap_agent_with_progress("writer_agent", lambda: "x", "not emitter")  # type: ignore[arg-type]


def test_wrap_records_non_negative_duration():
    captured: list[ProgressEvent] = []
    emitter = ProgressEmitter(captured.append)

    def quick_agent():
        return "fast"

    wrapped = wrap_agent_with_progress("writer_agent", quick_agent, emitter)
    wrapped()
    finished = next(e for e in captured if e.kind == "agent_finished")
    assert finished.duration_ms is not None
    assert finished.duration_ms >= 0


# ---------------------------------------------------------------------------
# wrap_registry_with_progress
# ---------------------------------------------------------------------------


def test_wrap_registry_wraps_all_required_agents():
    emitter = ProgressEmitter(lambda _e: None)
    # Use default args to bind `role` per-iteration (avoids B023).
    registry = {
        role: (lambda *_a, _role=role, **_k: f"out:{_role}")
        for role in REQUIRED_ROLES
    }
    wrapped = wrap_registry_with_progress(registry, emitter)
    assert set(wrapped.keys()) == REQUIRED_ROLES
    for role in REQUIRED_ROLES:
        # Wrapped function is different from original
        assert wrapped[role] is not registry[role]


def test_wrap_registry_returns_new_dict_does_not_mutate():
    emitter = ProgressEmitter(lambda _e: None)
    registry = {role: lambda *a, **k: "x" for role in REQUIRED_ROLES}
    snapshot = dict(registry)
    wrap_registry_with_progress(registry, emitter)
    assert registry == snapshot  # original unchanged


def test_wrap_registry_preserves_unknown_roles_unwrapped():
    emitter = ProgressEmitter(lambda _e: None)
    registry: dict = {
        role: lambda *a, **k: "x" for role in REQUIRED_ROLES
    }
    extra = lambda *a, **k: "extra"  # noqa: E731
    registry["custom_role"] = extra
    wrapped = wrap_registry_with_progress(registry, emitter)
    assert wrapped["custom_role"] is extra  # not wrapped


def test_wrap_registry_emits_events_on_call():
    captured: list[ProgressEvent] = []
    emitter = ProgressEmitter(captured.append)
    registry = {role: (lambda *a, **k: "ok") for role in REQUIRED_ROLES}
    wrapped = wrap_registry_with_progress(registry, emitter)
    wrapped["architect_agent"]("input")
    kinds = [e.kind for e in captured]
    assert kinds == ["agent_started", "agent_finished"]


def test_wrap_registry_rejects_non_mapping():
    emitter = ProgressEmitter(lambda _e: None)
    with pytest.raises(ValueError, match="registry_not_mapping"):
        wrap_registry_with_progress(["not a mapping"], emitter)  # type: ignore[arg-type]


def test_wrap_registry_rejects_non_emitter():
    registry = {role: (lambda *a, **k: "x") for role in REQUIRED_ROLES}
    with pytest.raises(ValueError, match="invalid_emitter"):
        wrap_registry_with_progress(registry, "not emitter")  # type: ignore[arg-type]
