"""Unit tests for core.agents with router mocked.

agents.py builds 8 prompts and dispatches them through ask_openrouter.
We do not assert exact prompt wording — that is a moving target — but we
do verify:
  * the agent calls ask_openrouter (not ask_ollama)
  * the user-supplied input ends up inside the prompt
  * each agent returns the router's response unchanged

This pins the contract between agents.py and core.router without coupling
tests to prompt prose.
"""

from typing import Any

import pytest

import core.agents as agents


@pytest.fixture
def captured(monkeypatch) -> dict[str, Any]:
    """Replace ask_openrouter with a spy that returns a deterministic stub."""
    record: dict[str, Any] = {}

    def fake_openrouter(prompt: str) -> str:
        record["prompt"] = prompt
        record["calls"] = record.get("calls", 0) + 1
        return '{"verdict":"PASS"}'

    monkeypatch.setattr(agents, "ask_openrouter", fake_openrouter)
    return record


def _ensure_routed_through_openrouter(captured: dict[str, Any]) -> None:
    assert captured.get("calls") == 1, "agent must call ask_openrouter exactly once"


# ---------------------------------------------------------------------------
# planning_agent
# ---------------------------------------------------------------------------


def test_planning_agent_routes_through_openrouter_with_input(captured):
    out = agents.planning_agent("build a parser")
    assert out == '{"verdict":"PASS"}'
    _ensure_routed_through_openrouter(captured)
    assert "build a parser" in captured["prompt"]
    assert "PLANNING_AGENT" in captured["prompt"]


# ---------------------------------------------------------------------------
# pm_agent
# ---------------------------------------------------------------------------


def test_pm_agent_routes_through_openrouter_with_input(captured):
    out = agents.pm_agent("decompose this task")
    assert out == '{"verdict":"PASS"}'
    _ensure_routed_through_openrouter(captured)
    assert "decompose this task" in captured["prompt"]
    assert "PM_AGENT" in captured["prompt"]


# ---------------------------------------------------------------------------
# architect_agent
# ---------------------------------------------------------------------------


def test_architect_agent_includes_pm_plan(captured):
    pm_plan_json = '{"plan_id":"x","subtasks":[]}'
    out = agents.architect_agent(pm_plan_json)
    assert out == '{"verdict":"PASS"}'
    _ensure_routed_through_openrouter(captured)
    assert pm_plan_json in captured["prompt"]
    assert "ARCHITECT_AGENT" in captured["prompt"]


# ---------------------------------------------------------------------------
# writer_agent
# ---------------------------------------------------------------------------


def test_writer_agent_includes_arch_plan(captured):
    arch_plan_json = '{"arch_id":"a","modules":[]}'
    out = agents.writer_agent(arch_plan_json)
    assert out == '{"verdict":"PASS"}'
    _ensure_routed_through_openrouter(captured)
    assert arch_plan_json in captured["prompt"]
    assert "WRITER_AGENT" in captured["prompt"]


# ---------------------------------------------------------------------------
# reviewer_agent
# ---------------------------------------------------------------------------


def test_reviewer_agent_includes_writer_and_arch(captured):
    out = agents.reviewer_agent("FILE: app.py\n---\nx\n---", "{\"arch_id\":\"a\"}")
    assert out == '{"verdict":"PASS"}'
    _ensure_routed_through_openrouter(captured)
    assert "FILE: app.py" in captured["prompt"]
    assert "arch_id" in captured["prompt"]
    assert "REVIEWER_AGENT" in captured["prompt"]


# ---------------------------------------------------------------------------
# tester_agent
# ---------------------------------------------------------------------------


def test_tester_agent_includes_writer_and_arch(captured):
    out = agents.tester_agent("FILE: app.py\n---\ndef f(): pass\n---", "{}")
    assert out == '{"verdict":"PASS"}'
    _ensure_routed_through_openrouter(captured)
    assert "TESTER_AGENT" in captured["prompt"]
    assert "FILE: app.py" in captured["prompt"]


# ---------------------------------------------------------------------------
# qa_agent
# ---------------------------------------------------------------------------


def test_qa_agent_includes_all_five_artifacts(captured):
    out = agents.qa_agent(
        pm_plan="PMP",
        arch_plan="ARCHP",
        writer_output="WROUT",
        review="REV",
        test_output="TST",
    )
    assert out == '{"verdict":"PASS"}'
    _ensure_routed_through_openrouter(captured)
    p = captured["prompt"]
    for marker in ("PMP", "ARCHP", "WROUT", "REV", "TST", "QA_AGENT"):
        assert marker in p


# ---------------------------------------------------------------------------
# fixer_agent
# ---------------------------------------------------------------------------


def test_fixer_agent_includes_writer_for_fixer_and_arch(captured):
    out = agents.fixer_agent(
        writer_output="FILE: app.py\n---\nold\n---",
        for_fixer='[{"path":"app.py","instruction":"x"}]',
        arch_plan='{"arch_id":"a"}',
    )
    assert out == '{"verdict":"PASS"}'
    _ensure_routed_through_openrouter(captured)
    p = captured["prompt"]
    assert "FIXER_AGENT" in p
    assert "FILE: app.py" in p
    assert "instruction" in p
    assert "arch_id" in p


# ---------------------------------------------------------------------------
# Negative: agents must not call ask_ollama directly
# ---------------------------------------------------------------------------


def test_no_agent_calls_ask_ollama_directly(monkeypatch):
    """Each agent must go through ask_openrouter; ask_ollama is reserved for
    short routing decisions inside core.router.route(). If an agent ever
    calls ask_ollama directly, we want this test to fail loudly.
    """
    monkeypatch.setattr(
        agents, "ask_openrouter", lambda p: '{"verdict":"PASS"}'
    )

    def boom(*args, **kwargs):
        raise AssertionError("agents must not call ask_ollama directly")

    monkeypatch.setattr(agents, "ask_ollama", boom, raising=False)

    agents.planning_agent("x")
    agents.pm_agent("x")
    agents.architect_agent("x")
    agents.writer_agent("x")
    agents.reviewer_agent("x", "x")
    agents.tester_agent("x", "x")
    agents.qa_agent("p", "a", "w", "r", "t")
    agents.fixer_agent("w", "f", "a")
