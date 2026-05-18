from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README_PATH = REPO_ROOT / "README.md"
ROADMAP_PATH = REPO_ROOT / "docs" / "ROADMAP_TO_PRODUCTION.md"
RUNBOOK_PATH = REPO_ROOT / "docs" / "LOCAL_ISOLATED_PILOT_RUNBOOK.md"
STATUS_PATH = REPO_ROOT / "docs" / "LOCAL_ISOLATED_PILOT_STATUS.md"


def test_local_isolated_pilot_docs_exist_and_readme_links_to_runbook():
    assert RUNBOOK_PATH.exists() is True
    assert STATUS_PATH.exists() is True
    assert ROADMAP_PATH.exists() is True
    assert README_PATH.exists() is True

    readme = README_PATH.read_text(encoding="utf-8")
    assert "docs/LOCAL_ISOLATED_PILOT_RUNBOOK.md" in readme


def test_roadmap_contains_explicit_local_deployment_track():
    roadmap = ROADMAP_PATH.read_text(encoding="utf-8")

    required_markers = (
        "Локальное развертывание",
        "L0.1",
        "L0.2",
        "L0.3",
        "L0.4",
        "L0.5",
        "docs/LOCAL_ISOLATED_PILOT_RUNBOOK.md",
        "docs/LOCAL_ISOLATED_PILOT_STATUS.md",
    )

    for marker in required_markers:
        assert marker in roadmap


def test_local_runbook_mentions_isolated_native_local_contract():
    text = RUNBOOK_PATH.read_text(encoding="utf-8")

    required_markers = (
        "native local on macOS",
        "STATE_DB_PATH",
        "WORKTREE_ROOT",
        "~/ai-dev-team-local-pilot/state/state.db",
        "~/ai-dev-team-local-pilot/worktrees/",
        "~/ai-dev-team-local-pilot/targets/",
        "127.0.0.1:8001",
        "scripts/run_telegram_bot.py",
        "web.main:app",
        "/healthz",
        "/readyz",
        "scripts/backup_state_db.py",
        "live Docker-mounted main project",
        "Do not point `REPO_PATH` at the live Docker-mounted main project path.",
    )

    for marker in required_markers:
        assert marker in text

    forbidden_markers = (
        "docker compose up",
        "podman compose",
        "systemd already configured",
        "HTTPS already connected",
    )

    for marker in forbidden_markers:
        assert marker not in text


def test_local_status_doc_contains_exactly_one_truthful_outcome():
    text = STATUS_PATH.read_text(encoding="utf-8")

    outcomes = (
        "local pilot baseline ready",
        "pilot blocked externally",
    )

    matches = sum(outcome in text for outcome in outcomes)
    assert matches == 1

    required_markers = (
        "/private/tmp/ai-dev-team-local-pilot-smoke/state/state.db",
        "127.0.0.1:8001",
        "Backup artifact verified successfully.",
        "missing_telegram_owner_chat_id",
        "missing_bot_identity_startup_path",
    )

    for marker in required_markers:
        assert marker in text
