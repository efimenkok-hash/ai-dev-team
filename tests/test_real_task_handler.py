"""Tests for core.real_task_handler (Step 14b-5: bridge↔pipeline integration).

These tests use:
  * A fake _SubprocessRunner inside SandboxWorkspace so no real git is run.
  * A real BackgroundTaskRunner (single worker thread) but with very short
    fake agent calls — total runtime per test stays under a second.
  * Predictable agent registries (happy-path JSON, noop, exploding) to drive
    Orchestrator into SUCCESS / FAIL / agent-exception code paths without
    touching any LLM.
"""

import threading
import time
from pathlib import Path

import pytest

from core.background_runner import BackgroundTaskRunner
from core.model_tier import default_registry as default_tier_registry
from core.observability import Observability
from core.progress_emitter import ProgressEvent
from core.real_task_handler import (
    RealTaskHandlerConfig,
    _format_event,
    generate_task_id,
    make_real_task_handler,
)
from core.sandbox_workspace import (
    SandboxConfig,
    SandboxWorkspace,
    _RunResult,
    _SubprocessRunner,
)
from core.telegram_bridge import BridgeReply, IncomingMessage
from core.tier_session import TierSessionStore

# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


class FakeGitRunner(_SubprocessRunner):
    """Always succeeds; records every git invocation for assertion."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, cmd, cwd, env, timeout):
        self.calls.append({"cmd": cmd, "cwd": cwd, "timeout": timeout})
        return _RunResult(returncode=0, stdout="", stderr="")


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "main_repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


@pytest.fixture
def sandbox(fake_repo: Path, tmp_path: Path) -> SandboxWorkspace:
    cfg = SandboxConfig(
        main_repo_path=fake_repo,
        worktree_root=tmp_path / "worktrees",
    )
    return SandboxWorkspace(cfg, runner=FakeGitRunner())


@pytest.fixture
def tier_store() -> TierSessionStore:
    return TierSessionStore(default_tier_registry())


@pytest.fixture
def runner():
    r = BackgroundTaskRunner()
    yield r
    r.shutdown()


def _msg(chat_id: int = 100, text: str = "build me a thing") -> IncomingMessage:
    return IncomingMessage(
        chat_id=chat_id,
        user_id=chat_id,
        message_id=1,
        text=text,
    )


def _make_progress_capture():
    captured: list[tuple[int, str]] = []
    lock = threading.Lock()

    def _send(chat_id: int, text: str) -> None:
        with lock:
            captured.append((chat_id, text))

    return _send, captured


def _wait_until_idle(runner: BackgroundTaskRunner, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while runner.is_busy() and time.time() < deadline:
        time.sleep(0.02)


def _wait_for_count(captured, predicate, timeout: float = 5.0) -> None:
    """Wait until predicate(captured) is True."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate(captured):
            return
        time.sleep(0.02)


# Predictable agent registries -------------------------------------------------


def happy_agents(_tier):
    return {
        "planning_agent": lambda *_a: '{"plan": "ok"}',
        "pm_agent": lambda *_a: '{"tasks": []}',
        "architect_agent": lambda *_a: '{"arch": "spec"}',
        "writer_agent": lambda *_a: "def f(): return 42",
        "reviewer_agent": lambda *_a: '{"verdict": "APPROVED"}',
        "tester_agent": lambda *_a: "tests pass",
        "qa_agent": lambda *_a: '{"verdict": "PASS"}',
        "fixer_agent": lambda *_a: "def f(): return 42",
    }


def noop_agents(_tier):
    """All agents return empty strings — orchestrator FAILs in PLANNING."""
    return {role: (lambda *_a: "") for role in (
        "planning_agent", "pm_agent", "architect_agent", "writer_agent",
        "reviewer_agent", "tester_agent", "qa_agent", "fixer_agent",
    )}


