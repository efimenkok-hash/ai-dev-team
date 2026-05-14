from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from core.agent_personas import PersonaRegistry, default_registry
from core.agent_role_catalog import is_runtime_exposed_agent_role
from core.coordinator_role import COORDINATOR_ROLE

VALID_MULTI_BOT_RUNTIME_SOURCES = frozenset(
    {"single_token_legacy", "telegram_agent_tokens"}
)

_BOT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ROLE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


def _require_personas(
    personas: PersonaRegistry | None,
) -> PersonaRegistry:
    if personas is None:
        return default_registry()
    if not isinstance(personas, PersonaRegistry):
        raise ValueError(
            f"invalid_persona_registry_type:{type(personas).__name__}"
        )
    return personas


def _normalize_bot_id(bot_id: str) -> str:
    if not isinstance(bot_id, str):
        raise ValueError(f"invalid_bot_id_type:{type(bot_id).__name__}")
    normalized = bot_id.strip()
    if not normalized:
        raise ValueError("empty_bot_id")
    if not normalized.isascii():
        raise ValueError(f"non_ascii_bot_id:{normalized}")
    if normalized.lower() != normalized:
        raise ValueError(f"non_lowercase_bot_id:{normalized}")
    if not _BOT_ID_RE.fullmatch(normalized):
        raise ValueError(f"invalid_bot_id:{normalized}")
    return normalized


def _normalize_role_id(role: str) -> str:
    if not isinstance(role, str):
        raise ValueError(f"invalid_role_id_type:{type(role).__name__}")
    normalized = role.strip().lower()
    if not normalized:
        raise ValueError("empty_role_id")
    if not normalized.isascii():
        raise ValueError(f"non_ascii_role_id:{normalized}")
    if not _ROLE_ID_RE.fullmatch(normalized):
        raise ValueError(f"invalid_role_id:{normalized}")
    return normalized


def _normalize_token_env_key(token_env_key: str) -> str:
    if not isinstance(token_env_key, str):
        raise ValueError(
            f"invalid_token_env_key_type:{type(token_env_key).__name__}"
        )
    normalized = token_env_key.strip()
    if not normalized:
        raise ValueError("empty_token_env_key")
    if not _ENV_KEY_RE.fullmatch(normalized):
        raise ValueError(f"invalid_token_env_key:{normalized}")
    return normalized


def _normalize_token(token: str) -> str:
    if not isinstance(token, str):
        raise ValueError(f"invalid_token_type:{type(token).__name__}")
    if token != token.strip():
        raise ValueError("token_has_surrounding_whitespace")
    if not token:
        raise ValueError("empty_token")
    return token


@dataclass(frozen=True)
class BotIdentity:
    bot_id: str
    agent_role: str
    token_env_key: str
    token: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "bot_id", _normalize_bot_id(self.bot_id))
        normalized_role = _normalize_role_id(self.agent_role)
        personas = default_registry()
        if normalized_role not in personas:
            raise ValueError(f"unknown_agent_role:{normalized_role}")
        if not is_runtime_exposed_agent_role(normalized_role):
            raise ValueError(
                f"runtime_agent_role_not_allowed:{normalized_role}"
            )
        object.__setattr__(self, "agent_role", normalized_role)
        object.__setattr__(
            self,
            "token_env_key",
            _normalize_token_env_key(self.token_env_key),
        )
        object.__setattr__(self, "token", _normalize_token(self.token))


@dataclass(frozen=True)
class PerRoleBotMap:
    by_role: Mapping[str, BotIdentity]

    def __post_init__(self) -> None:
        if not isinstance(self.by_role, Mapping):
            raise ValueError(
                "invalid_per_role_bot_map_type:"
                f"{type(self.by_role).__name__}"
            )
        normalized: dict[str, BotIdentity] = {}
        seen_bot_ids: set[str] = set()
        seen_tokens: dict[str, str] = {}
        for role in sorted(self.by_role.keys()):
            normalized_role = _normalize_role_id(role)
            identity = self.by_role[role]
            if not isinstance(identity, BotIdentity):
                raise ValueError(
                    "invalid_bot_identity_type:"
                    f"{type(identity).__name__}"
                )
            if identity.agent_role != normalized_role:
                raise ValueError(
                    "role_identity_mismatch:"
                    f"{normalized_role}!={identity.agent_role}"
                )
            if identity.bot_id in seen_bot_ids:
                raise ValueError(f"duplicate_bot_id:{identity.bot_id}")
            prior_bot_id = seen_tokens.get(identity.token)
            if prior_bot_id is not None and prior_bot_id != identity.bot_id:
                raise ValueError(
                    "duplicate_bot_token:"
                    f"{prior_bot_id}:{identity.bot_id}"
                )
            seen_bot_ids.add(identity.bot_id)
            seen_tokens[identity.token] = identity.bot_id
            normalized[normalized_role] = identity
        if not normalized:
            raise ValueError("empty_role_map")
        if COORDINATOR_ROLE not in normalized:
            raise ValueError("missing_coordinator_agent")
        object.__setattr__(self, "by_role", MappingProxyType(normalized))


