from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.env_layout import (
    STATE_DB_SOURCE_DEFAULT,
    STATE_DB_SOURCE_ENV,
    STATE_DB_SOURCE_LEGACY,
    SUPPORTED_ENV_KEYS,
    BotRuntimeEnvConfig,
    LegacyBootstrapEnvConfig,
    SharedRuntimeEnvConfig,
    WebRuntimeEnvConfig,
)

ENV_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / ".env.example"
_DYNAMIC_TELEGRAM_BOT_TOKEN_RE = re.compile(
    r"^TELEGRAM_[A-Z0-9_]+_BOT_TOKEN$"
)


def _extract_env_example_keys() -> set[str]:
    pattern = re.compile(r"^\s*(?:#\s*)?([A-Z][A-Z0-9_]+)=")
    keys: set[str] = set()
    for line in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match is not None:
            keys.add(match.group(1))
    return keys


def test_shared_runtime_env_config_prefers_canonical_state_db_path(tmp_path):
    config = SharedRuntimeEnvConfig.from_env(
        {
            "STATE_DB_PATH": str(tmp_path / "state.db"),
            "OBS_LOG_PATH": str(tmp_path / "obs.jsonl"),
            "LOG_LEVEL": " debug ",
        }
    )

    assert config.state_db_path == tmp_path / "state.db"
    assert config.state_db_source == STATE_DB_SOURCE_ENV
    assert config.obs_log_path == tmp_path / "obs.jsonl"
    assert config.log_level == "DEBUG"


def test_shared_runtime_env_config_keeps_bot_state_dir_as_legacy_fallback_only(
    tmp_path,
):
    state_dir = tmp_path / "legacy-state"
    config = SharedRuntimeEnvConfig.from_env(
        {"BOT_STATE_DIR": str(state_dir)}
    )
    legacy = LegacyBootstrapEnvConfig.from_env(
        {"BOT_STATE_DIR": str(state_dir)}
    )

    assert config.state_db_path == state_dir / "state.db"
    assert config.state_db_source == STATE_DB_SOURCE_LEGACY
    assert legacy.bot_state_dir == state_dir
    assert legacy.has_state_dir_fallback is True


def test_shared_runtime_env_config_defaults_to_user_home_state_db_path():
    config = SharedRuntimeEnvConfig.from_env({})

    assert config.state_db_source == STATE_DB_SOURCE_DEFAULT
    assert config.state_db_path.name == "state.db"


def test_shared_runtime_env_config_rejects_non_string_state_db_path_value():
    with pytest.raises(
        ValueError,
        match="invalid_env_value_type:STATE_DB_PATH:int",
    ):
        SharedRuntimeEnvConfig.from_env(  # type: ignore[arg-type]
            {"STATE_DB_PATH": 123}
        )


def test_bot_runtime_env_config_keeps_single_bot_compatibility(tmp_path):
    config = BotRuntimeEnvConfig.from_env(
        {
            "STATE_DB_PATH": str(tmp_path / "state.db"),
            "TELEGRAM_OWNER_CHAT_ID": "777",
            "TELEGRAM_BOT_TOKEN": "123:legacy",
            "OPENROUTER_API_KEY": "sk-or-test",
        }
    )

    assert config.telegram_owner_chat_id == "777"
    assert config.telegram_bot_token == "123:legacy"
    assert config.telegram_agent_tokens is None
    assert config.has_single_bot_token is True
    assert config.has_multi_bot_contract is False
    assert config.openrouter_api_key == "sk-or-test"


def test_bot_runtime_env_config_keeps_multi_bot_contract_truthfully(tmp_path):
    config = BotRuntimeEnvConfig.from_env(
        {
            "STATE_DB_PATH": str(tmp_path / "state.db"),
            "TELEGRAM_OWNER_CHAT_ID": "777",
            "TELEGRAM_AGENT_TOKENS": (
                "coordinator_agent=TELEGRAM_BOT_TOKEN,"
                "writer_agent=TELEGRAM_WRITER_BOT_TOKEN"
            ),
            "TELEGRAM_BOT_TOKEN": "123:coord",
            "OPENROUTER_API_KEY": "sk-or-test",
            "OPENAI_API_KEY": "sk-openai-test",
            "BOT_COST_THRESHOLD_USD": "2.5",
        }
    )

    assert config.has_multi_bot_contract is True
    assert config.telegram_agent_tokens is not None
    assert config.telegram_bot_token == "123:coord"
    assert config.openrouter_api_key == "sk-or-test"
    assert config.openai_api_key == "sk-openai-test"
    assert config.bot_cost_threshold_usd_raw == "2.5"


def test_legacy_bootstrap_env_config_does_not_elevate_repo_seed_into_primary_state_truth(
    tmp_path,
):
    repo = tmp_path / "repo"
    worktree_root = tmp_path / "worktrees"
    config = LegacyBootstrapEnvConfig.from_env(
        {
            "REPO_PATH": str(repo),
            "WORKTREE_ROOT": str(worktree_root),
        }
    )
    shared = SharedRuntimeEnvConfig.from_env(
        {
            "REPO_PATH": str(repo),
            "WORKTREE_ROOT": str(worktree_root),
        }
    )

    assert config.repo_path == repo
    assert config.worktree_root == worktree_root
    assert config.has_project_runtime_seed is True
    assert shared.state_db_source == STATE_DB_SOURCE_DEFAULT


def test_web_runtime_env_config_reads_state_db_path_truthfully(tmp_path):
    config = WebRuntimeEnvConfig.from_env(
        {"STATE_DB_PATH": str(tmp_path / "web.db")}
    )

    assert config.state_db_path == tmp_path / "web.db"
    assert config.state_db_source == STATE_DB_SOURCE_ENV


def test_env_example_matches_supported_env_layout_keys():
    keys = _extract_env_example_keys()

    unsupported = sorted(
        key
        for key in keys
        if key not in SUPPORTED_ENV_KEYS
        and _DYNAMIC_TELEGRAM_BOT_TOKEN_RE.fullmatch(key) is None
    )

    assert unsupported == []
    assert "STATE_DB_PATH" in keys
    assert "TELEGRAM_AGENT_TOKENS" in keys
    assert "BOT_STATE_DIR" in keys
