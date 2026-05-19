from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "LOCAL_DEVOPS_AGENT_LIVE_IDENTITY.md"


def test_local_devops_agent_live_identity_doc_exists() -> None:
    assert DOC_PATH.is_file()


def test_local_devops_agent_live_identity_doc_is_truthful() -> None:
    text = DOC_PATH.read_text(encoding="utf-8")
    text_lower = text.lower()

    outcomes = (
        "Outcome: `devops_agent live identity certified`",
        "Outcome: `devops_agent live identity partially blocked`",
    )
    assert sum(outcome in text for outcome in outcomes) == 1

    required_markers = (
        "devops_agent",
        "security_agent",
        "TELEGRAM_DEVOPS_BOT_TOKEN",
        "TELEGRAM_AGENT_TOKENS",
        "TELEGRAM_SECURITY_BOT_TOKEN",
        "does not become a baseline internal team member",
        "optional live Telegram identity",
        "@ai_dev_team_lead_bot",
        "@ai_dev_team_writer_bot",
        "@ai_dev_team_reviewer_bot",
        "@ai_dev_team_security_agent_bot",
        "/healthz",
        "/readyz",
        "/api/projects/sandbox_project/history",
        "/api/projects/sandbox_project/threads",
        "task-1779122095-e24170",
        "docs/LOCAL_DIRECT_DM_ROLE_VOICE.md",
        "live identities before = `4`",
    )

    for marker in required_markers:
        assert marker in text

    if "Outcome: `devops_agent live identity partially blocked`" in text:
        blocked_markers = (
            "TELEGRAM_DEVOPS_BOT_TOKEN_present=false",
            "TELEGRAM_DEVOPS_BOT_TOKEN_len=0",
            "ValueError: telegram_agent_token_env_missing:TELEGRAM_DEVOPS_BOT_TOKEN",
            "`('coordinator_agent', 'reviewer_agent', 'security_agent', 'writer_agent')`",
            "live identities after = `4`",
            "There is no truthful direct DM proof yet for `devops_agent`.",
        )
        for marker in blocked_markers:
            assert marker in text

    forbidden_markers = (
        "all specialists are now live",
        "20-30 live agents are live",
        "hedgekeeper attached",
        "vps rollout is complete",
        "production deploy is complete",
        "data_agent is now live-enabled",
    )

    for marker in forbidden_markers:
        assert marker not in text_lower


def test_roadmap_syncs_l15_devops_agent_live_identity_step() -> None:
    roadmap = (REPO_ROOT / "docs" / "ROADMAP_TO_PRODUCTION.md").read_text(
        encoding="utf-8"
    )

    assert "docs/LOCAL_DEVOPS_AGENT_LIVE_IDENTITY.md" in roadmap
    assert "`L0.15`" in roadmap
    assert "devops_agent" in roadmap
    assert "second specialist Telegram" in roadmap
