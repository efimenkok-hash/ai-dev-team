from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "LOCAL_DIRECT_DM_ROLE_VOICE.md"


def test_local_direct_dm_role_voice_doc_exists() -> None:
    assert DOC_PATH.is_file()


def test_local_direct_dm_role_voice_doc_is_truthful() -> None:
    text = DOC_PATH.read_text(encoding="utf-8")
    text_lower = text.lower()

    outcomes = (
        "Outcome: `direct DM role voice certified`",
        "Outcome: `direct DM role voice partially blocked`",
    )
    assert sum(outcome in text for outcome in outcomes) == 1

    required_markers = (
        "security_agent",
        "@ai_dev_team_security_agent_bot",
        "Координатор: 🛠 Доступные команды",
        "Безопасник: 🛠 Доступные команды",
        "core/telegram_bridge.py",
        "_resolve_command_reply_role(msg)",
        "sender_role=reply_role",
        "delivery_role",
        "/healthz",
        "/readyz",
        "/api/projects/sandbox_project/history",
        "/api/projects/sandbox_project/threads",
        "task-1779122095-e24170",
        "count=3",
        "coordinator remains the orchestrator and control-plane voice",
    )

    for marker in required_markers:
        assert marker in text

    forbidden_markers = (
        "all specialists are now live",
        "20-30 live agents are live",
        "hedgekeeper attached",
        "vps rollout is complete",
        "production deploy is complete",
        "devops_agent is now live-enabled",
        "data_agent is now live-enabled",
    )

    for marker in forbidden_markers:
        assert marker not in text_lower
