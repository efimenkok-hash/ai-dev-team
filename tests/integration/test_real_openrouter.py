"""Integration tests against the real OpenRouter API.

These tests are SKIPPED unless:
  * OPENROUTER_API_KEY is set in the environment, AND
  * the env variable AI_DEV_TEAM_REAL_LLM is set to "1".

This double opt-in guards against accidental spend in CI. Run locally:

    AI_DEV_TEAM_REAL_LLM=1 pytest tests/integration/ -q

Each test makes a tiny request to keep cost under ~$0.01 per run.
"""

import os

import pytest

from core.router import ask_openrouter

_REAL_LLM_ALLOWED = os.environ.get("AI_DEV_TEAM_REAL_LLM") == "1"
_OPENROUTER_KEY_PRESENT = bool(os.environ.get("OPENROUTER_API_KEY"))

pytestmark = pytest.mark.skipif(
    not (_REAL_LLM_ALLOWED and _OPENROUTER_KEY_PRESENT),
    reason=(
        "Real-LLM tests are opt-in: set both OPENROUTER_API_KEY and "
        "AI_DEV_TEAM_REAL_LLM=1 to run them."
    ),
)


def test_openrouter_responds_to_minimal_prompt():
    """Smallest possible call: one model invocation with a 6-word prompt."""
    out = ask_openrouter("Reply with the single word: pong")
    assert isinstance(out, str)
    assert out.strip(), "response must not be empty"


def test_full_pipeline_on_trivial_task():
    """End-to-end pipeline on a tiny task. Uses cost_budget_usd as a hard
    cap so a runaway loop cannot spend more than $0.50 by accident.
    """
    from core.memory import PipelineMemory
    from core.observability import Observability
    from core.orchestrator import (
        Orchestrator,
        default_agent_registry,
        reject_long_task,
    )

    obs = Observability()
    orch = Orchestrator(
        memory=PipelineMemory(),
        agents=default_agent_registry(),
        observability=obs,
        task_validators=(reject_long_task(max_chars=500),),
        cost_budget_usd=0.50,
    )
    result = orch.run(
        "smoke-1",
        "Define a Python function add(a, b) that returns a + b.",
    )
    # We do not assert SUCCESS — model output is non-deterministic. We only
    # assert that the pipeline terminated cleanly and produced a result.
    assert result.task_id == "smoke-1"
    assert result.final_state.value in {
        "SUCCESS", "FAIL", "BLOCKED",
    }
    # Observability must have captured at least one agent call.
    assert len(obs.agent_calls(task_id="smoke-1")) >= 1
