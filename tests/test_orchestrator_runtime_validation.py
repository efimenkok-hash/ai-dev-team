"""Tests for Orchestrator <-> runtime_validator integration (Step 15).

These tests cover what happens when the QA agent returns PASS but the
runtime validation hook is supplied:

  - hook=None        -> SUCCESS (backward compat, regression-guarded here)
  - hook ok=True     -> SUCCESS
  - hook ok=False    -> synthesized REJECTED review -> FIX -> qa_fix++ counter
  - hook ok=False repeatedly -> FAIL on qa_fix_loop_exceeded
  - hook raises      -> FAIL with runtime_validator_exception
  - hook returns None -> SUCCESS (treat None as "skip")

We reuse the existing SeqAgent helpers from tests/test_orchestrator.py-style
recipe inline, to keep this test file self-contained.
"""

import json

import pytest

from core.fsm import State
from core.memory import PipelineMemory
from core.orchestrator import Orchestrator
from core.quality_gates import CheckResult
from core.runtime_validator import ValidationReport, ValidationStrategy

# ---------------------------------------------------------------------------
# canned agent payloads (mirrors test_orchestrator.py constants)
# ---------------------------------------------------------------------------

_PLANNING_OK = '{"planning_id":"p1","ready_for_pm":true}'
_PM_OK = '{"plan_id":"x","subtasks":[{"id":"T-001"}]}'
_ARCH_OK = '{"arch_id":"a1","plan_id":"x","modules":[]}'
_WRITER_OK = "FILE: app.py\n---\nprint('hi')\n---"
_REVIEW_APPROVED = (
    '{"review_id":"r1","verdict":"APPROVED","files":[],"for_fixer":[]}'
)
_TEST_OK = "FILE: tests/test_app.py\n---\ndef test_x(): pass\n---"
_QA_PASS = '{"qa_id":"q1","verdict":"PASS","blockers":[],"for_fixer":[]}'
_FIX_OK = "FILE: app.py\n---\nprint('fixed')\n---"


class SeqAgent:
    """Returns a fixed sequence of responses; last value sticks once exhausted."""

    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)
        if not self.responses:
            return ""
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[idx]


def _registry(**overrides):
    base = {
        "planning_agent": SeqAgent(_PLANNING_OK),
        "pm_agent": SeqAgent(_PM_OK),
        "architect_agent": SeqAgent(_ARCH_OK),
        "writer_agent": SeqAgent(_WRITER_OK),
        "reviewer_agent": SeqAgent(_REVIEW_APPROVED),
        "tester_agent": SeqAgent(_TEST_OK),
        "qa_agent": SeqAgent(_QA_PASS),
        "fixer_agent": SeqAgent(_FIX_OK),
    }
    base.update(overrides)
    return base


def _make_report(ok: bool, *check_names: str) -> ValidationReport:
    checks = tuple(
        CheckResult(
            name=name,
            ok=ok,
            summary="ok:test" if ok else f"failed:{name}",
            raw_output="" if ok else f"raw output for {name}",
            duration_ms=1,
        )
        for name in (check_names or ("lint", "tests"))
    )
    return ValidationReport(
        ok=ok,
        strategy=ValidationStrategy.INPLACE,
        checks=checks,
        duration_ms=1,
    )


# ---------------------------------------------------------------------------
# constructor / invariants
# ---------------------------------------------------------------------------


def test_constructor_accepts_runtime_validator():
    Orchestrator(
        memory=PipelineMemory(),
        agents=_registry(),
        runtime_validator=lambda task_id, snap: None,
    )


def test_constructor_rejects_non_callable_runtime_validator():
    with pytest.raises(ValueError, match="invalid_runtime_validator"):
        Orchestrator(
            memory=PipelineMemory(),
            agents=_registry(),
            runtime_validator="not callable",  # type: ignore[arg-type]
        )


def test_constructor_default_runtime_validator_is_none():
    """Backward compatibility: existing callers must not be forced to pass it."""
    orch = Orchestrator(memory=PipelineMemory(), agents=_registry())
    # private attribute check intentional; this is the contract observers care about
    assert orch._runtime_validator is None


# ---------------------------------------------------------------------------
# happy path: hook returns ok=True
# ---------------------------------------------------------------------------


def test_qa_pass_with_ok_runtime_validator_reaches_success():
    invocations: list[tuple] = []

    def hook(task_id, snapshot):
        invocations.append((task_id, snapshot.task_id))
        return _make_report(True)

    orch = Orchestrator(
        memory=PipelineMemory(),
        agents=_registry(),
        runtime_validator=hook,
    )
    result = orch.run("t-ok", "Build a hello-world script")
    assert result.final_state is State.SUCCESS
    assert result.failure_reason is None
    assert invocations == [("t-ok", "t-ok")]


def test_qa_pass_with_no_validator_reaches_success():
    """Sanity: without a hook, behavior is unchanged (regression guard)."""
    orch = Orchestrator(memory=PipelineMemory(), agents=_registry())
    result = orch.run("t-noh", "Build a hello-world script")
    assert result.final_state is State.SUCCESS


def test_qa_pass_with_validator_returning_none_reaches_success():
    orch = Orchestrator(
        memory=PipelineMemory(),
        agents=_registry(),
        runtime_validator=lambda task_id, snapshot: None,
    )
    result = orch.run("t-none", "Build a hello-world script")
    assert result.final_state is State.SUCCESS


# ---------------------------------------------------------------------------
# failure path: hook ok=False -> FIX -> recover -> SUCCESS
# ---------------------------------------------------------------------------


