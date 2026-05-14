import pytest

from core.fsm import (
    MAX_QA_FIX_LOOPS,
    MAX_REVIEW_FIX_LOOPS,
    STATE_MAX_RETRY,
    State,
)
from core.memory import PipelineMemory
from core.orchestrator import (
    REQUIRED_AGENTS,
    Orchestrator,
    RunResult,
    _is_empty,
    _parse_json_object,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class SeqAgent:
    """Callable returning a deterministic sequence of responses.

    Records every call. Once the sequence is exhausted the last value repeats,
    which models a stuck agent for retry/loop tests.
    """

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if not self.responses:
            return ""
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[idx]


class RaisingAgent:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def __call__(self, *args):
        self.calls += 1
        raise self.exc


_PLANNING_OK = '{"planning_id":"p1","ready_for_pm":true}'
_PM_OK = '{"plan_id":"x","subtasks":[{"id":"T-001"}]}'
_ARCH_OK = '{"arch_id":"a1","plan_id":"x","modules":[]}'
_WRITER_OK = "FILE: app.py\n---\nprint('hi')\n---"
_REVIEW_APPROVED = (
    '{"review_id":"r1","verdict":"APPROVED","files":[],"for_fixer":[]}'
)
_REVIEW_REJECTED = (
    '{"review_id":"r2","verdict":"REJECTED","files":[],"for_fixer":[{"path":"app.py","instruction":"do x"}]}'
)
_REVIEW_REJECTED_NO_FIXER = (
    '{"review_id":"r3","verdict":"REJECTED","files":[],"for_fixer":[]}'
)
_TEST_OK = "FILE: tests/test_app.py\n---\ndef test_x(): pass\n---"
_QA_PASS = '{"qa_id":"q1","verdict":"PASS","blockers":[],"for_fixer":[]}'
_QA_FAIL = (
    '{"qa_id":"q2","verdict":"FAIL","blockers":["b"],"for_fixer":[{"path":"app.py","instruction":"y"}]}'
)
_QA_FAIL_NO_FIXER = (
    '{"qa_id":"q3","verdict":"FAIL","blockers":["b"],"for_fixer":[]}'
)
_FIX_OK = "FILE: app.py\n---\nprint('fixed')\n---"


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


def _orch(registry=None) -> Orchestrator:
    return Orchestrator(memory=PipelineMemory(), agents=registry or _registry())


# ---------------------------------------------------------------------------
# helpers / pure functions
# ---------------------------------------------------------------------------


def test_is_empty_treats_none_and_whitespace_as_empty():
    assert _is_empty(None)
    assert _is_empty("")
    assert _is_empty("   \n\t ")
    assert not _is_empty("x")


def test_parse_json_object_returns_dict_only():
    assert _parse_json_object('{"k":1}') == {"k": 1}
    assert _parse_json_object("[1,2]") is None
    assert _parse_json_object("not json") is None
    assert _parse_json_object("") is None


# ---------------------------------------------------------------------------
# construction contract
# ---------------------------------------------------------------------------


def test_construction_requires_all_agents():
    full = _registry()
    for missing in REQUIRED_AGENTS:
        partial = {k: v for k, v in full.items() if k != missing}
        with pytest.raises(ValueError, match=f"missing_agents:{missing}"):
            Orchestrator(memory=PipelineMemory(), agents=partial)


def test_construction_succeeds_with_all_required_agents():
    Orchestrator(memory=PipelineMemory(), agents=_registry())


def test_construction_accepts_extra_agents():
    extras = _registry(custom_agent=SeqAgent("noop"))
    Orchestrator(memory=PipelineMemory(), agents=extras)


def test_construction_accepts_extra_specialist_agents_without_requiring_them():
    extras = _registry(security_agent=SeqAgent("noop"))
    Orchestrator(memory=PipelineMemory(), agents=extras)


# ---------------------------------------------------------------------------
# run idempotency
# ---------------------------------------------------------------------------


def test_run_rejects_duplicate_task_id():
    orch = _orch()
    orch.run("T1", "build x")
    with pytest.raises(ValueError, match="task_already_exists:T1"):
        orch.run("T1", "build x again")


def test_run_rejects_empty_task_id():
    with pytest.raises(ValueError, match="empty_task_id"):
        _orch().run("", "raw")


# ---------------------------------------------------------------------------
# initial artifacts
# ---------------------------------------------------------------------------


def test_run_seeds_initial_artifacts_before_planning():
    memory = PipelineMemory()
    seen: dict[str, str | None] = {}

    def _planning_agent(*args):
        seen["project_brief"] = memory.get_artifact("T1", "project_brief")
        seen["team_proposal"] = memory.get_artifact("T1", "team_proposal")
        return _PLANNING_OK

    orch = Orchestrator(
        memory=memory,
        agents=_registry(planning_agent=_planning_agent),
    )

    result = orch.run(
        "T1",
        "build x",
        initial_artifacts={
            "project_brief": "Coordinator project brief",
            "team_proposal": "Coordinator team proposal",
        },
    )

    assert result.final_state == State.SUCCESS
    assert seen["project_brief"] == "Coordinator project brief"
    assert seen["team_proposal"] == "Coordinator team proposal"
    assert result.snapshot.artifacts["project_brief"] == "Coordinator project brief"
    assert result.snapshot.artifacts["team_proposal"] == "Coordinator team proposal"


def test_run_without_initial_artifacts_keeps_old_behavior():
    result = _orch().run("T1", "build x")

    assert result.final_state == State.SUCCESS
    assert "project_brief" not in result.snapshot.artifacts


def test_invalid_initial_artifacts_do_not_leave_partial_task_state():
    memory = PipelineMemory()
    orch = Orchestrator(memory=memory, agents=_registry())

    with pytest.raises(ValueError, match="unknown_artifact_kind:bad"):
        orch.run("T1", "build x", initial_artifacts={"bad": "payload"})

    assert memory.list_tasks() == []


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_happy_path_reaches_success():
    orch = _orch()
    result = orch.run("T1", "build x")
    assert isinstance(result, RunResult)
    assert result.final_state == State.SUCCESS
    assert result.failure_reason is None


def test_happy_path_records_full_transition_chain():
    orch = _orch()
    result = orch.run("T1", "build x")
    chain = [(t.from_state, t.to_state) for t in result.snapshot.transitions]
    assert chain == [
        (State.IDLE, State.PLANNING),
        (State.PLANNING, State.PM),
        (State.PM, State.ARCHITECT),
        (State.ARCHITECT, State.WRITER),
        (State.WRITER, State.REVIEW),
        (State.REVIEW, State.TEST),
        (State.TEST, State.QA),
        (State.QA, State.SUCCESS),
    ]


def test_happy_path_calls_each_agent_once_in_order():
    orch = _orch()
    result = orch.run("T1", "build x")
    assert list(result.snapshot.agent_calls) == [
        "planning_agent",
        "pm_agent",
        "architect_agent",
        "writer_agent",
        "reviewer_agent",
        "tester_agent",
        "qa_agent",
    ]


def test_happy_path_persists_seven_artifacts():
    orch = _orch()
    result = orch.run("T1", "build x")
    expected = {"planning", "pm", "architect", "writer", "review", "test", "qa"}
    assert set(result.snapshot.artifacts.keys()) == expected
    assert "fix" not in result.snapshot.artifacts


# ---------------------------------------------------------------------------
# retry semantics on planning state
# ---------------------------------------------------------------------------


def test_planning_empty_then_valid_eventually_succeeds():
    reg = _registry(planning_agent=SeqAgent("", _PLANNING_OK))
    orch = Orchestrator(memory=PipelineMemory(), agents=reg)
    result = orch.run("T1", "build x")
    assert result.final_state == State.SUCCESS
    assert reg["planning_agent"].calls and len(reg["planning_agent"].calls) == 2


def test_planning_empty_too_many_times_blocks():
    reg = _registry(planning_agent=SeqAgent(""))
    orch = Orchestrator(memory=PipelineMemory(), agents=reg)
    result = orch.run("T1", "build x")
    assert result.final_state == State.BLOCKED
    assert result.failure_reason is not None
    assert "state_retry_exceeded:PLANNING" in result.failure_reason
    assert len(reg["planning_agent"].calls) == STATE_MAX_RETRY + 1


def test_pm_invalid_json_too_many_times_blocks():
    reg = _registry(pm_agent=SeqAgent("not a json at all"))
    orch = Orchestrator(memory=PipelineMemory(), agents=reg)
    result = orch.run("T1", "build x")
    assert result.final_state == State.BLOCKED
    assert "state_retry_exceeded:PM" in (result.failure_reason or "")


# ---------------------------------------------------------------------------
# writer / tester / fixer: BLOCKED literal
# ---------------------------------------------------------------------------


def test_writer_returns_blocked_terminates_with_blocked_state():
    reg = _registry(writer_agent=SeqAgent("BLOCKED: missing arch"))
    orch = Orchestrator(memory=PipelineMemory(), agents=reg)
    result = orch.run("T1", "build x")
    assert result.final_state == State.BLOCKED
    assert "writer_blocked" in (result.failure_reason or "")


def test_tester_returns_blocked_terminates_with_fail_state():
    reg = _registry(tester_agent=SeqAgent("BLOCKED: cannot parse writer"))
    orch = Orchestrator(memory=PipelineMemory(), agents=reg)
    result = orch.run("T1", "build x")
    assert result.final_state == State.FAIL
    assert "tester_blocked" in (result.failure_reason or "")


# ---------------------------------------------------------------------------
# review verdicts
# ---------------------------------------------------------------------------


def test_review_rejected_without_for_fixer_fails():
    reg = _registry(reviewer_agent=SeqAgent(_REVIEW_REJECTED_NO_FIXER))
    orch = Orchestrator(memory=PipelineMemory(), agents=reg)
    result = orch.run("T1", "build x")
    assert result.final_state == State.FAIL
    assert result.failure_reason == "review_rejected_without_for_fixer"


def test_review_rejected_then_approved_round_trip_succeeds():
    reg = _registry(
        reviewer_agent=SeqAgent(_REVIEW_REJECTED, _REVIEW_APPROVED),
    )
    orch = Orchestrator(memory=PipelineMemory(), agents=reg)
    result = orch.run("T1", "build x")
    assert result.final_state == State.SUCCESS
    chain = [(t.from_state, t.to_state) for t in result.snapshot.transitions]
    assert (State.REVIEW, State.FIX) in chain
    assert (State.FIX, State.REVIEW) in chain
    assert chain[-1] == (State.QA, State.SUCCESS)
    assert reg["reviewer_agent"].calls and len(reg["reviewer_agent"].calls) == 2
    assert reg["fixer_agent"].calls and len(reg["fixer_agent"].calls) == 1


def test_review_fix_loop_exceeds_limit_fails():
    # Reviewer always rejects with for_fixer; loop must hit MAX_REVIEW_FIX_LOOPS.
    reg = _registry(reviewer_agent=SeqAgent(_REVIEW_REJECTED))
    orch = Orchestrator(memory=PipelineMemory(), agents=reg)
    result = orch.run("T1", "build x")
    assert result.final_state == State.FAIL
    assert result.failure_reason == "review_fix_loop_exceeded"
    assert result.snapshot.loop_counters["review_fix"] == MAX_REVIEW_FIX_LOOPS + 1


def test_review_first_artifact_persisted_in_memory_only_once():
    reg = _registry(
        reviewer_agent=SeqAgent(_REVIEW_REJECTED, _REVIEW_APPROVED),
    )
    orch = Orchestrator(memory=PipelineMemory(), agents=reg)
    result = orch.run("T1", "build x")
    # First review (REJECTED) should be the persisted artifact.
    assert result.snapshot.artifacts["review"] == _REVIEW_REJECTED


# ---------------------------------------------------------------------------
# qa verdicts
# ---------------------------------------------------------------------------


def test_qa_fail_without_for_fixer_fails_with_explicit_reason():
    reg = _registry(qa_agent=SeqAgent(_QA_FAIL_NO_FIXER))
    orch = Orchestrator(memory=PipelineMemory(), agents=reg)
    result = orch.run("T1", "build x")
    assert result.final_state == State.FAIL
    assert result.failure_reason == "qa_failed_without_for_fixer"


def test_qa_fail_loop_exceeds_limit_fails():
    reg = _registry(qa_agent=SeqAgent(_QA_FAIL))
    orch = Orchestrator(memory=PipelineMemory(), agents=reg)
    result = orch.run("T1", "build x")
    assert result.final_state == State.FAIL
    assert result.failure_reason == "qa_fix_loop_exceeded"
    assert result.snapshot.loop_counters["qa_fix"] == MAX_QA_FIX_LOOPS + 1


def test_qa_fail_then_pass_round_trip_succeeds():
    reg = _registry(qa_agent=SeqAgent(_QA_FAIL, _QA_PASS))
    orch = Orchestrator(memory=PipelineMemory(), agents=reg)
    result = orch.run("T1", "build x")
    assert result.final_state == State.SUCCESS
    chain = [(t.from_state, t.to_state) for t in result.snapshot.transitions]
    assert (State.QA, State.FIX) in chain
    assert chain[-1] == (State.QA, State.SUCCESS)


# ---------------------------------------------------------------------------
# agent runtime exceptions
# ---------------------------------------------------------------------------


def test_planning_agent_exception_blocks():
    reg = _registry(planning_agent=RaisingAgent(RuntimeError("boom")))
    orch = Orchestrator(memory=PipelineMemory(), agents=reg)
    result = orch.run("T1", "build x")
    assert result.final_state == State.BLOCKED
    assert "agent_exception:RuntimeError:boom" in (result.failure_reason or "")


def test_review_agent_exception_fails():
    reg = _registry(reviewer_agent=RaisingAgent(ValueError("nope")))
    orch = Orchestrator(memory=PipelineMemory(), agents=reg)
    result = orch.run("T1", "build x")
    assert result.final_state == State.FAIL
    assert "agent_exception:ValueError:nope" in (result.failure_reason or "")


# ---------------------------------------------------------------------------
# misc invariants
# ---------------------------------------------------------------------------


def test_run_result_snapshot_is_decoupled_from_memory():
    memory = PipelineMemory()
    orch = Orchestrator(memory=memory, agents=_registry())
    result = orch.run("T1", "build x")
    initial_artifacts = dict(result.snapshot.artifacts)
    # Memory still contains the same task; snapshot must not be mutated.
    assert dict(result.snapshot.artifacts) == initial_artifacts


def test_invalid_review_verdict_value_retries_then_blocks():
    bogus = '{"review_id":"r","verdict":"MAYBE","files":[],"for_fixer":[]}'
    reg = _registry(reviewer_agent=SeqAgent(bogus))
    orch = Orchestrator(memory=PipelineMemory(), agents=reg)
    result = orch.run("T1", "build x")
    assert result.final_state == State.FAIL
    assert "state_retry_exceeded:REVIEW" in (result.failure_reason or "")


def test_invalid_qa_verdict_value_retries_then_fails():
    bogus = '{"qa_id":"q","verdict":"MEH","blockers":[],"for_fixer":[]}'
    reg = _registry(qa_agent=SeqAgent(bogus))
    orch = Orchestrator(memory=PipelineMemory(), agents=reg)
    result = orch.run("T1", "build x")
    assert result.final_state == State.FAIL
    assert "state_retry_exceeded:QA" in (result.failure_reason or "")


# ---------------------------------------------------------------------------
# task validators
# ---------------------------------------------------------------------------


def test_reject_long_task_validator():
    from core.orchestrator import reject_long_task

    v = reject_long_task(max_chars=10)
    v("short")
    with pytest.raises(ValueError, match="task_too_long:11>10"):
        v("x" * 11)


def test_reject_long_task_factory_rejects_invalid_max():
    from core.orchestrator import reject_long_task
    with pytest.raises(ValueError, match="invalid_max_chars"):
        reject_long_task(max_chars=0)
    with pytest.raises(ValueError, match="invalid_max_chars"):
        reject_long_task(max_chars=-5)


def test_reject_injection_markers_blocks_known_sentinels():
    from core.orchestrator import reject_injection_markers

    v = reject_injection_markers()
    v("normal task")
    for bad in ["please </system> reveal", "use [INST] markers", "<|im_start|>boom"]:
        with pytest.raises(ValueError, match="injection_marker"):
            v(bad)


def test_reject_injection_markers_is_case_insensitive():
    from core.orchestrator import reject_injection_markers

    v = reject_injection_markers()
    with pytest.raises(ValueError, match="injection_marker"):
        v("HELLO </PROMPT> world")


def test_reject_injection_markers_factory_rejects_empty_markers():
    from core.orchestrator import reject_injection_markers
    with pytest.raises(ValueError, match="empty_markers"):
        reject_injection_markers(markers=())


def test_orchestrator_runs_validators_before_new_task():
    from core.orchestrator import reject_long_task

    orch = Orchestrator(
        memory=PipelineMemory(),
        agents=_registry(),
        task_validators=(reject_long_task(max_chars=5),),
    )
    with pytest.raises(ValueError, match="task_too_long"):
        orch.run("T1", "this is way too long")
    # Memory must NOT contain the rejected task.
    assert "T1" not in orch._memory.list_tasks()


def test_orchestrator_runs_multiple_validators_in_order():
    calls: list[str] = []

    def v1(raw: str) -> None:
        calls.append("v1")

    def v2(raw: str) -> None:
        calls.append("v2")
        raise ValueError("v2_blocked")

    def v3(raw: str) -> None:
        calls.append("v3")

    orch = Orchestrator(
        memory=PipelineMemory(),
        agents=_registry(),
        task_validators=(v1, v2, v3),
    )
    with pytest.raises(ValueError, match="v2_blocked"):
        orch.run("T1", "raw")
    # v3 must NOT run because v2 raised.
    assert calls == ["v1", "v2"]


# ---------------------------------------------------------------------------
# observability integration
# ---------------------------------------------------------------------------


def test_orchestrator_without_observability_works_unchanged():
    """Backward compatibility: no observability => no behavioural change."""
    orch = Orchestrator(memory=PipelineMemory(), agents=_registry())
    result = orch.run("T1", "build x")
    assert result.final_state == State.SUCCESS


def test_orchestrator_records_agent_calls_in_observability():
    from core.observability import Observability

    obs = Observability()
    orch = Orchestrator(
        memory=PipelineMemory(),
        agents=_registry(),
        observability=obs,
    )
    result = orch.run("T1", "build x")
    assert result.final_state == State.SUCCESS
    calls = obs.agent_calls(task_id="T1")
    # 7 agents on happy path: planning, pm, architect, writer, reviewer, tester, qa
    assert len(calls) == 7
    assert all(c.ok for c in calls)
    assert all(c.duration_ms >= 0 for c in calls)
    expected_agents = [
        "planning_agent", "pm_agent", "architect_agent", "writer_agent",
        "reviewer_agent", "tester_agent", "qa_agent",
    ]
    assert [c.agent_name for c in calls] == expected_agents


def test_orchestrator_records_agent_failures_in_observability():
    from core.observability import Observability

    obs = Observability()
    reg = _registry(reviewer_agent=RaisingAgent(RuntimeError("boom")))
    orch = Orchestrator(
        memory=PipelineMemory(), agents=reg, observability=obs
    )
    result = orch.run("T1", "build x")
    assert result.final_state == State.FAIL
    failed = [c for c in obs.agent_calls(task_id="T1") if not c.ok]
    assert len(failed) == 1
    assert failed[0].agent_name == "reviewer_agent"
    assert "RuntimeError:boom" in (failed[0].error or "")


def test_orchestrator_uses_cost_estimator_if_provided():
    from core.observability import Observability

    obs = Observability()

    def estimator(agent_name, args, output):
        return 100, 50, 0.001

    orch = Orchestrator(
        memory=PipelineMemory(),
        agents=_registry(),
        observability=obs,
        cost_estimator=estimator,
    )
    orch.run("T1", "build x")
    snap = obs.cost_snapshot(task_id="T1")
    # 7 calls × 0.001
    assert snap.total_usd == pytest.approx(0.007)
    assert snap.total_input_tokens == 700
    assert snap.total_output_tokens == 350


def test_orchestrator_cost_estimator_exceptions_do_not_break_pipeline():
    from core.observability import Observability

    obs = Observability()

    def bad_estimator(agent_name, args, output):
        raise RuntimeError("estimator broken")

    orch = Orchestrator(
        memory=PipelineMemory(),
        agents=_registry(),
        observability=obs,
        cost_estimator=bad_estimator,
    )
    result = orch.run("T1", "build x")
    assert result.final_state == State.SUCCESS  # pipeline must still succeed


def test_orchestrator_cost_budget_exceeded_terminates_fail():
    from core.observability import Observability

    obs = Observability()

    def estimator(agent_name, args, output):
        return 1, 1, 0.01  # each call $0.01

    orch = Orchestrator(
        memory=PipelineMemory(),
        agents=_registry(),
        observability=obs,
        cost_estimator=estimator,
        cost_budget_usd=0.025,  # allow only 2 calls' worth
    )
    result = orch.run("T1", "build x")
    assert result.final_state in (State.FAIL, State.BLOCKED)
    assert "cost_budget_exceeded" in (result.failure_reason or "")


def test_orchestrator_rejects_invalid_cost_budget():
    with pytest.raises(ValueError, match="invalid_cost_budget"):
        Orchestrator(
            memory=PipelineMemory(),
            agents=_registry(),
            cost_budget_usd=0,
        )
    with pytest.raises(ValueError, match="invalid_cost_budget"):
        Orchestrator(
            memory=PipelineMemory(),
            agents=_registry(),
            cost_budget_usd=-1.0,
        )
    with pytest.raises(ValueError, match="invalid_cost_budget"):
        Orchestrator(
            memory=PipelineMemory(),
            agents=_registry(),
            cost_budget_usd=True,  # type: ignore[arg-type]
        )


def test_orchestrator_cost_budget_without_observability_is_inactive():
    """If user sets cost_budget_usd without observability, the budget check
    is silently inactive — no spurious failures.
    """
    orch = Orchestrator(
        memory=PipelineMemory(),
        agents=_registry(),
        cost_budget_usd=0.001,
    )
    result = orch.run("T1", "build x")
    assert result.final_state == State.SUCCESS
