from __future__ import annotations

import pytest

from core.agent_personas import default_registry
from core.coordinator_role import COORDINATOR_ROLE
from core.multi_bot_runtime import (
    BotIdentity,
    MultiBotRuntimeSpec,
    PerRoleBotMap,
    build_multi_bot_runtime_spec,
    parse_agent_token_bindings,
)


def _identity(
    role: str = COORDINATOR_ROLE,
    *,
    bot_id: str | None = None,
    token_env_key: str | None = None,
    token: str = "123:token",
) -> BotIdentity:
    resolved_bot_id = bot_id if bot_id is not None else role
    resolved_env_key = (
        token_env_key
        if token_env_key is not None
        else f"TELEGRAM_{role.upper()}_TOKEN"
    )
    return BotIdentity(
        bot_id=resolved_bot_id,
        agent_role=role,
        token_env_key=resolved_env_key,
        token=token,
    )


def _role_map(*identities: BotIdentity) -> PerRoleBotMap:
    return PerRoleBotMap({identity.agent_role: identity for identity in identities})


# ---------------------------------------------------------------------------
# BotIdentity
# ---------------------------------------------------------------------------


def test_bot_identity_happy_path():
    identity = _identity()
    assert identity.bot_id == COORDINATOR_ROLE
    assert identity.agent_role == COORDINATOR_ROLE


@pytest.mark.parametrize("bad", ["", "   "])
def test_bot_identity_rejects_empty_bot_id(bad: str):
    with pytest.raises(ValueError, match="empty_bot_id"):
        _identity(bot_id=bad)


@pytest.mark.parametrize("bad", ["Координатор", "Coordinator", "bad-id", "UPPER"])
def test_bot_identity_rejects_malformed_bot_id(bad: str):
    with pytest.raises(ValueError):
        _identity(bot_id=bad)


@pytest.mark.parametrize("bad", ["", "   "])
def test_bot_identity_rejects_empty_agent_role(bad: str):
    with pytest.raises(ValueError, match="empty_role_id"):
        _identity(role=bad, bot_id=COORDINATOR_ROLE)


@pytest.mark.parametrize("bad", ["", "   "])
def test_bot_identity_rejects_empty_token_env_key(bad: str):
    with pytest.raises(ValueError, match="empty_token_env_key"):
        _identity(token_env_key=bad)


@pytest.mark.parametrize("bad", ["", "   "])
def test_bot_identity_rejects_empty_token(bad: str):
    with pytest.raises(ValueError):
        _identity(token=bad)


def test_bot_identity_rejects_unknown_role():
    with pytest.raises(ValueError, match="unknown_agent_role:ghost_agent"):
        _identity(role="ghost_agent")


@pytest.mark.parametrize(
    "role",
    ("security_agent", "devops_agent", "data_agent"),
)
def test_bot_identity_rejects_specialist_role_not_runtime_exposed(role: str):
    with pytest.raises(
        ValueError,
        match=fr"runtime_agent_role_not_allowed:{role}",
    ):
        _identity(role=role)


# ---------------------------------------------------------------------------
# PerRoleBotMap
# ---------------------------------------------------------------------------


def test_per_role_bot_map_happy_path():
    coordinator = _identity()
    writer = _identity(
        "writer_agent",
        token_env_key="TELEGRAM_WRITER_BOT_TOKEN",
        token="456:writer",
    )
    role_map = _role_map(writer, coordinator)

    assert tuple(role_map.by_role.keys()) == (COORDINATOR_ROLE, "writer_agent")
    assert role_map.by_role[COORDINATOR_ROLE] == coordinator


def test_per_role_bot_map_rejects_empty_map():
    with pytest.raises(ValueError, match="empty_role_map"):
        PerRoleBotMap({})


def test_per_role_bot_map_rejects_missing_coordinator():
    with pytest.raises(ValueError, match="missing_coordinator_agent"):
        _role_map(
            _identity(
                "writer_agent",
                token_env_key="TELEGRAM_WRITER_BOT_TOKEN",
                token="456:writer",
            )
        )


def test_per_role_bot_map_rejects_role_mismatch():
    coordinator = _identity(role=COORDINATOR_ROLE)
    with pytest.raises(ValueError, match="role_identity_mismatch"):
        PerRoleBotMap({"writer_agent": coordinator})


def test_per_role_bot_map_rejects_duplicate_token_values():
    coordinator = _identity(token="123:shared")
    writer = _identity(
        "writer_agent",
        token_env_key="TELEGRAM_WRITER_BOT_TOKEN",
        token="123:shared",
    )
    with pytest.raises(ValueError, match="duplicate_bot_token"):
        _role_map(coordinator, writer)


