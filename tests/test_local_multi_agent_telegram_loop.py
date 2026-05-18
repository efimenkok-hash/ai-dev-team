from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "LOCAL_MULTI_AGENT_TELEGRAM_LOOP.md"


def test_local_multi_agent_telegram_loop_doc_exists() -> None:
    assert DOC_PATH.is_file()


def test_local_multi_agent_telegram_loop_doc_is_truthful() -> None:
    text = DOC_PATH.read_text(encoding="utf-8")

    assert "Outcome: `live multi-agent Telegram loop partially blocked`" in text
    assert "live multi-agent Telegram loop certified" not in text

    required_markers = (
        "coordinator_agent=TELEGRAM_BOT_TOKEN",
        "writer_agent=TELEGRAM_WRITER_BOT_TOKEN",
        "reviewer_agent=TELEGRAM_REVIEWER_BOT_TOKEN",
        "@ai_dev_team_lead_bot",
        "@ai_dev_team_reviewer_bot",
        "@ai_dev_team_writer_bot",
        "planning_agent",
        "pm_agent",
        "architect_agent",
        "writer_agent",
        "reviewer_agent",
        "fixer_agent",
        "task-1779096156-c9f73a",
        "feature/task-1779096156-c9f73a",
        "review_fix_loop_exceeded",
        "thread_000001",
        "project_task_count=1",
        "thread_count=1",
        "the live Telegram roster stayed at **3 real bot identities**",
        "there are no real configured tokens for",
        "This step still does **not** claim any of the following:",
        "20-30 live Telegram identities are already running",
        "Hedgekeeper is attached",
        "write-enabled assist-mode is active",
        "VPS rollout is complete",
        "production deploy is complete",
        "/api/projects/sandbox_project/history",
        "/api/projects/sandbox_project/threads",
    )

    for marker in required_markers:
        assert marker in text

    assert "Outcome: `live multi-agent Telegram loop certified`" not in text


def test_roadmap_syncs_l06_multi_agent_step() -> None:
    roadmap = (REPO_ROOT / "docs" / "ROADMAP_TO_PRODUCTION.md").read_text(
        encoding="utf-8"
    )

    assert "docs/LOCAL_MULTI_AGENT_TELEGRAM_LOOP.md" in roadmap
    assert (
        "`L0.6` — first live roster expansion / role-aware multi-agent Telegram loop"
        in roadmap
    )