def exploding_agents(_tier):
    """planning_agent raises; orchestrator should terminate with agent_exception."""
    def boom(*_a):
        raise RuntimeError("planning kaboom")
    return {
        "planning_agent": boom,
        "pm_agent": lambda *_a: '{}',
        "architect_agent": lambda *_a: '{}',
        "writer_agent": lambda *_a: "x",
        "reviewer_agent": lambda *_a: '{"verdict":"APPROVED"}',
        "tester_agent": lambda *_a: "ok",
        "qa_agent": lambda *_a: '{"verdict":"PASS"}',
        "fixer_agent": lambda *_a: "x",
    }


# ---------------------------------------------------------------------------
# RealTaskHandlerConfig validation
# ---------------------------------------------------------------------------


def test_config_defaults_are_valid():
    c = RealTaskHandlerConfig()
    assert c.cost_budget_usd > 0
    assert c.max_task_chars > 0
    assert "@" in c.author_email


def test_config_is_frozen():
    c = RealTaskHandlerConfig()
    with pytest.raises(Exception):
        c.cost_budget_usd = 99  # type: ignore[misc]


@pytest.mark.parametrize("bad", [0, -1, True, "5"])
def test_config_rejects_invalid_cost_budget(bad):
    with pytest.raises(ValueError, match="invalid_cost_budget"):
        RealTaskHandlerConfig(cost_budget_usd=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "10"])
def test_config_rejects_invalid_max_task_chars(bad):
    with pytest.raises(ValueError, match="invalid_max_task_chars"):
        RealTaskHandlerConfig(max_task_chars=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["", "   "])
def test_config_rejects_empty_author_name(bad):
    with pytest.raises(ValueError, match="empty_author_name"):
        RealTaskHandlerConfig(author_name=bad)


@pytest.mark.parametrize("bad", ["no-at-sign", "", "  "])
def test_config_rejects_invalid_author_email(bad):
    with pytest.raises(ValueError, match="invalid_author_email"):
        RealTaskHandlerConfig(author_email=bad)


# ---------------------------------------------------------------------------
# generate_task_id
# ---------------------------------------------------------------------------


def test_generate_task_id_default():
    tid = generate_task_id()
    assert tid.startswith("task-")
    # Must be acceptable to SandboxWorkspace
    import re
    assert re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", tid)


def test_generate_task_id_with_custom_prefix():
    tid = generate_task_id(prefix="abc")
    assert tid.startswith("abc-")


def test_generate_task_id_with_custom_clock():
    tid = generate_task_id(clock=lambda: 1234567890.0)
    assert tid.startswith("task-1234567890-")


def test_generate_task_id_unique():
    ids = {generate_task_id() for _ in range(50)}
    assert len(ids) == 50


@pytest.mark.parametrize("bad", ["", "   ", "BadCase", "with space", "x" * 70])
def test_generate_task_id_rejects_bad_prefix(bad):
    with pytest.raises(ValueError):
        generate_task_id(prefix=bad)


