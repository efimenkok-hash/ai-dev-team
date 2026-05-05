"""End-to-end integration test: real OpenRouter → real git worktree → real ruff/pytest.

This test is SKIPPED unless:
  * OPENROUTER_API_KEY is set in the environment, AND
  * the env variable AI_DEV_TEAM_REAL_LLM is set to "1".

Run:
    AI_DEV_TEAM_REAL_LLM=1 OPENROUTER_API_KEY=sk-or-v1-... \\
        pytest tests/integration/test_real_pipeline_e2e.py -v -s

Cost: up to ~$0.20 per run (ECONOMY tier, 8-agent pipeline).
Timeout: up to 5 minutes (real LLM round-trips).

The test does NOT touch the user's ~/sandbox-project. It creates an isolated
tmp git repo so the test is reproducible, safe, and leaves the main repo clean.
"""

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Skip guard — double opt-in (no accidental spend in CI)
# ---------------------------------------------------------------------------

_REAL_LLM_ALLOWED = os.environ.get("AI_DEV_TEAM_REAL_LLM") == "1"
_OPENROUTER_KEY_PRESENT = bool(os.environ.get("OPENROUTER_API_KEY"))

pytestmark = pytest.mark.skipif(
    not (_REAL_LLM_ALLOWED and _OPENROUTER_KEY_PRESENT),
    reason=(
        "Real-LLM e2e tests are opt-in: set both OPENROUTER_API_KEY and "
        "AI_DEV_TEAM_REAL_LLM=1 to run them."
    ),
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_GIT_TIMEOUT = 30  # seconds, for repo-setup commands only


def _git(args: list[str], cwd: Path) -> str:
    """Run a git command in cwd, return stdout. Raises on non-zero exit."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {args} failed (rc={result.returncode}):\n{result.stderr}"
        )
    return result.stdout.strip()


def _build_tmp_repo(root: Path) -> Path:
    """Create a minimal, valid git repo at root/main_repo and return its path.

    Structure:
        src/__init__.py       (empty)
        tests/__init__.py     (empty)
        pyproject.toml        (ruff + pytest config)
        README.md             (placeholder)

    One commit on branch 'main' so SandboxWorkspace can create worktrees off it.
    """
    repo = root / "main_repo"
    repo.mkdir(parents=True)

    # git init — try -b main first (git >= 2.28), fall back to renaming.
    try:
        _git(["init", "-b", "main"], repo)
    except RuntimeError:
        _git(["init"], repo)
        _git(["checkout", "-b", "main"], repo)

    _git(["config", "user.name", "Test Bot"], repo)
    _git(["config", "user.email", "bot@test.local"], repo)

    # Minimal project structure that ruff and pytest are happy with.
    (repo / "src").mkdir()
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")

    (repo / "tests").mkdir()
    (repo / "tests" / "__init__.py").write_text("", encoding="utf-8")

    (repo / "pyproject.toml").write_text(
        """\
[tool.ruff]
line-length = 100
target-version = "py310"

[tool.ruff.lint]
select = ["E", "F", "W"]

[tool.pytest.ini_options]
testpaths = ["tests"]
""",
        encoding="utf-8",
    )

    (repo / "README.md").write_text("# E2E Test Repo\n", encoding="utf-8")

    _git(["add", "-A"], repo)
    _git(["commit", "-m", "chore: initial project skeleton"], repo)

    return repo


def _wait_until_idle(runner, timeout: float = 300.0) -> None:
    deadline = time.time() + timeout
    while runner.is_busy() and time.time() < deadline:
        time.sleep(0.5)


def _wait_for_final(events: list[str], timeout: float = 300.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any("Готово" in e or "Не получилось" in e for e in events):
            return
        time.sleep(0.5)


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_real_pipeline_runs_all_agents_and_finishes_cleanly(tmp_path):
    """Full e2e: real OpenRouter calls → real git worktree → real ruff/pytest.

    Uses ECONOMY tier (~$0.20). Creates an isolated tmp git repo — does NOT
    touch the user's ~/sandbox-project.

    Assertions are intentionally soft: we do NOT assert SUCCESS because a real
    LLM may produce lint errors or test failures and route into the FIX loop.
    We only assert that the pipeline:
      - started (Старт event captured)
      - all 8 agent roles were invoked at least once
      - terminated cleanly with a final message (Готово or Не получилось)
      - released the worktree (directory gone after runner finishes)
    """
    from core.background_runner import BackgroundTaskRunner
    from core.bot_runner import build_dispatcher_from_env
    from core.dispatcher_agents import build_dispatcher_agent_registry_factory
    from core.model_tier import default_registry as default_tier_registry
    from core.real_task_handler import make_real_task_handler
    from core.sandbox_workspace import SandboxConfig, SandboxWorkspace
    from core.tier_session import TierSessionStore

    task_id = "task-e2e-001"

    # 1. Isolated tmp git repo.
    repo_path = _build_tmp_repo(tmp_path)
    worktree_root = tmp_path / "worktrees"

    # 2. Capture all progress events.
    events: list[str] = []
    lock = threading.Lock()

    def send_progress(_chat_id: int, text: str) -> None:
        with lock:
            events.append(text)
        # Print for -s visibility during local runs.
        print(f"[e2e] {text[:120]}", flush=True)

    # 3. Build real stack.
    api_key = os.environ["OPENROUTER_API_KEY"]
    dispatcher = build_dispatcher_from_env({"OPENROUTER_API_KEY": api_key})
    assert dispatcher is not None, "dispatcher must be built when key present"

    tier_store = TierSessionStore(default_tier_registry())
    tier_store.set_active(chat_id=1, tier_name="ECONOMY")

    sandbox = SandboxWorkspace(
        SandboxConfig(
            main_repo_path=repo_path,
            worktree_root=worktree_root,
        )
    )

    runner = BackgroundTaskRunner()

    try:
        agent_registry_factory = build_dispatcher_agent_registry_factory(dispatcher)

        handler = make_real_task_handler(
            runner=runner,
            sandbox=sandbox,
            tier_store=tier_store,
            send_progress=send_progress,
            agent_registry_factory=agent_registry_factory,
            task_id_factory=lambda: task_id,
        )

        # 4. Submit trivial, well-defined task.
        task_text = (
            "Добавь функцию square(x: int) -> int в src/example.py, "
            "которая возвращает x*x. "
            "Плюс тест в tests/test_example.py: assert square(3) == 9."
        )
        reply = handler(task_text, _make_msg(chat_id=1, text=task_text))

        from core.telegram_bridge import BridgeReply
        assert isinstance(reply, BridgeReply), f"expected BridgeReply, got {reply!r}"
        assert "принял" in reply.body.lower() or "task-id" in reply.body.lower(), (
            f"unexpected ack: {reply.body!r}"
        )

        # 5. Wait for pipeline to finish (up to 5 min).
        _wait_until_idle(runner, timeout=300.0)
        _wait_for_final(events, timeout=30.0)

    finally:
        # 7. Always shut down runner — releases thread regardless of outcome.
        runner.shutdown()

    # 6. Assertions (soft — pipeline must finish cleanly, not necessarily SUCCESS).
    print(f"\n[e2e] All events ({len(events)} total):")
    for e in events:
        print(f"  {e[:160]}")

    # 6a. Pipeline started.
    assert any("Старт" in e for e in events), (
        "expected at least one 'Старт' event"
    )

    # 6b. All 8 agent roles appeared in event stream.
    expected_roles = (
        "planning_agent",
        "pm_agent",
        "architect_agent",
        "writer_agent",
        "reviewer_agent",
        "tester_agent",
        "qa_agent",
        "fixer_agent",
    )
    all_text = "\n".join(events)
    missing = [role for role in expected_roles if role not in all_text]
    # fixer_agent is only invoked on FAIL/FIX loops — allow it to be absent.
    non_fixer_missing = [r for r in missing if r != "fixer_agent"]
    assert not non_fixer_missing, (
        f"these agent roles never appeared in events: {non_fixer_missing}"
    )

    # 6c. Pipeline terminated with a final message.
    assert any("Готово" in e or "Не получилось" in e for e in events), (
        "pipeline must emit either 'Готово' or 'Не получилось' as final message"
    )

    # 6d. Worktree released — directory must not exist after runner shut down.
    worktree_path = worktree_root / task_id
    assert not worktree_path.exists(), (
        f"worktree not released: {worktree_path} still exists"
    )

    # 6e. If SUCCESS: branch exists in the repo and at least one file was written.
    success_events = [e for e in events if "Готово" in e]
    if success_events:
        branch = f"feature/{task_id}"
        try:
            sha = _git(["rev-parse", branch], repo_path)
            assert sha, f"branch {branch!r} has no commits"
        except RuntimeError:
            pytest.fail(
                f"Pipeline reported Готово but branch {branch!r} not found in repo"
            )


# ---------------------------------------------------------------------------
# helpers for test
# ---------------------------------------------------------------------------


def _make_msg(chat_id: int, text: str):
    from core.telegram_bridge import IncomingMessage

    return IncomingMessage(
        chat_id=chat_id,
        user_id=chat_id,
        message_id=1,
        text=text,
    )
