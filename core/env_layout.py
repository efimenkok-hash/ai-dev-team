from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def default_state_db_path() -> Path:
    return Path("~/.ai-dev-team/state.db").expanduser()


DEFAULT_STATE_DB_PATH = default_state_db_path()

STATE_DB_SOURCE_ENV = "env_state_db_path"
STATE_DB_SOURCE_LEGACY = "legacy_bot_state_dir"
STATE_DB_SOURCE_DEFAULT = "default_home"
VALID_STATE_DB_SOURCES = frozenset(
    {
        STATE_DB_SOURCE_ENV,
        STATE_DB_SOURCE_LEGACY,
        STATE_DB_SOURCE_DEFAULT,
    }
)

CANONICAL_SHARED_ENV_KEYS = frozenset(
    {
        "STATE_DB_PATH",
        "OBS_LOG_PATH",
        "LOG_LEVEL",
    }
)
CANONICAL_BOT_ENV_KEYS = frozenset(
    {
        "TELEGRAM_OWNER_CHAT_ID",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_AGENT_TOKENS",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "BOT_COST_THRESHOLD_USD",
    }
)
CANONICAL_WEB_ENV_KEYS = frozenset({"STATE_DB_PATH"})
LEGACY_COMPAT_ENV_KEYS = frozenset(
    {
        "BOT_STATE_DIR",
        "REPO_PATH",
        "WORKTREE_ROOT",
    }
)
EXPERIMENTAL_ENV_KEYS = frozenset({"AI_DEV_TEAM_REAL_LLM"})
SUPPORTED_ENV_KEYS = frozenset().union(
    CANONICAL_SHARED_ENV_KEYS,
    CANONICAL_BOT_ENV_KEYS,
    CANONICAL_WEB_ENV_KEYS,
    LEGACY_COMPAT_ENV_KEYS,
    EXPERIMENTAL_ENV_KEYS,
)


def _resolve_env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    if env is None:
        return os.environ
    if not isinstance(env, Mapping):
        raise ValueError("env_must_be_mapping")
    return env


def read_optional_env_text(
    env: Mapping[str, str] | None,
    key: str,
) -> str | None:
    resolved_env = _resolve_env(env)
    if not isinstance(key, str) or not key.strip():
        raise ValueError("empty_env_key")
    value = resolved_env.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(
            f"invalid_env_value_type:{key}:{type(value).__name__}"
        )
    normalized = value.strip()
    return normalized or None


def _read_optional_path(
    env: Mapping[str, str] | None,
    key: str,
) -> Path | None:
    value = read_optional_env_text(env, key)
    if value is None:
        return None
    return Path(value).expanduser()


@dataclass(frozen=True)
class LegacyBootstrapEnvConfig:
    bot_state_dir: Path | None = None
    repo_path: Path | None = None
    worktree_root: Path | None = None

    def __post_init__(self) -> None:
        for field_name in ("bot_state_dir", "repo_path", "worktree_root"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, Path):
                raise ValueError(
                    f"invalid_{field_name}_type:{type(value).__name__}"
                )

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> LegacyBootstrapEnvConfig:
        resolved_env = _resolve_env(env)
        return cls(
            bot_state_dir=_read_optional_path(resolved_env, "BOT_STATE_DIR"),
            repo_path=_read_optional_path(resolved_env, "REPO_PATH"),
            worktree_root=_read_optional_path(resolved_env, "WORKTREE_ROOT"),
        )

    @property
    def has_state_dir_fallback(self) -> bool:
        return self.bot_state_dir is not None

    @property
    def has_project_runtime_seed(self) -> bool:
        return self.repo_path is not None