@pytest.mark.parametrize("bad", [0, -1, True])
def test_generate_task_id_rejects_invalid_clock_value(bad):
    with pytest.raises(ValueError, match="invalid_clock_value"):
        generate_task_id(clock=lambda _bad=bad: _bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _format_event
# ---------------------------------------------------------------------------


def test_format_task_started():
    e = ProgressEvent(kind="task_started", timestamp=1.0, detail="hello")
    text = _format_event(e)
    assert "Старт" in text
    assert "hello" in text


def test_format_agent_started():
    e = ProgressEvent(kind="agent_started", timestamp=1.0, agent_role="architect_agent")
    assert "architect_agent" in _format_event(e)
    assert "начал" in _format_event(e)


def test_format_agent_finished_includes_duration():
    e = ProgressEvent(
        kind="agent_finished",
        timestamp=1.0,
        agent_role="writer_agent",
        duration_ms=1234,
    )
    assert "1234" in _format_event(e)


def test_format_agent_failed_truncates_long_detail():
    e = ProgressEvent(
        kind="agent_failed",
        timestamp=1.0,
        agent_role="writer_agent",
        detail="X" * 500,
    )
    out = _format_event(e)
    # We don't allow a single line to grow unboundedly.
    assert len(out) <= 200


def test_format_task_completed():
    e = ProgressEvent(kind="task_completed", timestamp=1.0, detail="branch=feature/x")
    assert "Готово" in _format_event(e)


def test_format_task_failed_with_no_detail():
    e = ProgressEvent(kind="task_failed", timestamp=1.0)
    assert "Провалена" in _format_event(e)


# ---------------------------------------------------------------------------
# make_real_task_handler — validation
# ---------------------------------------------------------------------------


def test_make_handler_rejects_non_runner(sandbox, tier_store):
    send, _ = _make_progress_capture()
    with pytest.raises(ValueError, match="invalid_runner"):
        make_real_task_handler(
            runner="not a runner",  # type: ignore[arg-type]
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send,
        )


def test_make_handler_rejects_non_sandbox(runner, tier_store):
    send, _ = _make_progress_capture()
    with pytest.raises(ValueError, match="invalid_sandbox"):
        make_real_task_handler(
            runner=runner,
            sandbox="not a sandbox",  # type: ignore[arg-type]
            tier_store=tier_store,
            send_progress=send,
        )


def test_make_handler_rejects_non_store(runner, sandbox):
    send, _ = _make_progress_capture()
    with pytest.raises(ValueError, match="invalid_tier_store"):
        make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store="not a store",  # type: ignore[arg-type]
            send_progress=send,
        )


def test_make_handler_rejects_non_callable_send(runner, sandbox, tier_store):
    with pytest.raises(ValueError, match="send_progress_not_callable"):
        make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress="not callable",  # type: ignore[arg-type]
        )


def test_make_handler_rejects_non_callable_factory(runner, sandbox, tier_store):
    send, _ = _make_progress_capture()
    with pytest.raises(ValueError, match="agent_registry_factory_not_callable"):
        make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send,
            agent_registry_factory="not callable",  # type: ignore[arg-type]
        )


def test_make_handler_rejects_non_config(runner, sandbox, tier_store):
    send, _ = _make_progress_capture()
    with pytest.raises(ValueError, match="invalid_config"):
        make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send,
            config="not a config",  # type: ignore[arg-type]
        )


def test_make_handler_rejects_non_observability(runner, sandbox, tier_store):
    send, _ = _make_progress_capture()
    with pytest.raises(ValueError, match="invalid_observability"):
        make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send,
            observability="not obs",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# _handle: short-circuit cases (synchronous, no submit)
# ---------------------------------------------------------------------------


def test_handle_no_tier_returns_pick_tier_reply(runner, sandbox, tier_store):
    send, _ = _make_progress_capture()
    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=send,
    )
    reply = handler("do something", _msg(chat_id=42))
    assert isinstance(reply, BridgeReply)
    assert "тариф" in reply.body.lower()
    assert "/tier" in reply.body
    # No work was submitted.
    assert runner.is_busy() is False


def test_handle_invalid_msg_returns_internal_error(runner, sandbox, tier_store):
    send, _ = _make_progress_capture()
    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=send,
    )
    reply = handler("hi", "not a message")  # type: ignore[arg-type]
    assert isinstance(reply, BridgeReply)
    assert "ошибка моста" in reply.body.lower()


def test_handle_busy_returns_wait_reply(runner, sandbox, tier_store):
    """If runner is already running another task, /tier-eligible chats get told."""
    tier_store.set_active(42, "STANDARD")
    send, _captured = _make_progress_capture()

    block = threading.Event()

    def slow_run(_token):
        block.wait(timeout=5)
        return "done"

    runner.submit(
        raw_task="first",
        run_fn=slow_run,
        on_complete=lambda *_: None,
    )
    try:
        handler = make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send,
        )
        reply = handler("second task", _msg(chat_id=42))
        assert isinstance(reply, BridgeReply)
        assert "уже работаю" in reply.body.lower()
        assert "first" in reply.body
    finally:
        block.set()
        _wait_until_idle(runner)


