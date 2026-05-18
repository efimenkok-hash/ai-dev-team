from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "LOCAL_SUCCESSFUL_TELEGRAM_TASK_LOOP.md"


def test_local_successful_telegram_task_loop_doc_exists() -> None:
    assert DOC_PATH.is_file()


def test_local_successful_telegram_task_loop_doc_is_truthful() -> None:
    text = DOC_PATH.read_text(encoding="utf-8")
    text_lower = text.lower()

    assert (
        "Outcome: `successful live Telegram task loop partially blocked`"
        in text
    )
    assert "successful live Telegram task loop certified" not in text

    required_markers = (
        "@ai_dev_team_lead_bot",
        "@ai_dev_team_writer_bot",
        "@ai_dev_team_reviewer_bot",
        "STATE_DB_PATH=/private/tmp/ai-dev-team-live-telegram-l05/",
        "OBS_LOG_PATH=/private/tmp/ai-dev-team-live-telegram-l05/",
        "/Users/efimenko_k/sandbox-project",
        "project_id=sandbox_project",
        "square(x: int) -> int",
        "test_square()",
        "README.md не меняй",
        "task-1779102006-e4d531",
        "feature/task-1779102006-e4d531",
        "review_fix_loop_exceeded",
        "thread_000002",
        "project_task_count=2",
        "thread_count=2",
        "tester_agent",
        "qa_agent",
        "ValueError: unknown_specialist_role:writer_agent",
        "❌ Не получилось",
        "state FAIL",
        "owner review",
        "/projects/sandbox_project/history",
        "/api/projects/sandbox_project/history",
        "/api/projects/sandbox_project/threads",
        "successful live Telegram task already exists",
        "write-enabled assist-mode is active",
        "20-30 live Telegram identities are already running",
    )

    for marker in required_markers:
        assert marker in text

    forbidden_markers = (
        "hedgekeeper attached in read-only study-mode",
        "main project attached in assist-mode",
        "successful live telegram task loop certified",
    )

    for marker in forbidden_markers:
        assert marker not in text_lower


def test_roadmap_syncs_l07_successful_live_task_step() -> None:
    roadmap = (REPO_ROOT / "docs" / "ROADMAP_TO_PRODUCTION.md").read_text(
        encoding="utf-8"
    )

    assert "docs/LOCAL_SUCCESSFUL_TELEGRAM_TASK_LOOP.md" in roadmap
    assert "`L0.7` — first successful live Telegram task loop / owner-facing" in roadmap
    assert "path (`docs/LOCAL_SUCCESSFUL_TELEGRAM_TASK_LOOP.md`)" in roadmap
