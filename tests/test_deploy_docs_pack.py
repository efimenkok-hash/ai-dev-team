from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README_PATH = REPO_ROOT / "README.md"
DEPLOY_DOC_PATH = REPO_ROOT / "docs" / "DEPLOY_NEW_ARCHITECTURE.md"


def test_canonical_deploy_doc_exists_and_readme_links_to_it():
    assert DEPLOY_DOC_PATH.exists() is True
    assert README_PATH.exists() is True

    readme = README_PATH.read_text(encoding="utf-8")

    assert "docs/DEPLOY_NEW_ARCHITECTURE.md" in readme
    assert "docs/ROADMAP_TO_PRODUCTION.md" in readme
    assert "Current priorities after pipeline validation are" not in readme


def test_deploy_doc_mentions_current_canonical_surfaces_truthfully():
    text = DEPLOY_DOC_PATH.read_text(encoding="utf-8")

    required_markers = (
        ".env.example",
        "scripts/run_telegram_bot.py",
        "scripts/backup_state_db.py",
        "web.main:app",
        "/healthz",
        "/readyz",
        "STATE_DB_PATH",
        "What Is Already Real",
        "What Is Not Implemented Yet",
    )

    for marker in required_markers:
        assert marker in text


def test_deploy_doc_references_real_files_and_commands_only():
    text = DEPLOY_DOC_PATH.read_text(encoding="utf-8")

    referenced_paths = (
        ".env.example",
        "README.md",
        "core/env_layout.py",
        "core/startup_config_validation.py",
        "core/healthcheck_model.py",
        "core/state_db_backup.py",
        "scripts/run_telegram_bot.py",
        "scripts/backup_state_db.py",
        "web/main.py",
        "docs/ROADMAP_TO_PRODUCTION.md",
    )

    for relative_path in referenced_paths:
        assert relative_path in text
        assert (REPO_ROOT / relative_path).exists() is True

    assert ".venv/bin/python -m uvicorn web.main:app --host 127.0.0.1 --port 8000" in text


def test_deploy_doc_clearly_marks_c1_items_as_not_yet_implemented():
    text = DEPLOY_DOC_PATH.read_text(encoding="utf-8")

    not_yet_markers = (
        "no systemd unit",
        "no nginx config",
        "no domain / HTTPS rollout",
        "no cron backup automation",
        "no remote backup push",
        "no VPS rollout",
    )

    for marker in not_yet_markers:
        assert marker in text
