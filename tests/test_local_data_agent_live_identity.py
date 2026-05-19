from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "LOCAL_DATA_AGENT_LIVE_IDENTITY.md"


def test_local_data_agent_live_identity_doc_exists() -> None:
    assert DOC_PATH.is_file()


def test_local_data_agent_live_identity_doc_is_truthful() -> None:
    text = DOC_PATH.read_text(encoding="utf-8")
    text_lower = text.lower()

    outcomes = (
        "Outcome: `data_agent live identity certified`",
        "Outcome: `data_agent live identity partially blocked`",
    )
    assert sum(outcome in text for outcome in outcomes) == 1

    required_markers = (
        "data_agent",
        "security_agent",
        "devops_agent",
        "TELEGRAM_DATA_BOT_TOKEN",
        "TELEGRAM_DEVOPS_BOT_TOKEN",
        "TELEGRAM_SECURITY_BOT_TOKEN",
        "TELEGRAM_AGENT_TOKENS",
        "does not become a baseline internal team member",
        "optional live Telegram identity",
        "@ai_dev_team_lead_bot",
        "@ai_dev_team_dev_ops_bot",
        "@ai_dev_team_writer_bot",
        "@ai_dev_team_reviewer_bot",
        "@ai_dev_team_security_agent_bot",
        "/healthz",
        "/readyz",
        "/api/projects/sandbox_project/history",
        "/api/projects/sandbox_project/threads",
        "task-1779122095-e24170",
        "docs/LOCAL_DIRECT_DM_ROLE_VOICE.md",
        "live identities before = `5`",
        "TELEGRAM_DATA_BOT_TOKEN_present=true",
        "TELEGRAM_DATA_BOT_TOKEN_len=46",
        "`('coordinator_agent', 'data_agent', 'devops_agent', 'reviewer_agent', 'security_agent', 'writer_agent')`",
        "@ai_dev_team_data_agent_bot",
        "token_valid=`true`",
        "reachable=`true`",
        "started=`true`",
        "polling_started=`true`",
        "inbound message: `/help`",
        "`Дата-инженер: 🛠 Доступные команды`",
        "live identities after = `6`",
        "Outcome: `data_agent live identity certified`",
        "Дата-инженер:",
    )

    for marker in required_markers:
        assert marker in text

    forbidden_markers = (
        "all specialists are now live",
        "20-30 live agents are live",
        "hedgekeeper attached",
        "vps rollout is complete",
        "production deploy is complete",
        "telegram_agent_token_env_missing:telegram_data_bot_token",
        "there is no truthful direct personal dm-proof yet for `data_agent`.",
        "outcome: `data_agent live identity partially blocked`",
        "live identities after = `5`",
    )

    for marker in forbidden_markers:
        assert marker not in text_lower


def test_roadmap_syncs_l18_data_agent_live_identity_step() -> None:
    roadmap = (REPO_ROOT / "docs" / "ROADMAP_TO_PRODUCTION.md").read_text(
        encoding="utf-8"
    )

    assert "docs/LOCAL_DATA_AGENT_LIVE_IDENTITY.md" in roadmap
    assert "`L0.17`" in roadmap
    assert "`L0.18`" in roadmap
    assert "data_agent" in roadmap
    assert "third specialist live identity" in roadmap
    assert "personal live DM proof for `data_agent`" in roadmap
