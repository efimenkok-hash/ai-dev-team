from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

import scripts.backup_state_db as backup_script
from core.project_models import Project
from core.state_db import StateDB
from core.state_db_backup import (
    StateDbBackupConfig,
    StateDbBackupError,
    create_state_db_backup,
    default_state_db_backup_dir,
)
from core.task_history import TaskSummary


def _project(**overrides: object) -> Project:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "slug": "alpha-project",
        "name": "Alpha Project",
        "description": "Primary AI Office project.",
        "owner_user_id": 101,
        "status": "active",
    }
    data.update(overrides)
    return Project(**data)


def _task_summary(**overrides: object) -> TaskSummary:
    data: dict[str, object] = {
        "task_id": "task-123",
        "branch": "feature/task-123",
        "commit_sha": "abc123def456789",
        "final_state": "SUCCESS",
        "failure_reason": None,
        "tier_name": "PREMIUM",
        "finished_at": 1712345678.0,
        "project_id": "alpha_project",
    }
    data.update(overrides)
    return TaskSummary(**data)


def _table_count(path: Path, table_name: str) -> int:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    finally:
        conn.close()
    return int(row[0])


def _single_value(path: Path, query: str) -> object:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(query).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row[0]


def _build_state_db_with_real_data(tmp_path: Path) -> StateDB:
    state_db = StateDB(tmp_path / "state.db")
    state_db.upsert_project(_project())
    state_db.record_task(_task_summary())
    state_db.set_budget(777, 12.5)
    return state_db


class _TrackingConnection:
    def __init__(self, real_connection: sqlite3.Connection, seen: dict[str, bool]) -> None:
        self._real = real_connection
        self._seen = seen

    def backup(self, target, *args, **kwargs):
        self._seen["backup_called"] = True
        real_target = target._real if isinstance(target, _TrackingConnection) else target
        return self._real.backup(real_target, *args, **kwargs)

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


def test_create_state_db_backup_happy_path_preserves_schema_and_verifies(tmp_path):
    state_db = _build_state_db_with_real_data(tmp_path)

    result = create_state_db_backup(
        StateDbBackupConfig(
            source_state_db_path=state_db.path,
            backup_dir=tmp_path / "backups",
        )
    )

    artifact = result.artifact
    assert result.verified is True
    assert result.verification_detail == "Backup artifact verified successfully."
    assert artifact.backup_path.exists() is True
    assert artifact.manifest_path is not None
    assert artifact.manifest_path.exists() is True
    assert artifact.schema_version == state_db.schema_version()
    assert artifact.size_bytes == artifact.backup_path.stat().st_size

    manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == artifact.schema_version
    assert manifest["backup_path"] == str(artifact.backup_path)
    assert manifest["source_state_db_path"] == str(state_db.path)


def test_create_state_db_backup_preserves_real_persisted_data(tmp_path):
    state_db = _build_state_db_with_real_data(tmp_path)

    result = create_state_db_backup(
        StateDbBackupConfig(
            source_state_db_path=state_db.path,
            backup_dir=tmp_path / "backups",
        )
    )

    backup_path = result.artifact.backup_path
    assert _table_count(backup_path, "projects") == 1
    assert _table_count(backup_path, "task_history") == 1
    assert _table_count(backup_path, "budget") == 1
    assert _single_value(
        backup_path,
        "SELECT slug FROM projects WHERE project_id = 'alpha_project'",
    ) == "alpha-project"
    assert _single_value(
        backup_path,
        "SELECT task_id FROM task_history ORDER BY id DESC LIMIT 1",
    ) == "task-123"


def test_create_state_db_backup_uses_sqlite_backup_primitive_not_blind_file_copy(
    tmp_path,
    monkeypatch,
):
    state_db = _build_state_db_with_real_data(tmp_path)
    seen = {"backup_called": False}

    from core import state_db_backup as backup_module

    original_connect = backup_module._connect_sqlite

    def _tracking_connect(path: Path) -> _TrackingConnection:
        return _TrackingConnection(original_connect(path), seen)

    monkeypatch.setattr(backup_module, "_connect_sqlite", _tracking_connect)

    result = create_state_db_backup(
        StateDbBackupConfig(
            source_state_db_path=state_db.path,
            backup_dir=tmp_path / "backups",
        )
    )

    assert seen["backup_called"] is True
    assert result.verified is True


