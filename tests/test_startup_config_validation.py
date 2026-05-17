from __future__ import annotations

import pytest

from core.env_layout import (
    STATE_DB_SOURCE_DEFAULT,
    STATE_DB_SOURCE_ENV,
    STATE_DB_SOURCE_LEGACY,
    SharedRuntimeEnvConfig,
    WebRuntimeEnvConfig,
)
from core.startup_config_validation import (
    StartupConfigValidationError,
    StartupValidationReport,
    format_startup_validation_report,
    raise_for_startup_validation_errors,
    validate_bot_startup_config,
    validate_shared_runtime_config,
    validate_web_startup_config,
)


def _issue_codes(report: StartupValidationReport) -> set[str]:
    return {issue.code for issue in report.issues}


def test_validate_shared_runtime_config_accepts_explicit_state_db_path_and_log_level(
    tmp_path,
):
    env = {
        "STATE_DB_PATH": str(tmp_path / "state.db"),
        "OBS_LOG_PATH": str(tmp_path / "obs.jsonl"),
        "LOG_LEVEL": " debug ",
    }

    report = validate_shared_runtime_config(env)
    config = SharedRuntimeEnvConfig.from_env(env)

    assert report.scope == "shared"
    assert report.is_valid is True
    assert report.has_errors is False
    assert report.has_warnings is False
    assert config.state_db_source == STATE_DB_SOURCE_ENV
    assert config.log_level == "DEBUG"


def test_validate_shared_runtime_config_accepts_bot_state_dir_fallback(tmp_path):
    env = {"BOT_STATE_DIR": str(tmp_path / "legacy-state")}

    report = validate_shared_runtime_config(env)
    config = SharedRuntimeEnvConfig.from_env(env)

    assert report.is_valid is True
    assert config.state_db_source == STATE_DB_SOURCE_LEGACY
    assert config.state_db_path == tmp_path / "legacy-state" / "state.db"


def test_validate_shared_runtime_config_warns_for_unknown_log_level():
    report = validate_shared_runtime_config({"LOG_LEVEL": "trace"})

    assert report.has_errors is False
    assert report.has_warnings is True
    assert _issue_codes(report) == {"unknown_log_level"}


def test_validate_bot_startup_config_reports_missing_owner_chat_id():
    report = validate_bot_startup_config({"TELEGRAM_BOT_TOKEN": "123:legacy"})

    assert report.has_errors is True
    assert "missing_telegram_owner_chat_id" in _issue_codes(report)


def test_validate_bot_startup_config_reports_invalid_owner_chat_id():
    report = validate_bot_startup_config(
        {
            "TELEGRAM_OWNER_CHAT_ID": "bad",
            "TELEGRAM_BOT_TOKEN": "123:legacy",
        }
    )

    assert report.has_errors is True
    assert "invalid_telegram_owner_chat_id" in _issue_codes(report)


def test_validate_bot_startup_config_accepts_valid_single_bot_path():
    report = validate_bot_startup_config(
        {
            "TELEGRAM_OWNER_CHAT_ID": "777",
            "TELEGRAM_BOT_TOKEN": "123:legacy",
        }
    )

    assert report.is_valid is True
    assert report.has_errors is False


def test_validate_bot_startup_config_accepts_valid_multi_bot_path():
    report = validate_bot_startup_config(
        {
            "TELEGRAM_OWNER_CHAT_ID": "777",
            "TELEGRAM_AGENT_TOKENS": (
                "coordinator_agent=TELEGRAM_BOT_TOKEN,"
                "writer_agent=TELEGRAM_WRITER_BOT_TOKEN"
            ),
            "TELEGRAM_BOT_TOKEN": "123:coord",
            "TELEGRAM_WRITER_BOT_TOKEN": "456:writer",
        }
    )

    assert report.is_valid is True
    assert report.has_errors is False


