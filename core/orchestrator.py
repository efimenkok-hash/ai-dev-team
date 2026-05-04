"""
core/orchestrator.py

Pipeline orchestrator. Drives FSM, invokes agents through an injected registry,
records every transition and agent call into PipelineMemory, and applies the
FAIL_SAFE rules from docs/fsm_spec.md.

CONTRACTS:
1. run(task_id, raw_task) идемпотентен по task_id — повторный запуск с тем же
   id поднимет ValueError (через PipelineMemory.new_task).
2. Orchestrator не делает I/O на файловой системе и не выходит в сеть напрямую —
   все вызовы LLM проходят через AgentRegistry.
3. Любая необработанная Exception агента -> терминальное состояние
   (BLOCKED для подготовительных state'ов, FAIL для review/test/qa/fix) с
   зафиксированным failure_reason.
4. Любое превышение лимита (global, fix-loop, или cost_budget_usd) -> FAIL.
5. Пустой / невалидный JSON ответ агента в одном state STATE_MAX_RETRY раз
   подряд -> FAIL_SAFE.
6. Артефакт каждого агента иммутабелен в PipelineMemory: только первый успешный
   ответ агента фиксируется в memory; последующие итерации (loop через FIX)
   используют scratch-state run'а и в memory не пишутся. Это сохраняет
   аудитный след без нарушения иммутабельности артефактов.
7. task_validators применяются ДО new_task; любой validator может бросить
   ValueError для отказа от запуска (security gate).
8. Если observability задан, каждый вызов агента логируется с duration_ms;
   при заданном cost_estimator также логируются tokens и cost_usd.
9. Если cost_budget_usd задан, после каждого agent call проверяется
   накопленная стоимость. Превышение -> terminate FAIL.
"""

import contextlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from core.fsm import (
    MAX_QA_FIX_LOOPS,
    MAX_REVIEW_FIX_LOOPS,
    MAX_TOTAL_AGENT_CALLS,
    MAX_TOTAL_STEPS,
    STATE_MAX_RETRY,
    State,
    can_transition,
    is_terminal,
)
from core.memory import PipelineMemory, Snapshot
from core.observability import Observability

AgentFn = Callable[..., str]
AgentRegistry = dict[str, AgentFn]
TaskValidator = Callable[[str], None]
# (agent_name, args_tuple, output) -> (input_tokens, output_tokens, cost_usd)
CostEstimator = Callable[[str, tuple, str], tuple[int, int, float]]


def reject_long_task(max_chars: int = 10000) -> TaskValidator:
    """Validator factory: forbids tasks longer than max_chars (defence against
    LLM context blowups and resource-abuse attacks).
    """
    if not isinstance(max_chars, int) or max_chars <= 0:
        raise ValueError(f"invalid_max_chars:{max_chars}")

    def _check(raw: str) -> None:
        if not isinstance(raw, str):
            raise ValueError("non_string_task")
        if len(raw) > max_chars:
            raise ValueError(f"task_too_long:{len(raw)}>{max_chars}")

    return _check


def reject_injection_markers(
    markers: tuple[str, ...] = (
        "</prompt>",
        "</system>",
        "<|im_start|>",
        "<|im_end|>",
        "[INST]",
        "[/INST]",
    ),
) -> TaskValidator:
    """Validator factory: rejects tasks containing common LLM-prompt-injection
    sentinels. Case-insensitive substring match.
    """
    if not markers:
        raise ValueError("empty_markers")
    bad = tuple(m.lower() for m in markers)

    def _check(raw: str) -> None:
        if not isinstance(raw, str):
            raise ValueError("non_string_task")
        low = raw.lower()
        for marker in bad:
            if marker in low:
                raise ValueError(f"injection_marker:{marker}")

    return _check


REQUIRED_AGENTS = (
    "planning_agent",
    "pm_agent",
    "architect_agent",
    "writer_agent",
    "reviewer_agent",
    "tester_agent",
    "qa_agent",
    "fixer_agent",
)


_PREP_STATES = frozenset({
    State.IDLE,
    State.PLANNING,
    State.PM,
    State.ARCHITECT,
    State.WRITER,
})


@dataclass(frozen=True)
class StepDecision:
    next_state: State | None = None
    fail_reason: str | None = None
    latest_code: str | None = None
    latest_review: str | None = None


@dataclass(frozen=True)
class RunResult:
    task_id: str
    final_state: State
    snapshot: Snapshot
    failure_reason: str | None


@dataclass
class _Scratch:
    latest_code: str | None = None
    latest_review: str | None = None
    state_retry: dict[State, int] = field(default_factory=dict)


