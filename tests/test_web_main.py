from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.project_models import Project, ProjectPolicy
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.project_runtime import ProjectRuntimeBinding
from core.startup_config_validation import StartupValidationReport
from core.state_db import StateDB


def _import_web_main(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "state.db"))
    sys.modules.pop("web.main", None)
    return importlib.import_module("web.main")


def _project(**overrides) -> Project:
    data = {
        "project_id": "alpha_project",
        "slug": "alpha-project",
        "name": "Alpha Project",
        "description": "Primary project.",
        "owner_user_id": 101,
        "status": "active",
    }
    data.update(overrides)
    return Project(**data)


def _policy(**overrides) -> ProjectPolicy:
    data = {
        "project_id": "alpha_project",
        "allow_hiring": True,
        "allow_agent_dm": False,
        "require_owner_approval_for_hires": True,
    }
    data.update(overrides)
    return ProjectPolicy(**data)


def _runtime_binding(repo_path: Path, **overrides) -> ProjectRuntimeBinding:
    data = {
        "project_id": "alpha_project",
        "adapter_name": "alpha_adapter",
        "repo_path": repo_path,
        "worktree_root": repo_path.parent / "worktrees",
        "base_branch": "main",
        "branch_prefix": "feature/",
        "language": "python",
        "rules": (),
        "commands": (),
        "forbidden_paths": (),
        "forbidden_tokens": (),
    }
    data.update(overrides)
    return ProjectRuntimeBinding(**data)


def _snapshot(tmp_path: Path) -> ProjectSnapshot:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return ProjectSnapshot(
        project=_project(),
        policy=_policy(),
        runtime_binding=_runtime_binding(repo),
    )


def test_create_app_builds_valid_project_aware_web_application(tmp_path, monkeypatch):
    module = _import_web_main(tmp_path, monkeypatch)
    app = module.create_app(
        module.WebAppConfig(
            state_db_path=tmp_path / "custom-state.db",
            app_name="Test Web Office API",
            debug=True,
        )
    )

    assert isinstance(app, FastAPI)
    assert app.title == "Test Web Office API"
    assert app.debug is True
    assert isinstance(app.state.state_db, StateDB)
    assert isinstance(app.state.project_registry, ProjectRegistry)
    assert isinstance(app.state.startup_validation_report, StartupValidationReport)
    assert app.state.startup_validation_report.is_valid is True
    assert app.state.state_db.path == (tmp_path / "custom-state.db")
    assert app.state.state_db.schema_version() == 11
    assert app.state.state_db_fallback_in_use is False


def test_module_level_app_imports_without_telegram_runtime_or_demo_data(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)

    assert isinstance(module.app, FastAPI)
    assert isinstance(module.app.state.state_db, StateDB)
    assert isinstance(module.app.state.project_registry, ProjectRegistry)
    assert isinstance(module.app.state.startup_validation_report, StartupValidationReport)
    assert module.app.state.startup_validation_report.is_valid is True
    assert module.app.state.project_registry.list_projects() == []
    assert module.app.state.state_db_fallback_in_use is False


def test_web_app_config_from_env_supports_legacy_bot_state_dir_fallback(
    tmp_path,
    monkeypatch,
):
    state_dir = tmp_path / "legacy-state"
    monkeypatch.delenv("STATE_DB_PATH", raising=False)
    monkeypatch.setenv("BOT_STATE_DIR", str(state_dir))
    sys.modules.pop("web.main", None)

    module = importlib.import_module("web.main")
    config = module.WebAppConfig.from_env()

    assert config.state_db_path == state_dir / "state.db"


def test_module_level_app_import_is_safe_without_state_db_env_override(
    tmp_path,
    monkeypatch,
):
    fake_home = tmp_path / "not-a-directory"
    fake_home.write_text("home sentinel", encoding="utf-8")
    monkeypatch.delenv("STATE_DB_PATH", raising=False)
    monkeypatch.delenv("BOT_STATE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(fake_home))
    sys.modules.pop("web.main", None)

    module = importlib.import_module("web.main")

    assert isinstance(module.app, FastAPI)
    assert isinstance(module.app.state.state_db, StateDB)
    assert isinstance(module.app.state.project_registry, ProjectRegistry)
    assert module.app.state.state_db_fallback_in_use is True
    assert module.app.state.state_db.path == module.DEFAULT_FALLBACK_STATE_DB_PATH
    assert module.app.state.project_registry.list_projects() == []


def test_module_level_import_does_not_silently_fallback_on_broken_default_db(
    tmp_path,
    monkeypatch,
):
    fake_home = tmp_path / "fake-home"
    db_dir = fake_home / ".ai-dev-team"
    db_dir.mkdir(parents=True)
    (db_dir / "state.db").write_text("not sqlite", encoding="utf-8")
    monkeypatch.delenv("STATE_DB_PATH", raising=False)
    monkeypatch.delenv("BOT_STATE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(fake_home))
    sys.modules.pop("web.main", None)

    with pytest.raises(sqlite3.DatabaseError, match="file is not a database"):
        importlib.import_module("web.main")


def test_health_and_ready_endpoints_are_truthful_and_use_real_state_db(
    tmp_path,
    monkeypatch,
):
    module = _import_web_main(tmp_path, monkeypatch)
    db_path = tmp_path / "health-state.db"
    app = module.create_app(module.WebAppConfig(state_db_path=db_path))
    snapshot = _snapshot(tmp_path)
    app.state.project_registry.register_project(snapshot)

    with TestClient(app) as client:
        health = client.get("/healthz")
        ready = client.get("/readyz")

    assert app.state.project_registry.list_projects()[0].project_id == "alpha_project"

    assert health.status_code == 200
    assert health.json() == {
        "ok": True,
        "app": "AI Dev Team Web Office API",
        "schema_version": 11,
        "project_registry_ready": True,
        "state_db_path": str(db_path),
        "state_db_fallback_in_use": False,
    }
    assert ready.status_code == 200
    assert ready.json() == {
        "ok": True,
        "ready": True,
        "app": "AI Dev Team Web Office API",
        "schema_version": 11,
        "project_registry_ready": True,
        "state_db_path": str(db_path),
        "state_db_fallback_in_use": False,
    }


def test_cli_entrypoint_remains_importable_after_web_bootstrap(tmp_path, monkeypatch):
    _import_web_main(tmp_path, monkeypatch)
    cli_main = importlib.import_module("main")

    assert callable(cli_main.main)