def test_handle_invalid_task_id_factory(runner, sandbox, tier_store):
    """A bogus task_id_factory must not crash the handler."""
    tier_store.set_active(42, "STANDARD")
    send, _ = _make_progress_capture()
    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=send,
        task_id_factory=lambda: "BadCASE!",
    )
    reply = handler("hi", _msg(chat_id=42))
    assert isinstance(reply, BridgeReply)
    assert "невалидный task_id" in reply.body.lower()


def test_handle_factory_raises(runner, sandbox, tier_store):
    tier_store.set_active(42, "STANDARD")
    send, _ = _make_progress_capture()

    def bad_factory():
        raise RuntimeError("kaboom")

    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=send,
        task_id_factory=bad_factory,
    )
    reply = handler("hi", _msg(chat_id=42))
    assert isinstance(reply, BridgeReply)
    assert "kaboom" in reply.body


def test_handle_returns_ack_when_submitted(runner, sandbox, tier_store):
    tier_store.set_active(42, "STANDARD")
    send, _captured = _make_progress_capture()

    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=send,
        agent_registry_factory=happy_agents,
    )
    reply = handler("write me a function", _msg(chat_id=42))
    assert isinstance(reply, BridgeReply)
    assert "принял" in reply.body.lower()
    assert "task-id" in reply.body.lower()
    assert "STANDARD" in reply.body
    _wait_until_idle(runner)


# ---------------------------------------------------------------------------
# Full pipeline through the worker thread (happy + sad paths)
# ---------------------------------------------------------------------------


def test_full_run_happy_path_streams_progress_and_releases_worktree(
    runner, sandbox, tier_store, fake_repo, tmp_path,
):
    tier_store.set_active(42, "STANDARD")
    send, captured = _make_progress_capture()

    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=send,
        agent_registry_factory=happy_agents,
        task_id_factory=lambda: "task-happy-001",
    )

    handler("build a counter function", _msg(chat_id=42))
    _wait_until_idle(runner)
    # Wait briefly for on_complete after worker finishes
    _wait_for_count(captured, lambda c: any("Готово" in t for _, t in c))

    sent_chat_ids = {cid for cid, _ in captured}
    sent_texts = [t for _, t in captured]

    # All progress messages targeted the same chat
    assert sent_chat_ids == {42}
    # We see the start, agent transitions, and final 'Готово' (либо в ивенте, либо в финале)
    assert any("Старт" in t for t in sent_texts)
    assert any("worktree" in t.lower() for t in sent_texts)
    assert any("Готово" in t for t in sent_texts)
    # All 8 happy-path roles called at least once.
    for role in (
        "planning_agent", "pm_agent", "architect_agent", "writer_agent",
        "reviewer_agent", "tester_agent", "qa_agent",
    ):
        assert any(role in t for t in sent_texts), f"missing {role} in stream"

    # Worktree was released — directory shouldn't exist anymore
    worktree_path = sandbox.config.worktree_root / "task-happy-001"
    assert not worktree_path.exists()


def test_full_run_failing_pipeline_emits_failed_event(
    runner, sandbox, tier_store, tmp_path,
):
    tier_store.set_active(42, "ECONOMY")
    send, captured = _make_progress_capture()

    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=send,
        agent_registry_factory=noop_agents,
        task_id_factory=lambda: "task-noop-001",
    )

    handler("anything", _msg(chat_id=42))
    _wait_until_idle(runner)
    _wait_for_count(
        captured,
        lambda c: any("Не получилось" in t or "Провалена" in t for _, t in c),
    )

    sent_texts = [t for _, t in captured]
    assert any("Не получилось" in t or "Провалена" in t for t in sent_texts)


