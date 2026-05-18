from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "LOCAL_TELEGRAM_QUALITY_BARRIER_DIAGNOSTICS.md"


def test_local_telegram_quality_barrier_diagnostics_doc_exists() -> None:
    assert DOC_PATH.is_file()


def test_local_telegram_quality_barrier_diagnostics_doc_is_truthful() -> None:
    text = DOC_PATH.read_text(encoding="utf-8")
    text_lower = text.lower()

    assert (
        "Outcome: `owner-visible quality-barrier diagnostics partially blocked`"
        in text
    )
    assert "owner-visible quality-barrier diagnostics certified" not in text

    required_markers = (
        "@ai_dev_team_lead_bot",
        "@ai_dev_team_writer_bot",
        "@ai_dev_team_reviewer_bot",
        "STATE_DB_PATH=/private/tmp/ai-dev-team-live-telegram-l05/",
        "OBS_LOG_PATH=/private/tmp/ai-dev-team-live-telegram-l05/",
        "task-1779096156-c9f73a",
        "task-1779102006-e4d531",
        "review_fix_loop_exceeded",
        "failure_detail=null",
        "/log task-1779102006-e4d531",
        "/api/projects/sandbox_project/history",
        "/projects/sandbox_project/history",
        "PipelineMemory",
        "`review` artifact",
        "`qa` artifact",
        "core/real_task_handler.py",
        "core/task_history.py",
        "core/bot_runner.py",
        "web/main.py",
        "review=REJECTED; summary c=0 m=1 n=0;",
        "restore square implementation",
        "127.0.0.1:8004",
        "successful live Telegram task",
        "Hedgekeeper",
        "20–30 live Telegram identities",
    )

    for marker in required_markers:
        assert marker in text

    forbidden_markers = (
        "hedgekeeper attached in read-only study-mode",
        "main project attached in assist-mode",
        "successful live telegram task loop certified",
        "vps rollout is complete",
        "production deploy is complete",
    )

    for marker in forbidden_markers:
        assert marker not in text_lower


def test_roadmap_syncs_l08_quality_barrier_diagnostics_step() -> None:
    roadmap = (REPO_ROOT / "docs" / "ROADMAP_TO_PRODUCTION.md").read_text(
        encoding="utf-8"
    )

    assert "docs/LOCAL_TELEGRAM_QUALITY_BARRIER_DIAGNOSTICS.md" in roadmap
    assert (
        "`L0.8` — owner-visible quality-barrier diagnostics for live Telegram task"
        in roadmap
    )