def test_validate_bot_startup_config_reports_invalid_agent_tokens_syntax():
    report = validate_bot_startup_config(
        {
            "TELEGRAM_OWNER_CHAT_ID": "777",
            "TELEGRAM_AGENT_TOKENS": "coordinator_agent",
            "TELEGRAM_BOT_TOKEN": "123:coord",
        }
    )

    assert report.has_errors is True
    assert "invalid_telegram_agent_tokens" in _issue_codes(report)


def test_validate_bot_startup_config_reports_missing_referenced_token_env():
    report = validate_bot_startup_config(
        {
            "TELEGRAM_OWNER_CHAT_ID": "777",
            "TELEGRAM_AGENT_TOKENS": (
                "coordinator_agent=TELEGRAM_BOT_TOKEN,"
                "writer_agent=TELEGRAM_WRITER_BOT_TOKEN"
            ),
            "TELEGRAM_BOT_TOKEN": "123:coord",
        }
    )

    assert report.has_errors is True
    assert "missing_referenced_bot_token_env" in _issue_codes(report)


@pytest.mark.parametrize("raw_value", ("abc", "-1"))
def test_validate_bot_startup_config_reports_invalid_cost_threshold(raw_value: str):
    report = validate_bot_startup_config(
        {
            "TELEGRAM_OWNER_CHAT_ID": "777",
            "TELEGRAM_BOT_TOKEN": "123:legacy",
            "BOT_COST_THRESHOLD_USD": raw_value,
        }
    )

    assert report.has_errors is True
    assert "invalid_bot_cost_threshold_usd" in _issue_codes(report)


def test_validate_bot_startup_config_keeps_legacy_repo_and_worktree_issues_as_warnings(
    tmp_path,
):
    worktree_root = tmp_path / "worktree-file"
    worktree_root.write_text("not a dir", encoding="utf-8")

    report = validate_bot_startup_config(
        {
            "TELEGRAM_OWNER_CHAT_ID": "777",
            "TELEGRAM_BOT_TOKEN": "123:legacy",
            "REPO_PATH": str(tmp_path / "missing-repo"),
            "WORKTREE_ROOT": str(worktree_root),
        }
    )

    assert report.has_errors is False
    assert report.has_warnings is True
    assert "legacy_repo_path_missing" in _issue_codes(report)
    assert "legacy_worktree_root_not_directory" in _issue_codes(report)


def test_validate_web_startup_config_accepts_explicit_state_db_path(tmp_path):
    env = {"STATE_DB_PATH": str(tmp_path / "web.db")}

    report = validate_web_startup_config(env)
    config = WebRuntimeEnvConfig.from_env(env)

    assert report.scope == "web"
    assert report.is_valid is True
    assert config.state_db_source == STATE_DB_SOURCE_ENV


def test_validate_web_startup_config_accepts_bot_state_dir_fallback(tmp_path):
    env = {"BOT_STATE_DIR": str(tmp_path / "legacy-state")}

    report = validate_web_startup_config(env)
    config = WebRuntimeEnvConfig.from_env(env)

    assert report.is_valid is True
    assert config.state_db_source == STATE_DB_SOURCE_LEGACY


def test_validate_web_startup_config_keeps_default_home_path_supported(
    monkeypatch,
    tmp_path,
):
    fake_home = tmp_path / "fake-home"
    fake_home.write_text("home sentinel", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))

    report = validate_web_startup_config({})
    config = WebRuntimeEnvConfig.from_env({})

    assert report.is_valid is True
    assert config.state_db_source == STATE_DB_SOURCE_DEFAULT


def test_raise_for_startup_validation_errors_uses_typed_error():
    report = validate_bot_startup_config({"TELEGRAM_BOT_TOKEN": "123:legacy"})

    with pytest.raises(StartupConfigValidationError) as exc_info:
        raise_for_startup_validation_errors(report)

    assert "missing_telegram_owner_chat_id" in str(exc_info.value)
    assert (
        format_startup_validation_report(report)
        == str(exc_info.value)
    )