@dataclass(frozen=True)
class SharedRuntimeEnvConfig:
    state_db_path: Path
    state_db_source: str
    obs_log_path: Path | None = None
    log_level: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state_db_path, Path):
            raise ValueError(
                f"invalid_state_db_path_type:{type(self.state_db_path).__name__}"
            )
        if (
            not isinstance(self.state_db_source, str)
            or self.state_db_source not in VALID_STATE_DB_SOURCES
        ):
            raise ValueError(f"invalid_state_db_source:{self.state_db_source!r}")
        if self.obs_log_path is not None and not isinstance(self.obs_log_path, Path):
            raise ValueError(
                f"invalid_obs_log_path_type:{type(self.obs_log_path).__name__}"
            )
        if self.log_level is not None:
            if not isinstance(self.log_level, str) or not self.log_level.strip():
                raise ValueError("invalid_log_level")
            object.__setattr__(self, "log_level", self.log_level.strip().upper())

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> SharedRuntimeEnvConfig:
        resolved_env = _resolve_env(env)
        legacy = LegacyBootstrapEnvConfig.from_env(resolved_env)
        state_db_path = _read_optional_path(resolved_env, "STATE_DB_PATH")
        if state_db_path is not None:
            state_db_source = STATE_DB_SOURCE_ENV
        elif legacy.bot_state_dir is not None:
            state_db_path = legacy.bot_state_dir / "state.db"
            state_db_source = STATE_DB_SOURCE_LEGACY
        else:
            state_db_path = default_state_db_path()
            state_db_source = STATE_DB_SOURCE_DEFAULT
        return cls(
            state_db_path=state_db_path,
            state_db_source=state_db_source,
            obs_log_path=_read_optional_path(resolved_env, "OBS_LOG_PATH"),
            log_level=read_optional_env_text(resolved_env, "LOG_LEVEL"),
        )


@dataclass(frozen=True)
class BotRuntimeEnvConfig:
    shared: SharedRuntimeEnvConfig
    legacy: LegacyBootstrapEnvConfig
    telegram_owner_chat_id: str | None = None
    telegram_bot_token: str | None = None
    telegram_agent_tokens: str | None = None
    openrouter_api_key: str | None = None
    openai_api_key: str | None = None
    bot_cost_threshold_usd_raw: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.shared, SharedRuntimeEnvConfig):
            raise ValueError(
                "invalid_shared_runtime_env_config_type:"
                f"{type(self.shared).__name__}"
            )
        if not isinstance(self.legacy, LegacyBootstrapEnvConfig):
            raise ValueError(
                "invalid_legacy_bootstrap_env_config_type:"
                f"{type(self.legacy).__name__}"
            )
        for field_name in (
            "telegram_owner_chat_id",
            "telegram_bot_token",
            "telegram_agent_tokens",
            "openrouter_api_key",
            "openai_api_key",
            "bot_cost_threshold_usd_raw",
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise ValueError(
                    f"invalid_{field_name}_type:{type(value).__name__}"
                )

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> BotRuntimeEnvConfig:
        resolved_env = _resolve_env(env)
        return cls(
            shared=SharedRuntimeEnvConfig.from_env(resolved_env),
            legacy=LegacyBootstrapEnvConfig.from_env(resolved_env),
            telegram_owner_chat_id=read_optional_env_text(
                resolved_env,
                "TELEGRAM_OWNER_CHAT_ID",
            ),
            telegram_bot_token=read_optional_env_text(
                resolved_env,
                "TELEGRAM_BOT_TOKEN",
            ),
            telegram_agent_tokens=read_optional_env_text(
                resolved_env,
                "TELEGRAM_AGENT_TOKENS",
            ),
            openrouter_api_key=read_optional_env_text(
                resolved_env,
                "OPENROUTER_API_KEY",
            ),
            openai_api_key=read_optional_env_text(
                resolved_env,
                "OPENAI_API_KEY",
            ),
            bot_cost_threshold_usd_raw=read_optional_env_text(
                resolved_env,
                "BOT_COST_THRESHOLD_USD",
            ),
        )

    @property
    def has_multi_bot_contract(self) -> bool:
        return self.telegram_agent_tokens is not None

    @property
    def has_single_bot_token(self) -> bool:
        return self.telegram_bot_token is not None


@dataclass(frozen=True)
class WebRuntimeEnvConfig:
    shared: SharedRuntimeEnvConfig

    def __post_init__(self) -> None:
        if not isinstance(self.shared, SharedRuntimeEnvConfig):
            raise ValueError(
                "invalid_shared_runtime_env_config_type:"
                f"{type(self.shared).__name__}"
            )

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> WebRuntimeEnvConfig:
        return cls(shared=SharedRuntimeEnvConfig.from_env(env))

    @property
    def state_db_path(self) -> Path:
        return self.shared.state_db_path

    @property
    def state_db_source(self) -> str:
        return self.shared.state_db_source


def resolve_state_db_path_from_env(
    env: Mapping[str, str] | None = None,
) -> Path:
    return SharedRuntimeEnvConfig.from_env(env).state_db_path


def resolve_legacy_tier_sessions_path_from_env(
    env: Mapping[str, str] | None = None,
) -> Path | None:
    legacy = LegacyBootstrapEnvConfig.from_env(env)
    if legacy.bot_state_dir is None:
        return None
    return legacy.bot_state_dir / "tier_sessions.json"