def test_runtime_validator_failure_routes_to_fix_then_recovers():
    """First QA-PASS triggers runtime validation that fails, second QA-PASS
    runs against a passing validator, pipeline reaches SUCCESS.
    """
    call_count = {"n": 0}

    def hook(task_id, snapshot):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _make_report(False, "lint", "tests")
        return _make_report(True)

    orch = Orchestrator(
        memory=PipelineMemory(),
        agents=_registry(),
        runtime_validator=hook,
    )
    result = orch.run("t-recover", "Build something")
    assert result.final_state is State.SUCCESS
    assert call_count["n"] == 2

    # qa_fix loop must have been bumped exactly once via the runtime path
    assert result.snapshot.loop_counters.get("qa_fix") == 1
    # fixer must have run at least once
    assert "fixer_agent" in result.snapshot.agent_calls


def test_runtime_validator_failure_synthesizes_for_fixer_items():
    """The fixer must receive runtime-derived for_fixer items (not empty)."""
    call_count = {"n": 0}

    def hook(task_id, snapshot):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _make_report(False, "lint", "tests")
        return _make_report(True)

    fixer = SeqAgent(_FIX_OK)
    orch = Orchestrator(
        memory=PipelineMemory(),
        agents=_registry(fixer_agent=fixer),
        runtime_validator=hook,
    )
    orch.run("t-fixer", "Build something")
    assert len(fixer.calls) == 1
    # Fixer signature: (code, for_fixer_str, arch).
    _code, for_fixer_str, _arch = fixer.calls[0]
    parsed = json.loads(for_fixer_str)
    assert isinstance(parsed, list)
    # Two failed checks -> two for_fixer items
    assert len(parsed) == 2
    issues = [item["issue"] for item in parsed]
    assert any("lint:failed:lint" in i for i in issues)
    assert any("tests:failed:tests" in i for i in issues)
    for item in parsed:
        assert item["file"] == "<runtime>"
        assert item["severity"] == "error"


def test_runtime_validator_preservation_failure_includes_restore_guidance():
    report = ValidationReport(
        ok=False,
        strategy=ValidationStrategy.INPLACE,
        checks=(
            CheckResult(
                name="preservation_guard",
                ok=False,
                summary="deleted_public_defs:1",
                raw_output=(
                    "src/example.py:add\n\n"
                    "REFERENCE_FILE src/example.py\n---\n"
                    "def add(a: int, b: int) -> int:\n"
                    "    return a + b\n---"
                ),
                duration_ms=1,
            ),
        ),
        duration_ms=1,
    )

    payload = Orchestrator._build_runtime_review_payload(
        report,
        "preservation_guard:deleted_public_defs:1",
    )
    parsed = json.loads(payload)

    assert parsed["verdict"] == "REJECTED"
    item = parsed["for_fixer"][0]
    assert item["issue"] == "preservation_guard:deleted_public_defs:1"
    assert item["repair_mode"] == "preservation_restore"
    assert "REFERENCE_FILE src/example.py" in item["raw_excerpt"]
    assert "baseline" in item["instruction"]


# ---------------------------------------------------------------------------
# failure path: hook ok=False repeatedly -> FAIL on loop exhaustion
# ---------------------------------------------------------------------------


def test_runtime_validator_persistent_failure_eventually_fails():
    def hook(task_id, snapshot):
        return _make_report(False, "tests")

    orch = Orchestrator(
        memory=PipelineMemory(),
        agents=_registry(),
        runtime_validator=hook,
    )
    result = orch.run("t-fail", "Build something broken")
    assert result.final_state is State.FAIL
    assert result.failure_reason is not None
    assert "qa_fix_loop_exceeded" in result.failure_reason
    assert "runtime" in result.failure_reason


# ---------------------------------------------------------------------------
# hook raises
# ---------------------------------------------------------------------------


def test_runtime_validator_exception_terminates_fail():
    def hook(task_id, snapshot):
        raise RuntimeError("kaboom")

    orch = Orchestrator(
        memory=PipelineMemory(),
        agents=_registry(),
        runtime_validator=hook,
    )
    result = orch.run("t-exc", "Build something")
    assert result.final_state is State.FAIL
    assert result.failure_reason is not None
    assert "runtime_validator_exception" in result.failure_reason
    assert "RuntimeError" in result.failure_reason
    assert "kaboom" in result.failure_reason


def test_runtime_validator_returning_garbage_terminates_fail():
    """Hook returns something that isn't a ValidationReport — guarded path."""
    orch = Orchestrator(
        memory=PipelineMemory(),
        agents=_registry(),
        runtime_validator=lambda task_id, snapshot: "not a report",
    )
    result = orch.run("t-garbage", "Build something")
    assert result.final_state is State.FAIL
    assert result.failure_reason == "runtime_validator_returned_invalid_report"


# ---------------------------------------------------------------------------
# transitions audit
# ---------------------------------------------------------------------------


def test_runtime_failure_records_qa_to_fix_transition():
    call_count = {"n": 0}

    def hook(task_id, snapshot):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _make_report(False, "tests")
        return _make_report(True)

    orch = Orchestrator(
        memory=PipelineMemory(),
        agents=_registry(),
        runtime_validator=hook,
    )
    result = orch.run("t-trans", "Build something")
    transitions = [
        (t.from_state, t.to_state) for t in result.snapshot.transitions
    ]
    # Must include QA -> FIX exactly once (the runtime-driven one)
    qa_fix_count = sum(
        1 for f, t in transitions if f is State.QA and t is State.FIX
    )
    assert qa_fix_count == 1
    # And finally QA -> SUCCESS at the end
    assert (State.QA, State.SUCCESS) in transitions
