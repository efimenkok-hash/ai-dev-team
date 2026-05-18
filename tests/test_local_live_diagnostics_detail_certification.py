from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = (
    REPO_ROOT
    / "docs"
    / "LOCAL_LIVE_DIAGNOSTICS_DETAIL_CERTIFICATION.md"
)


def test_local_live_diagnostics_detail_certification_doc_exists() -> None:
    assert DOC_PATH.is_file()


def test_local_live_diagnostics_detail_certification_doc_is_truthful() -> None:
    text = DOC_PATH.read_text(encoding="utf-8")
    text_lower = text.lower()

    assert "Outcome: `live diagnostics detail partially blocked`" in text
    assert "live diagnostics detail certified" not in text

    required_markers = (
        "@ai_dev_team_lead_bot",
        "@ai_dev_team_writer_bot",
        "@ai_dev_team_reviewer_bot",
        "STATE_DB_PATH=/private/tmp/ai-dev-team-live-telegram-l05/",
        "OBS_LOG_PATH=/private/tmp/ai-dev-team-live-telegram-l05/",
        "/Users/efimenko_k/sandbox-project",
        "task-1779122095-e24170",
        "feature/task-1779122095-e24170",
        "d2e9d5ac8e65eb2d2ee200216cdec2e9268ed1d5",
        "square(x: int) -> int",
        "test_square()",
        "README.md не меняй",
        "/log task-1779122095-e24170",
        "/projects/sandbox_project",
        "/projects/sandbox_project/history",
        "/api/projects/sandbox_project/history",
        "/api/projects/sandbox_project/threads",
        "/healthz",
        "/readyz",
        "failure_reason=null",
        "failure_detail=null",
        "project_task_count=3",
        "thread_count=3",
        "thread_000003",
        "ValueError: unknown_specialist_role:writer_agent",
        "✅ Готово",
        "🏁 Готово",
        "SUCCESS",
        "Hedgekeeper",
    )

    for marker in required_markers:
        assert marker in text

    forbidden_markers = (
        "hedgekeeper attached in read-only study-mode",
        "main project attached in assist-mode",
        "vps rollout is complete",
        "production deploy is complete",
        "20-30 live telegram identities are already running",
    )

    for marker in forbidden_markers:
        assert marker not in text_lower


def test_roadmap_syncs_l09_live_diagnostics_detail_step() -> None:
    roadmap = (REPO_ROOT / "docs" / "ROADMAP_TO_PRODUCTION.md").read_text(
        encoding="utf-8"
    )

    assert "docs/LOCAL_LIVE_DIAGNOSTICS_DETAIL_CERTIFICATION.md" in roadmap
    assert (
        "`L0.9` — fresh live diagnostics-detail proof on the patched runtime"
        in roadmap
    )
