from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "LOCAL_TEAM_AGENT_CERTIFICATION.md"
ROADMAP_PATH = REPO_ROOT / "docs" / "ROADMAP_TO_PRODUCTION.md"


def test_local_team_agent_certification_doc_exists_and_roadmap_points_to_it():
    assert DOC_PATH.exists() is True
    assert ROADMAP_PATH.exists() is True

    roadmap = ROADMAP_PATH.read_text(encoding="utf-8")
    assert "L0.2" in roadmap
    assert "docs/LOCAL_TEAM_AGENT_CERTIFICATION.md" in roadmap


def test_local_team_agent_certification_doc_covers_team_contract_truthfully():
    text = DOC_PATH.read_text(encoding="utf-8")

    required_markers = (
        "coordinator_agent",
        "planning_agent",
        "pm_agent",
        "architect_agent",
        "writer_agent",
        "reviewer_agent",
        "tester_agent",
        "qa_agent",
        "fixer_agent",
        "security_agent",
        "devops_agent",
        "data_agent",
        "runtime-exposed roles",
        "Logical-Only Roles",
        "/agents",
        "/projects/{project_id}",
        "/projects/{project_id}/team",
        "GET /api/projects/{project_id}/team",
        "TELEGRAM_OWNER_CHAT_ID",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_AGENT_TOKENS",
        "TELEGRAM_SECURITY_BOT_TOKEN",
        "docs/LOCAL_SECURITY_AGENT_LIVE_IDENTITY.md",
        "does **not** become a",
        "baseline internal team member",
    )

    for marker in required_markers:
        assert marker in text

    outcomes = (
        "team model certified locally",
        "team certification partially blocked",
    )
    assert sum(outcome in text for outcome in outcomes) == 1

    forbidden_markers = (
        "all 12 agents are already fully separate Telegram bot identities",
        "every known role is already a dedicated Telegram bot identity",
        "specialist roles are already separately exposed as Telegram bot identities",
    )

    for marker in forbidden_markers:
        assert marker not in text
