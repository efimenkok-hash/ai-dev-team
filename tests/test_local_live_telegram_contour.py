from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "LOCAL_LIVE_TELEGRAM_CONTOUR.md"


def test_local_live_telegram_contour_doc_exists() -> None:
    assert DOC_PATH.is_file()


def test_local_live_telegram_contour_doc_is_truthful() -> None:
    text = DOC_PATH.read_text(encoding="utf-8")
    text_lower = text.lower()

    assert "Outcome: `live local Telegram contour certified`" in text
    assert "live local Telegram contour partially blocked" not in text

    required_markers = (
        "TELEGRAM_OWNER_CHAT_ID",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_AGENT_TOKENS",
        "OPENROUTER_API_KEY",
        "STATE_DB_PATH=/private/tmp/ai-dev-team-live-telegram-l05/",
        "OBS_LOG_PATH=/private/tmp/ai-dev-team-live-telegram-l05/",
        ".venv/bin/python scripts/run_telegram_bot.py --log-level INFO",
        "@ai_dev_team_lead_bot",
        "@ai_dev_team_reviewer_bot",
        "@ai_dev_team_writer_bot",
        "real Telegram DM to `@ai_dev_team_lead_bot`",
        "Exact inbound user message:",
        "`/help`",
        "Координатор: 🛠 Доступные команды",
        "/healthz",
        "/readyz",
        "project_task_count=0",
        "thread_count=0",
        "first one that crossed the **real Telegram transport boundary**",
        "synthetic owner-DM/control-path proof",
        "this step does **not** claim a real multi-agent discussion loop yet",
    )

    for marker in required_markers:
        assert marker in text

    forbidden_markers = (
        "hedgekeeper attached",
        "safe attach to the main project in assist-mode",
        "vps rollout is complete",
        "production deploy is complete",
        "20-30 live telegram agents are already configured",
    )

    for marker in forbidden_markers:
        assert marker not in text_lower


def test_roadmap_syncs_l05_to_live_local_telegram_contour() -> None:
    roadmap = (REPO_ROOT / "docs" / "ROADMAP_TO_PRODUCTION.md").read_text(
        encoding="utf-8"
    )

    assert "docs/LOCAL_LIVE_TELEGRAM_CONTOUR.md" in roadmap
    assert "`L0.5` — live local Telegram contour" in roadmap
    assert "safe attach to the main project in assist-mode" not in roadmap