def default_agent_registry() -> AgentRegistry:
    from core.agents import (
        architect_agent,
        fixer_agent,
        planning_agent,
        pm_agent,
        qa_agent,
        reviewer_agent,
        tester_agent,
        writer_agent,
    )
    return {
        "planning_agent": planning_agent,
        "pm_agent": pm_agent,
        "architect_agent": architect_agent,
        "writer_agent": writer_agent,
        "reviewer_agent": reviewer_agent,
        "tester_agent": tester_agent,
        "qa_agent": qa_agent,
        "fixer_agent": fixer_agent,
    }


def _is_empty(payload: str | None) -> bool:
    return payload is None or not payload.strip()


def _parse_json_object(payload: str) -> dict | None:
    try:
        result = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if isinstance(result, dict):
        return result
    return None


class Orchestrator:
    def __init__(
        self,
        memory: PipelineMemory,
        agents: AgentRegistry,
        observability: Observability | None = None,
        task_validators: Sequence[TaskValidator] = (),
        cost_estimator: CostEstimator | None = None,
        cost_budget_usd: float | None = None,
    ) -> None:
        missing = [a for a in REQUIRED_AGENTS if a not in agents]
        if missing:
            raise ValueError(f"missing_agents:{','.join(missing)}")
        if cost_budget_usd is not None and (
            isinstance(cost_budget_usd, bool)
            or not isinstance(cost_budget_usd, (int, float))
            or cost_budget_usd <= 0
        ):
            raise ValueError(f"invalid_cost_budget:{cost_budget_usd}")
        self._memory = memory
        self._agents = agents
        self._obs = observability
        self._task_validators: tuple[TaskValidator, ...] = tuple(task_validators)
        self._cost_estimator = cost_estimator
        self._cost_budget = cost_budget_usd

    def run(self, task_id: str, raw_task: str) -> RunResult:
        for validator in self._task_validators:
            validator(raw_task)  # may raise ValueError -> caller handles
        self._memory.new_task(task_id, raw_task)
        return self._drive(task_id)

    def _drive(self, task_id: str) -> RunResult:
        try:
            self._memory.record_transition(task_id, State.IDLE, State.PLANNING)
        except ValueError as exc:
            return self._build_result(
                task_id, State.IDLE, f"initial_transition_failed:{exc}"
            )

        state = State.PLANNING
        scratch = _Scratch()
        scratch.state_retry[state] = 0

        handlers: dict[State, Callable[[str, _Scratch], StepDecision]] = {
            State.PLANNING: self._handle_planning,
            State.PM: self._handle_pm,
            State.ARCHITECT: self._handle_architect,
            State.WRITER: self._handle_writer,
            State.REVIEW: self._handle_review,
            State.TEST: self._handle_test,
            State.QA: self._handle_qa,
            State.FIX: self._handle_fix,
        }

        while not is_terminal(state):
            limit_reason = self._check_global_limits(task_id)
            if limit_reason is not None:
                return self._terminate(task_id, state, limit_reason)

            handler = handlers[state]
            try:
                decision = handler(task_id, scratch)
            except Exception as exc:
                return self._terminate(
                    task_id,
                    state,
                    f"agent_exception:{type(exc).__name__}:{exc}",
                )

            if decision.fail_reason is not None:
                return self._terminate(task_id, state, decision.fail_reason)

            if decision.next_state is None:
                scratch.state_retry[state] = scratch.state_retry.get(state, 0) + 1
                if scratch.state_retry[state] > STATE_MAX_RETRY:
                    return self._terminate(
                        task_id, state, f"state_retry_exceeded:{state.value}"
                    )
                continue

            if not can_transition(state, decision.next_state):
                return self._terminate(
                    task_id,
                    state,
                    f"invalid_transition_decision:{state.value}->{decision.next_state.value}",
                )

            try:
                self._memory.record_transition(task_id, state, decision.next_state)
            except ValueError as exc:
                return self._terminate(
                    task_id, state, f"transition_failed:{exc}"
                )

            if decision.latest_code is not None:
                scratch.latest_code = decision.latest_code
            if decision.latest_review is not None:
                scratch.latest_review = decision.latest_review

            state = decision.next_state
            scratch.state_retry[state] = 0

        if state == State.SUCCESS:
            return self._build_result(task_id, state, None)
        return self._build_result(task_id, state, f"terminated:{state.value}")

    def _check_global_limits(self, task_id: str) -> str | None:
        transitions = self._memory.transitions_count(task_id)
        if transitions > MAX_TOTAL_STEPS:
            return f"max_total_steps_exceeded:{transitions}"
        calls = self._memory.agent_calls_count(task_id)
        if calls > MAX_TOTAL_AGENT_CALLS:
            return f"max_total_agent_calls_exceeded:{calls}"
        budget_reason = self._check_cost_budget(task_id)
        if budget_reason is not None:
            return budget_reason
        return None

    def _check_cost_budget(self, task_id: str) -> str | None:
        if self._cost_budget is None or self._obs is None:
            return None
        try:
            snap = self._obs.cost_snapshot(task_id=task_id)
        except RuntimeError:
            return None
        if snap.total_usd > self._cost_budget:
            return (
                f"cost_budget_exceeded:{snap.total_usd:.6f}"
                f">{self._cost_budget:.6f}"
            )
        return None

    def _invoke(self, task_id: str, agent_name: str, *args: str) -> str:
        self._memory.record_agent_call(task_id, agent_name)
        fn = self._agents[agent_name]
        if self._obs is None:
            return fn(*args)
        started = time.perf_counter()
        try:
            result = fn(*args)
        except Exception as exc:
            elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
            with contextlib.suppress(Exception):
                self._obs.record_agent_call(
                    agent_name=agent_name,
                    task_id=task_id,
                    duration_ms=elapsed_ms,
                    input_tokens=0,
                    output_tokens=0,
                    cost_usd=0.0,
                    ok=False,
                    error=f"{type(exc).__name__}:{exc}",
                )
            raise
        elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
        if self._cost_estimator is not None:
            try:
                in_tokens, out_tokens, cost_usd = self._cost_estimator(
                    agent_name, tuple(args), result
                )
            except Exception:
                in_tokens, out_tokens, cost_usd = 0, 0, 0.0
        else:
            in_tokens, out_tokens, cost_usd = 0, 0, 0.0
        with contextlib.suppress(Exception):
            self._obs.record_agent_call(
                agent_name=agent_name,
                task_id=task_id,
                duration_ms=elapsed_ms,
                input_tokens=int(in_tokens),
                output_tokens=int(out_tokens),
                cost_usd=float(cost_usd),
                ok=True,
            )
        return result

    def _handle_planning(self, task_id: str, scratch: _Scratch) -> StepDecision:
        raw_task = self._memory.snapshot(task_id).raw_task
        out = self._invoke(task_id, "planning_agent", raw_task)
        if _is_empty(out):
            return StepDecision()
        if _parse_json_object(out) is None:
            return StepDecision()
        if self._memory.get_artifact(task_id, "planning") is None:
            self._memory.set_artifact(task_id, "planning", out)
        return StepDecision(next_state=State.PM)

    def _handle_pm(self, task_id: str, scratch: _Scratch) -> StepDecision:
        planning = self._memory.get_artifact(task_id, "planning") or ""
        out = self._invoke(task_id, "pm_agent", planning)
        if _is_empty(out):
            return StepDecision()
        if _parse_json_object(out) is None:
            return StepDecision()
        if self._memory.get_artifact(task_id, "pm") is None:
            self._memory.set_artifact(task_id, "pm", out)
        return StepDecision(next_state=State.ARCHITECT)

    def _handle_architect(self, task_id: str, scratch: _Scratch) -> StepDecision:
        pm_plan = self._memory.get_artifact(task_id, "pm") or ""
        out = self._invoke(task_id, "architect_agent", pm_plan)
        if _is_empty(out):
            return StepDecision()
        if _parse_json_object(out) is None:
            return StepDecision()
        if self._memory.get_artifact(task_id, "architect") is None:
            self._memory.set_artifact(task_id, "architect", out)
        return StepDecision(next_state=State.WRITER)

    def _handle_writer(self, task_id: str, scratch: _Scratch) -> StepDecision:
        arch = self._memory.get_artifact(task_id, "architect") or ""
        out = self._invoke(task_id, "writer_agent", arch)
        if _is_empty(out):
            return StepDecision()
        if out.lstrip().startswith("BLOCKED:"):
            return StepDecision(fail_reason=f"writer_blocked:{out.strip()[:120]}")
        if self._memory.get_artifact(task_id, "writer") is None:
            self._memory.set_artifact(task_id, "writer", out)
        return StepDecision(
            next_state=State.REVIEW,
            latest_code=out,
        )

    def _handle_review(self, task_id: str, scratch: _Scratch) -> StepDecision:
        arch = self._memory.get_artifact(task_id, "architect") or ""
        code = scratch.latest_code or self._memory.get_artifact(task_id, "writer") or ""
        out = self._invoke(task_id, "reviewer_agent", code, arch)
        if _is_empty(out):
            return StepDecision()
        parsed = _parse_json_object(out)
        if parsed is None:
            return StepDecision()
        if self._memory.get_artifact(task_id, "review") is None:
            self._memory.set_artifact(task_id, "review", out)

        verdict = parsed.get("verdict")
        if verdict == "APPROVED":
            return StepDecision(next_state=State.TEST, latest_review=out)
        if verdict == "REJECTED":
            for_fixer = parsed.get("for_fixer")
            if not for_fixer:
                return StepDecision(fail_reason="review_rejected_without_for_fixer")
            loop = self._memory.increment_loop(task_id, "review_fix")
            if loop > MAX_REVIEW_FIX_LOOPS:
                return StepDecision(fail_reason="review_fix_loop_exceeded")
            return StepDecision(next_state=State.FIX, latest_review=out)
        return StepDecision()

    def _handle_test(self, task_id: str, scratch: _Scratch) -> StepDecision:
        arch = self._memory.get_artifact(task_id, "architect") or ""
        code = scratch.latest_code or self._memory.get_artifact(task_id, "writer") or ""
        out = self._invoke(task_id, "tester_agent", code, arch)
        if _is_empty(out):
            return StepDecision()
        if out.lstrip().startswith("BLOCKED:"):
            return StepDecision(fail_reason=f"tester_blocked:{out.strip()[:120]}")
        if self._memory.get_artifact(task_id, "test") is None:
            self._memory.set_artifact(task_id, "test", out)
        return StepDecision(next_state=State.QA)

    def _handle_qa(self, task_id: str, scratch: _Scratch) -> StepDecision:
        pm_plan = self._memory.get_artifact(task_id, "pm") or ""
        arch = self._memory.get_artifact(task_id, "architect") or ""
        code = scratch.latest_code or self._memory.get_artifact(task_id, "writer") or ""
        review = scratch.latest_review or self._memory.get_artifact(task_id, "review") or ""
        test = self._memory.get_artifact(task_id, "test") or ""
        out = self._invoke(
            task_id, "qa_agent", pm_plan, arch, code, review, test
        )
        if _is_empty(out):
            return StepDecision()
        parsed = _parse_json_object(out)
        if parsed is None:
            return StepDecision()
        if self._memory.get_artifact(task_id, "qa") is None:
            self._memory.set_artifact(task_id, "qa", out)

        verdict = parsed.get("verdict")
        if verdict == "PASS":
            return StepDecision(next_state=State.SUCCESS)
        if verdict == "FAIL":
            for_fixer = parsed.get("for_fixer")
            if not for_fixer:
                return StepDecision(fail_reason="qa_failed_without_for_fixer")
            loop = self._memory.increment_loop(task_id, "qa_fix")
            if loop > MAX_QA_FIX_LOOPS:
                return StepDecision(fail_reason="qa_fix_loop_exceeded")
            return StepDecision(next_state=State.FIX)
        return StepDecision()

    def _handle_fix(self, task_id: str, scratch: _Scratch) -> StepDecision:
        arch = self._memory.get_artifact(task_id, "architect") or ""
        code = scratch.latest_code or self._memory.get_artifact(task_id, "writer") or ""
        review_payload = (
            scratch.latest_review
            or self._memory.get_artifact(task_id, "review")
            or ""
        )
        for_fixer_str = self._extract_for_fixer(review_payload)
        out = self._invoke(
            task_id, "fixer_agent", code, for_fixer_str, arch
        )
        if _is_empty(out):
            return StepDecision()
        if out.lstrip().startswith("BLOCKED:"):
            return StepDecision(fail_reason=f"fixer_blocked:{out.strip()[:120]}")
        if self._memory.get_artifact(task_id, "fix") is None:
            self._memory.set_artifact(task_id, "fix", out)
        return StepDecision(next_state=State.REVIEW, latest_code=out)

    @staticmethod
    def _extract_for_fixer(review_payload: str) -> str:
        parsed = _parse_json_object(review_payload)
        if parsed is None:
            return "[]"
        for_fixer = parsed.get("for_fixer", [])
        return json.dumps(for_fixer, ensure_ascii=False)

    def _terminate(
        self,
        task_id: str,
        current_state: State,
        reason: str,
    ) -> RunResult:
        terminal = self._terminal_for(current_state)
        if can_transition(current_state, terminal):
            try:
                self._memory.record_transition(task_id, current_state, terminal)
                final = terminal
            except ValueError:
                final = current_state
        else:
            final = current_state
        return self._build_result(task_id, final, reason)

    @staticmethod
    def _terminal_for(state: State) -> State:
        if state in _PREP_STATES:
            return State.BLOCKED
        return State.FAIL

    def _build_result(
        self,
        task_id: str,
        final_state: State,
        failure_reason: str | None,
    ) -> RunResult:
        return RunResult(
            task_id=task_id,
            final_state=final_state,
            snapshot=self._memory.snapshot(task_id),
            failure_reason=failure_reason,
        )