def test_per_role_bot_map_normalizes_order_deterministically():
    reviewer = _identity(
        "reviewer_agent",
        token_env_key="TELEGRAM_REVIEWER_BOT_TOKEN",
        token="789:reviewer",
    )
    writer = _identity(
        "writer_agent",
        token_env_key="TELEGRAM_WRITER_BOT_TOKEN",
        token="456:writer",
    )
    coordinator = _identity()

    role_map = PerRoleBotMap(
        {
            "writer_agent": writer,
            COORDINATOR_ROLE: coordinator,
            "reviewer_agent": reviewer,
        }
    )

    assert tuple(role_map.by_role.keys()) == (
        COORDINATOR_ROLE,
        "reviewer_agent",
        "writer_agent",
    )


# ---------------------------------------------------------------------------
# MultiBotRuntimeSpec
# ---------------------------------------------------------------------------


def test_multi_bot_runtime_spec_happy_path_legacy_single_token():
    coordinator = _identity(
        token_env_key="TELEGRAM_BOT_TOKEN",
        token="123:legacy",
    )
    role_map = _role_map(coordinator)

    spec = MultiBotRuntimeSpec(
        primary_bot=coordinator,
        role_map=role_map,
        source="single_token_legacy",
    )

    assert spec.primary_bot == coordinator
    assert spec.source == "single_token_legacy"


def test_multi_bot_runtime_spec_happy_path_multi_token():
    coordinator = _identity(
        token_env_key="TELEGRAM_BOT_TOKEN",
        token="123:coord",
    )
    writer = _identity(
        "writer_agent",
        token_env_key="TELEGRAM_WRITER_BOT_TOKEN",
        token="456:writer",
    )
    role_map = _role_map(coordinator, writer)

    spec = MultiBotRuntimeSpec(
        primary_bot=coordinator,
        role_map=role_map,
        source="telegram_agent_tokens",
    )

    assert spec.primary_bot.agent_role == COORDINATOR_ROLE
    assert tuple(spec.role_map.by_role.keys()) == (
        COORDINATOR_ROLE,
        "writer_agent",
    )


def test_multi_bot_runtime_spec_rejects_bad_primary_bot():
    role_map = _role_map(_identity())
    with pytest.raises(ValueError, match="invalid_primary_bot_type"):
        MultiBotRuntimeSpec(  # type: ignore[arg-type]
            primary_bot="not-a-bot",
            role_map=role_map,
            source="single_token_legacy",
        )


def test_multi_bot_runtime_spec_rejects_bad_role_map():
    coordinator = _identity()
    with pytest.raises(ValueError, match="invalid_role_map_type"):
        MultiBotRuntimeSpec(  # type: ignore[arg-type]
            primary_bot=coordinator,
            role_map="not-a-map",
            source="single_token_legacy",
        )


def test_multi_bot_runtime_spec_rejects_invalid_source():
    coordinator = _identity()
    role_map = _role_map(coordinator)
    with pytest.raises(ValueError, match="invalid_multi_bot_runtime_source"):
        MultiBotRuntimeSpec(
            primary_bot=coordinator,
            role_map=role_map,
            source="unknown",
        )


def test_multi_bot_runtime_spec_rejects_primary_bot_not_present():
    coordinator = _identity(token="123:coord")
    writer = _identity(
        "writer_agent",
        token_env_key="TELEGRAM_WRITER_BOT_TOKEN",
        token="456:writer",
    )
    role_map = _role_map(coordinator)
    with pytest.raises(ValueError, match="primary_bot_missing_from_role_map"):
        MultiBotRuntimeSpec(
            primary_bot=writer,
            role_map=role_map,
            source="telegram_agent_tokens",
        )


def test_multi_bot_runtime_spec_rejects_non_coordinator_primary_bot():
    coordinator = _identity(token="123:coord")
    writer = _identity(
        "writer_agent",
        token_env_key="TELEGRAM_WRITER_BOT_TOKEN",
        token="456:writer",
    )
    role_map = _role_map(coordinator, writer)
    with pytest.raises(ValueError, match="primary_bot_must_be_coordinator_agent"):
        MultiBotRuntimeSpec(
            primary_bot=writer,
            role_map=role_map,
            source="telegram_agent_tokens",
        )


# ---------------------------------------------------------------------------
# parse_agent_token_bindings
# ---------------------------------------------------------------------------


def test_parse_agent_token_bindings_happy_path_multiple_roles():
    parsed = parse_agent_token_bindings(
        "writer_agent=TELEGRAM_WRITER_BOT_TOKEN,"
        "coordinator_agent=TELEGRAM_BOT_TOKEN"
    )
    assert parsed == (
        (COORDINATOR_ROLE, "TELEGRAM_BOT_TOKEN"),
        ("writer_agent", "TELEGRAM_WRITER_BOT_TOKEN"),
    )


