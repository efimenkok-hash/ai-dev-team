from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "LOCAL_SECURITY_AGENT_LIVE_IDENTITY.md"


def test_local_security_agent_live_identity_doc_exists() -> None:
    assert DOC_PATH.is_file()


def test_local_security_agent_live_identity_doc_is_truthful() -> None:
    text = DOC_PATH.read_text(encoding="utf-8")
    text_lower = text.lower()

    outcomes = (
        "Outcome: `security_agent live identity certified`",
        "Outcome: `security_agent live identity partially blocked`",
    )
    assert sum(outcome in text for outcome in outcomes) == 1

    required_markers = (
        "security_agent",
        "TELEGRAM_SECURITY_BOT_TOKEN",
        "TELEGRAM_AGENT_TOKENS",
        "@ai_dev_team_lead_bot",
        "@ai_dev_team_writer_bot",
        "@ai_dev_team_reviewer_bot",
        "optional live Telegram identity",
        "does not become a baseline internal team member",
        "token_valid",
        "reachable",
        "started",
        "polling_started",
        "/healthz",
        "/readyz",
        "/api/projects/sandbox_project/history",
        "/api/projects/sandbox_project/threads",
        "task-1779122095-e24170",
        "@ai_dev_team_security_agent_bot",
    )

    for marker in required_markers:
        assert marker in text

    if "Outcome: `security_agent live identity partially blocked`" in text:
        blocked_markers = (
            "TELEGRAM_SECURITY_BOT_TOKEN_present=false",
            "ValueError: telegram_agent_token_env_missing:TELEGRAM_SECURITY_BOT_TOKEN",
            "live identities remain exactly `3`",
            "There is no truthful direct DM proof yet",
        )
        for marker in blocked_markers:
            assert marker in text

    forbidden_markers = (
        "all specialists are now live",
        "20-30 agents are live",
        "hedgekeeper attached",
        "vps rollout is complete",
        "production deploy is complete",
        "devops_agent is now live-enabled",
        "data_agent is now live-enabled",
    )

    for marker in forbidden_markers:
        assert marker not in text_lower


def test_roadmap_syncs_l11_security_agent_live_identity_step() -> None:
    roadmap = (REPO_ROOT / "docs" / "ROADMAP_TO_PRODUCTION.md").read_text(
        encoding="utf-8"
    )

    assert "docs/LOCAL_SECURITY_AGENT_LIVE_IDENTITY.md" in roadmap
    assert (
        "`L0.11` — first specialist-to-live promotion for `security_agent`"
        in roadmap
    )
    assert "`L0.12`" in roadmap
    assert "TELEGRAM_SECURITY_BOT_TOKEN" in roadmap
    assert "direct live DM" in roadmap
    assert "security_agent" in roadmap