def test_full_run_agent_exception_terminates_pipeline(
    runner, sandbox, tier_store,
):
    tier_store.set_active(42, "ECONOMY")
    send, captured = _make_progress_capture()

    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=send,
        agent_registry_factory=exploding_agents,
        task_id_factory=lambda: "task-boom-001",
    )

    handler("anything", _msg(chat_id=42))
    _wait_until_idle(runner)
    _wait_for_count(
        captured,
        lambda c: any("Не получилось" in t or "Провалена" in t for _, t in c),
    )

    sent_texts = [t for _, t in captured]
    # Either the agent_failed event arrived or the on_complete final message
    assert (
        any("planning_agent" in t and "упал" in t for t in sent_texts)
        or any("Не получилось" in t for t in sent_texts)
    )


def test_send_progress_failure_does_not_break_worker(
    runner, sandbox, tier_store,
):
    """If send_progress raises, the worker thread must keep running cleanly."""
    tier_store.set_active(42, "STANDARD")
    call_count = {"n": 0}

    def flaky_send(_chat_id, _text):
        call_count["n"] += 1
        if call_count["n"] % 2 == 0:
            raise RuntimeError("transport down")

    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=flaky_send,
        agent_registry_factory=happy_agents,
    )
    handler("ok", _msg(chat_id=42))
    _wait_until_idle(runner)
    # Some attempted sends happened; importantly no exception escaped.
    assert call_count["n"] > 0
    assert runner.is_busy() is False


# ---------------------------------------------------------------------------
# on_complete final message
# ---------------------------------------------------------------------------


def test_on_complete_emits_success_summary_for_happy_path(
    runner, sandbox, tier_store,
):
    tier_store.set_active(42, "PREMIUM")
    send, captured = _make_progress_capture()
    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=send,
        agent_registry_factory=happy_agents,
        task_id_factory=lambda: "task-final-001",
    )
    handler("ok", _msg(chat_id=42))
    _wait_until_idle(runner)
    _wait_for_count(captured, lambda c: any("Готово" in t for _, t in c))

    final_msgs = [t for _, t in captured if "Готово" in t]
    assert final_msgs, "no success message"
    assert any("PREMIUM" in t for t in final_msgs)
    assert any("task-final-001" in t for t in final_msgs)


def test_on_complete_emits_failure_summary_for_noop(
    runner, sandbox, tier_store,
):
    tier_store.set_active(42, "ECONOMY")
    send, captured = _make_progress_capture()
    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=send,
        agent_registry_factory=noop_agents,
        task_id_factory=lambda: "task-fail-001",
    )
    handler("ok", _msg(chat_id=42))
    _wait_until_idle(runner)
    _wait_for_count(captured, lambda c: any("Не получилось" in t for _, t in c))

    final_msgs = [t for _, t in captured if "Не получилось" in t]
    assert final_msgs
    assert any("ECONOMY" in t for t in final_msgs)


# ---------------------------------------------------------------------------
# Edge: tier disappears between set_active and submit
# ---------------------------------------------------------------------------


class _DroppingStore(TierSessionStore):
    """active_tier_name returns a name that is NOT in the registry."""

    def active_tier_name(self, chat_id: int) -> str | None:
        return "GHOST"


def test_handle_stale_tier_returns_repick_reply(runner, sandbox):
    store = _DroppingStore(default_tier_registry())
    send, _ = _make_progress_capture()
    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=store,
        send_progress=send,
    )
    reply = handler("hi", _msg(chat_id=1))
    assert isinstance(reply, BridgeReply)
    assert "GHOST" in reply.body
    assert "/tier set" in reply.body


# ---------------------------------------------------------------------------
# Observability injection (smoke test only)
# ---------------------------------------------------------------------------


def test_observability_injected_succeeds(runner, sandbox, tier_store):
    tier_store.set_active(42, "STANDARD")
    send, _ = _make_progress_capture()
    obs = Observability()
    handler = make_real_task_handler(
        runner=runner,
        sandbox=sandbox,
        tier_store=tier_store,
        send_progress=send,
        observability=obs,
        agent_registry_factory=happy_agents,
    )
    reply = handler("hi", _msg(chat_id=42))
    assert isinstance(reply, BridgeReply)
    _wait_until_idle(runner)