def test_parse_agent_token_bindings_trims_whitespace():
    parsed = parse_agent_token_bindings(
        "  writer_agent=TELEGRAM_WRITER_BOT_TOKEN , "
        "coordinator_agent=TELEGRAM_BOT_TOKEN  "
    )
    assert parsed[0] == (COORDINATOR_ROLE, "TELEGRAM_BOT_TOKEN")
    assert parsed[1] == ("writer_agent", "TELEGRAM_WRITER_BOT_TOKEN")


def test_parse_agent_token_bindings_rejects_empty_input():
    with pytest.raises(ValueError, match="empty_telegram_agent_tokens"):
        parse_agent_token_bindings("   ")


def test_parse_agent_token_bindings_rejects_malformed_entry():
    with pytest.raises(ValueError, match="malformed_telegram_agent_token_entry"):
        parse_agent_token_bindings("coordinator_agent")


def test_parse_agent_token_bindings_rejects_duplicate_role():
    with pytest.raises(ValueError, match="duplicate_role:coordinator_agent"):
        parse_agent_token_bindings(
            "coordinator_agent=TELEGRAM_BOT_TOKEN,"
            "coordinator_agent=TELEGRAM_BOT_TOKEN_TWO"
        )


def test_parse_agent_token_bindings_rejects_empty_env_key():
    with pytest.raises(ValueError, match="empty_token_env_key"):
        parse_agent_token_bindings("coordinator_agent= ")


def test_parse_agent_token_bindings_rejects_invalid_role_id():
    with pytest.raises(ValueError, match="invalid_role_id"):
        parse_agent_token_bindings("bad-role=TELEGRAM_BAD_TOKEN")


def test_parse_agent_token_bindings_rejects_invalid_env_key_format():
    with pytest.raises(ValueError, match="invalid_token_env_key"):
        parse_agent_token_bindings("coordinator_agent=telegram_bot_token")


# ---------------------------------------------------------------------------
# build_multi_bot_runtime_spec
# ---------------------------------------------------------------------------


def test_build_multi_bot_runtime_spec_legacy_mode_returns_coordinator_only_spec():
    spec = build_multi_bot_runtime_spec({"TELEGRAM_BOT_TOKEN": "123:legacy"})
    assert spec is not None
    assert spec.source == "single_token_legacy"
    assert spec.primary_bot.agent_role == COORDINATOR_ROLE
    assert tuple(spec.role_map.by_role.keys()) == (COORDINATOR_ROLE,)


def test_build_multi_bot_runtime_spec_returns_none_when_no_tokens_exist():
    assert build_multi_bot_runtime_spec({}) is None


def test_build_multi_bot_runtime_spec_returns_none_for_blank_legacy_token():
    assert build_multi_bot_runtime_spec({"TELEGRAM_BOT_TOKEN": "   "}) is None


def test_build_multi_bot_runtime_spec_multi_bot_mode():
    spec = build_multi_bot_runtime_spec(
        {
            "TELEGRAM_AGENT_TOKENS": (
                "writer_agent=TELEGRAM_WRITER_BOT_TOKEN,"
                "coordinator_agent=TELEGRAM_BOT_TOKEN"
            ),
            "TELEGRAM_BOT_TOKEN": "123:coord",
            "TELEGRAM_WRITER_BOT_TOKEN": "456:writer",
        }
    )
    assert spec is not None
    assert spec.source == "telegram_agent_tokens"
    assert spec.primary_bot.agent_role == COORDINATOR_ROLE
    assert tuple(spec.role_map.by_role.keys()) == (
        COORDINATOR_ROLE,
        "writer_agent",
    )


def test_build_multi_bot_runtime_spec_requires_coordinator_mapping():
    with pytest.raises(
        ValueError,
        match="telegram_agent_tokens_missing_coordinator_agent",
    ):
        build_multi_bot_runtime_spec(
            {
                "TELEGRAM_AGENT_TOKENS": (
                    "writer_agent=TELEGRAM_WRITER_BOT_TOKEN"
                ),
                "TELEGRAM_WRITER_BOT_TOKEN": "456:writer",
            }
        )


def test_build_multi_bot_runtime_spec_rejects_missing_referenced_env_var():
    with pytest.raises(
        ValueError,
        match="telegram_agent_token_env_missing:TELEGRAM_WRITER_BOT_TOKEN",
    ):
        build_multi_bot_runtime_spec(
            {
                "TELEGRAM_AGENT_TOKENS": (
                    "coordinator_agent=TELEGRAM_BOT_TOKEN,"
                    "writer_agent=TELEGRAM_WRITER_BOT_TOKEN"
                ),
                "TELEGRAM_BOT_TOKEN": "123:coord",
            }
        )


