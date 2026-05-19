from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "LOCAL_LIVE_ROSTER_EXPANSION.md"


def test_local_live_roster_expansion_doc_exists() -> None:
    assert DOC_PATH.is_file()


def test_local_live_roster_expansion_doc_is_truthful() -> None:
    text = DOC_PATH.read_text(encoding="utf-8")
    text_lower = text.lower()

    assert "Outcome: `live roster expansion partially blocked`" in text
    assert "live roster expansion certified" not in text

    required_markers = (
        "@ai_dev_team_lead_bot",
        "@ai_dev_team_writer_bot",
        "@ai_dev_team_reviewer_bot",
        "coordinator_agent=TELEGRAM_BOT_TOKEN",
        "reviewer_agent=TELEGRAM_REVIEWER_BOT_TOKEN",
        "writer_agent=TELEGRAM_WRITER_BOT_TOKEN",
        "TELEGRAM_OWNER_CHAT_ID",
        "TELEGRAM_AGENT_TOKENS",
        "TELEGRAM_SECURITY_BOT_TOKEN",
        "planning_agent",
        "pm_agent",
        "architect_agent",
        "tester_agent",
        "qa_agent",
        "fixer_agent",
        "security_agent",
        "devops_agent",
        "data_agent",
        "specialist role, not a baseline internal",
        "team member",
        "only three roles were actually separate live Telegram identities on",
        "`L0.10`; later certified live widening now reaches six identities through",
        "`security_agent`, `devops_agent`, and `data_agent`",
        "docs/LOCAL_DEVOPS_AGENT_LIVE_IDENTITY.md",
        "docs/LOCAL_DATA_AGENT_LIVE_IDENTITY.md",
        "later third specialist live-certified proof now lives in",
        "project_task_count=3",
        "thread_count=3",
        "task-1779122095-e24170",
        "/healthz",
        "/readyz",
        "/api/projects/sandbox_project/history",
        "20-30 live agents are already running",
        "Hedgekeeper",
    )

    for marker in required_markers:
        assert marker in text

    forbidden_markers = (
        "hedgekeeper attached in read-only study-mode",
        "main project attached in assist-mode",
        "vps rollout is complete",
        "production deploy is complete",
        "planning_agent direct dm proof completed successfully",
    )

    for marker in forbidden_markers:
        assert marker not in text_lower


def test_roadmap_syncs_l10_live_roster_expansion_step() -> None:
    roadmap = (REPO_ROOT / "docs" / "ROADMAP_TO_PRODUCTION.md").read_text(
        encoding="utf-8"
    )

    assert "docs/LOCAL_LIVE_ROSTER_EXPANSION.md" in roadmap
    assert (
        "`L0.10` — next bounded live Telegram roster wave / per-role delivery proof"
        in roadmap
    )