@dataclass(frozen=True)
class MultiBotRuntimeSpec:
    primary_bot: BotIdentity
    role_map: PerRoleBotMap
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.primary_bot, BotIdentity):
            raise ValueError(
                "invalid_primary_bot_type:"
                f"{type(self.primary_bot).__name__}"
            )
        if not isinstance(self.role_map, PerRoleBotMap):
            raise ValueError(
                "invalid_role_map_type:"
                f"{type(self.role_map).__name__}"
            )
        if (
            not isinstance(self.source, str)
            or self.source not in VALID_MULTI_BOT_RUNTIME_SOURCES
        ):
            raise ValueError(f"invalid_multi_bot_runtime_source:{self.source!r}")
        if self.primary_bot.agent_role not in self.role_map.by_role:
            raise ValueError(
                "primary_bot_missing_from_role_map:"
                f"{self.primary_bot.agent_role}"
            )
        if self.role_map.by_role[self.primary_bot.agent_role] != self.primary_bot:
            raise ValueError("primary_bot_identity_mismatch")
        if self.primary_bot.agent_role != COORDINATOR_ROLE:
            raise ValueError(
                "primary_bot_must_be_coordinator_agent:"
                f"{self.primary_bot.agent_role}"
            )
        if self.source == "single_token_legacy":
            if tuple(self.role_map.by_role.keys()) != (COORDINATOR_ROLE,):
                raise ValueError(
                    "single_token_legacy_requires_coordinator_only_role_map"
                )
        elif not self.role_map.by_role:
            raise ValueError("telegram_agent_tokens_requires_non_empty_role_map")


def parse_agent_token_bindings(raw: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, str):
        raise ValueError(f"invalid_telegram_agent_tokens_type:{type(raw).__name__}")
    if not raw.strip():
        raise ValueError("empty_telegram_agent_tokens")
    parsed: dict[str, str] = {}
    for entry in raw.split(","):
        normalized_entry = entry.strip()
        if not normalized_entry:
            raise ValueError("empty_telegram_agent_token_entry")
        if "=" not in normalized_entry:
            raise ValueError(f"malformed_telegram_agent_token_entry:{normalized_entry}")
        role_text, env_key_text = normalized_entry.split("=", 1)
        role = _normalize_role_id(role_text)
        env_key = _normalize_token_env_key(env_key_text)
        if role in parsed:
            raise ValueError(f"duplicate_role:{role}")
        parsed[role] = env_key
    return tuple((role, parsed[role]) for role in sorted(parsed))


def build_multi_bot_runtime_spec(
    env: Mapping[str, str],
    personas: PersonaRegistry | None = None,
) -> MultiBotRuntimeSpec | None:
    if not isinstance(env, Mapping):
        raise ValueError("env_must_be_mapping")
    resolved_personas = _require_personas(personas)

    if "TELEGRAM_AGENT_TOKENS" not in env:
        legacy_token = env.get("TELEGRAM_BOT_TOKEN")
        if not isinstance(legacy_token, str):
            return None
        normalized_legacy_token = legacy_token.strip()
        if not normalized_legacy_token:
            return None
        coordinator_identity = BotIdentity(
            bot_id=COORDINATOR_ROLE,
            agent_role=COORDINATOR_ROLE,
            token_env_key="TELEGRAM_BOT_TOKEN",
            token=normalized_legacy_token,
        )
        role_map = PerRoleBotMap({COORDINATOR_ROLE: coordinator_identity})
        return MultiBotRuntimeSpec(
            primary_bot=coordinator_identity,
            role_map=role_map,
            source="single_token_legacy",
        )
    raw_bindings = env.get("TELEGRAM_AGENT_TOKENS")
    if not isinstance(raw_bindings, str):
        raise ValueError("telegram_agent_tokens_invalid:invalid_telegram_agent_tokens_type")

    try:
        bindings = parse_agent_token_bindings(raw_bindings)
    except ValueError as exc:
        raise ValueError(f"telegram_agent_tokens_invalid:{exc}") from exc

    by_role: dict[str, BotIdentity] = {}
    for role, token_env_key in bindings:
        if role not in resolved_personas:
            raise ValueError(f"telegram_agent_role_unknown:{role}")
        if not is_runtime_exposed_agent_role(role):
            raise ValueError(
                f"telegram_agent_role_not_runtime_exposed:{role}"
            )
        raw_token = env.get(token_env_key)
        if raw_token is None:
            raise ValueError(f"telegram_agent_token_env_missing:{token_env_key}")
        if not isinstance(raw_token, str):
            raise ValueError(f"telegram_agent_token_env_missing:{token_env_key}")
        normalized_token = raw_token.strip()
        if not normalized_token:
            raise ValueError(f"telegram_agent_token_empty:{token_env_key}")
        by_role[role] = BotIdentity(
            bot_id=role,
            agent_role=role,
            token_env_key=token_env_key,
            token=normalized_token,
        )

    if COORDINATOR_ROLE not in by_role:
        raise ValueError("telegram_agent_tokens_missing_coordinator_agent")
    role_map = PerRoleBotMap(by_role)
    return MultiBotRuntimeSpec(
        primary_bot=role_map.by_role[COORDINATOR_ROLE],
        role_map=role_map,
        source="telegram_agent_tokens",
    )