def test_create_state_db_backup_fails_when_source_db_missing(tmp_path):
    with pytest.raises(StateDbBackupError) as exc_info:
        create_state_db_backup(
            StateDbBackupConfig(
                source_state_db_path=tmp_path / "missing.db",
                backup_dir=tmp_path / "backups",
            )
        )

    assert exc_info.value.code == "missing_source_state_db_path"


def test_create_state_db_backup_fails_when_source_file_is_not_sqlite(tmp_path):
    source_path = tmp_path / "not-sqlite.db"
    source_path.write_text("not sqlite", encoding="utf-8")

    with pytest.raises(StateDbBackupError) as exc_info:
        create_state_db_backup(
            StateDbBackupConfig(
                source_state_db_path=source_path,
                backup_dir=tmp_path / "backups",
            )
        )

    assert exc_info.value.code == "invalid_sqlite_backup"


def test_create_state_db_backup_fails_when_backup_dir_is_unusable(tmp_path):
    state_db = _build_state_db_with_real_data(tmp_path)
    backup_target = tmp_path / "backup-file"
    backup_target.write_text("not a directory", encoding="utf-8")

    with pytest.raises(StateDbBackupError) as exc_info:
        create_state_db_backup(
            StateDbBackupConfig(
                source_state_db_path=state_db.path,
                backup_dir=backup_target,
            )
        )

    assert exc_info.value.code == "backup_dir_not_directory"


def test_default_state_db_backup_dir_is_deterministic(tmp_path):
    source_path = tmp_path / "state.db"

    assert default_state_db_backup_dir(source_path) == tmp_path / "state-db-backups"


def test_backup_state_db_cli_prints_deterministic_success_result(
    tmp_path,
    monkeypatch,
    capsys,
):
    state_db = _build_state_db_with_real_data(tmp_path)
    backup_dir = tmp_path / "cli-backups"
    monkeypatch.setenv("STATE_DB_PATH", str(state_db.path))
    monkeypatch.delenv("BOT_STATE_DIR", raising=False)

    with patch("scripts.backup_state_db.load_dotenv", return_value=True):
        rc = backup_script.main(["--backup-dir", str(backup_dir)])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert rc == 0
    assert captured.err == ""
    assert payload["verified"] is True
    assert payload["backup_path"].startswith(str(backup_dir))
    assert payload["source_state_db_path"] == str(state_db.path)
    assert payload["schema_version"] == state_db.schema_version()


def test_backup_state_db_cli_supports_legacy_bot_state_dir_fallback(
    tmp_path,
    monkeypatch,
    capsys,
):
    state_dir = tmp_path / "legacy-state"
    state_db = _build_state_db_with_real_data(state_dir)
    backup_dir = tmp_path / "cli-backups"
    monkeypatch.delenv("STATE_DB_PATH", raising=False)
    monkeypatch.setenv("BOT_STATE_DIR", str(state_dir))

    with patch("scripts.backup_state_db.load_dotenv", return_value=True):
        rc = backup_script.main(["--backup-dir", str(backup_dir)])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert rc == 0
    assert captured.err == ""
    assert payload["verified"] is True
    assert payload["source_state_db_path"] == str(state_db.path)


def test_backup_state_db_cli_rejects_invalid_backup_dir_with_truthful_failure(
    tmp_path,
    monkeypatch,
    capsys,
):
    state_db = _build_state_db_with_real_data(tmp_path)
    monkeypatch.setenv("STATE_DB_PATH", str(state_db.path))
    monkeypatch.delenv("BOT_STATE_DIR", raising=False)

    with patch("scripts.backup_state_db.load_dotenv", return_value=True):
        rc = backup_script.main(["--backup-dir", " "])

    captured = capsys.readouterr()

    assert rc == 2
    assert captured.out == ""
    assert "backup_state_db_failed:invalid_backup_config: empty_backup_dir" in captured.err


def test_backup_state_db_cli_exits_non_zero_on_truthful_failure(
    tmp_path,
    monkeypatch,
    capsys,
):
    missing_state_db_path = tmp_path / "missing.db"
    backup_dir = tmp_path / "cli-backups"
    monkeypatch.setenv("STATE_DB_PATH", str(missing_state_db_path))
    monkeypatch.delenv("BOT_STATE_DIR", raising=False)

    with patch("scripts.backup_state_db.load_dotenv", return_value=True):
        rc = backup_script.main(["--backup-dir", str(backup_dir)])

    captured = capsys.readouterr()

    assert rc == 1
    assert captured.out == ""
    assert "backup_state_db_failed:missing_source_state_db_path" in captured.err