def test_build_multi_bot_runtime_spec_rejects_blank_referenced_token():
    with pytest.raises(
        ValueError,
        match="telegram_agent_token_empty:TELEGRAM_WRITER_BOT_TOKEN",
    ):
        build_multi_bot_runtime_spec(
            {
                "TELEGRAM_AGENT_TOKENS": (
                    "coordinator_agent=TELEGRAM_BOT_TOKEN,"
                    "writer_agent=TELEGRAM_WRITER_BOT_TOKEN"
                ),
                "TELEGRAM_BOT_TOKEN": "123:coord",
                "TELEGRAM_WRITER_BOT_TOKEN": "   ",
            }
        )


def test_build_multi_bot_runtime_spec_rejects_unknown_role_not_in_persona_registry():
    with pytest.raises(
        ValueError,
        match="telegram_agent_role_unknown:ghost_agent",
    ):
        build_multi_bot_runtime_spec(
            {
                "TELEGRAM_AGENT_TOKENS": (
                    "coordinator_agent=TELEGRAM_BOT_TOKEN,"
                    "ghost_agent=TELEGRAM_GHOST_BOT_TOKEN"
                ),
                "TELEGRAM_BOT_TOKEN": "123:coord",
                "TELEGRAM_GHOST_BOT_TOKEN": "999:ghost",
            }
        )


def test_build_multi_bot_runtime_spec_accepts_partial_role_coverage():
    spec = build_multi_bot_runtime_spec(
        {
            "TELEGRAM_AGENT_TOKENS": (
                "coordinator_agent=TELEGRAM_BOT_TOKEN,"
                "reviewer_agent=TELEGRAM_REVIEWER_BOT_TOKEN"
            ),
            "TELEGRAM_BOT_TOKEN": "123:coord",
            "TELEGRAM_REVIEWER_BOT_TOKEN": "789:reviewer",
        }
    )
    assert spec is not None
    assert tuple(spec.role_map.by_role.keys()) == (
        COORDINATOR_ROLE,
        "reviewer_agent",
    )


def test_build_multi_bot_runtime_spec_wraps_invalid_binding_syntax():
    with pytest.raises(
        ValueError,
        match="telegram_agent_tokens_invalid:malformed_telegram_agent_token_entry",
    ):
        build_multi_bot_runtime_spec(
            {"TELEGRAM_AGENT_TOKENS": "coordinator_agent"}
        )


def test_build_multi_bot_runtime_spec_rejects_blank_agent_tokens_when_present():
    with pytest.raises(
        ValueError,
        match="telegram_agent_tokens_invalid:empty_telegram_agent_tokens",
    ):
        build_multi_bot_runtime_spec(
            {
                "TELEGRAM_AGENT_TOKENS": "   ",
                "TELEGRAM_BOT_TOKEN": "123:legacy",
            }
        )


def test_build_multi_bot_runtime_spec_uses_explicit_persona_registry_validation():
    personas = default_registry()
    spec = build_multi_bot_runtime_spec(
        {
            "TELEGRAM_AGENT_TOKENS": "coordinator_agent=TELEGRAM_BOT_TOKEN",
            "TELEGRAM_BOT_TOKEN": "123:coord",
        },
        personas=personas,
    )
    assert spec is not None
    assert spec.primary_bot.agent_role == COORDINATOR_ROLE


@pytest.mark.parametrize(
    ("role", "env_key", "token"),
    (
        ("security_agent", "TELEGRAM_SECURITY_BOT_TOKEN", "777:security"),
        ("devops_agent", "TELEGRAM_DEVOPS_BOT_TOKEN", "778:devops"),
        ("data_agent", "TELEGRAM_DATA_BOT_TOKEN", "779:data"),
    ),
)
def test_build_multi_bot_runtime_spec_rejects_specialist_runtime_identity_activation(
    role: str,
    env_key: str,
    token: str,
):
    with pytest.raises(
        ValueError,
        match=fr"telegram_agent_role_not_runtime_exposed:{role}",
    ):
        build_multi_bot_runtime_spec(
            {
                "TELEGRAM_AGENT_TOKENS": (
                    f"coordinator_agent=TELEGRAM_BOT_TOKEN,{role}={env_key}"
                ),
                "TELEGRAM_BOT_TOKEN": "123:coord",
                env_key: token,
            }
        )


def test_build_multi_bot_runtime_spec_rejects_bad_persona_registry_type():
    with pytest.raises(ValueError, match="invalid_persona_registry_type"):
        build_multi_bot_runtime_spec(  # type: ignore[arg-type]
            {"TELEGRAM_BOT_TOKEN": "123:legacy"},
            personas="not-a-registry",
        )
