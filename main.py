"""
main.py — CLI entry point for AI Dev Team.

Usage:
    python main.py "build a python script that does X"
    python main.py --task-id T1 "fix bug in module Y"
    python main.py --pipeline-log /tmp/run.jsonl --cost-budget 0.50 "make a parser"

Environment:
    OPENROUTER_API_KEY — required for real LLM calls (used by core.router).

The CLI deliberately wires only the production agent registry. For offline /
testing runs, instantiate Orchestrator directly with a custom registry.
"""

import argparse
import sys
import uuid
from pathlib import Path

from core.memory import PipelineMemory
from core.observability import JsonLinesSink, Observability
from core.orchestrator import (
    Orchestrator,
    default_agent_registry,
    reject_injection_markers,
    reject_long_task,
)

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ai-dev-team",
        description="Run the AI Dev Team pipeline on a single task.",
    )
    p.add_argument("task", nargs="?", help="The raw task description.")
    p.add_argument(
        "--task-id",
        help="Stable task id (default: auto-generated).",
    )
    p.add_argument(
        "--pipeline-log",
        help=(
            "If provided, append observability records (logs, metrics, "
            "agent calls) as JSONL to this file."
        ),
    )
    p.add_argument(
        "--cost-budget",
        type=float,
        default=None,
        help=(
            "Hard upper bound on total LLM spend in USD for this run. "
            "Effective only with --pipeline-log AND a cost estimator wired "
            "via the API; CLI does not estimate cost on its own."
        ),
    )
    p.add_argument(
        "--max-task-chars",
        type=int,
        default=10000,
        help="Reject tasks longer than this many characters (default: 10000).",
    )
    p.add_argument(
        "--no-injection-guard",
        action="store_true",
        help=(
            "Disable the prompt-injection sentinel check. Use only if you "
            "trust the input source completely."
        ),
    )
    return p


def _print_result(result) -> None:
    print(f"task_id:        {result.task_id}")
    print(f"final_state:    {result.final_state.value}")
    if result.failure_reason:
        print(f"failure_reason: {result.failure_reason}")
    print(f"transitions:    {len(result.snapshot.transitions)}")
    print(f"agent_calls:    {len(result.snapshot.agent_calls)}")
    print(f"artifacts:      {sorted(result.snapshot.artifacts.keys())}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv)

    if not args.task or not args.task.strip():
        parser.print_usage(sys.stderr)
        print("error: task is required", file=sys.stderr)
        return EXIT_USAGE

    task_id = args.task_id or f"task-{uuid.uuid4().hex[:8]}"

    validators = [reject_long_task(max_chars=args.max_task_chars)]
    if not args.no_injection_guard:
        validators.append(reject_injection_markers())

    obs: Observability | None = None
    if args.pipeline_log:
        obs = Observability(sink=JsonLinesSink(Path(args.pipeline_log)))

    try:
        orch = Orchestrator(
            memory=PipelineMemory(),
            agents=default_agent_registry(),
            observability=obs,
            task_validators=tuple(validators),
            cost_budget_usd=args.cost_budget,
        )
    except ValueError as exc:
        print(f"error: cannot construct orchestrator: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        result = orch.run(task_id, args.task)
    except ValueError as exc:
        print(f"error: task rejected by validator: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    _print_result(result)
    return EXIT_SUCCESS if result.failure_reason is None else EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
