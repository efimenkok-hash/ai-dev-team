from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.agent_personas import default_registry
from core.coordinator_role import COORDINATOR_ROLE
from core.multi_bot_bridge import MultiBotBridge
from core.multi_bot_runtime import BotIdentity, MultiBotRuntimeSpec, PerRoleBotMap
from core.telegram_bridge import BridgeResult, IncomingMessage, TelegramBridge


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
    return PerRoleBotMap(
        {identity.agent_role: identity for identity in identities}
    )


def _runtime_spec(*identities: BotIdentity, source: str) -> MultiBotRuntimeSpec:
    role_map = _role_map(*identities)
    return MultiBotRuntimeSpec(
        primary_bot=role_map.by_role[COORDINATOR_ROLE],
        role_map=role_map,
        source=source,
    )


def _unsafe_runtime_spec(
    *,
    primary_bot: BotIdentity,
    role_map: PerRoleBotMap,
    source: str,
) -> MultiBotRuntimeSpec:
    spec = object.__new__(MultiBotRuntimeSpec)
    object.__setattr__(spec, "primary_bot", primary_bot)
    object.__setattr__(spec, "role_map", role_map)
    object.__setattr__(spec, "source", source)
    return spec


def _primary_bridge() -> TelegramBridge:
    def _send(_out) -> None:
        return None

    return TelegramBridge(
        owner_chat_ids=frozenset({11111}),
        send=_send,
        personas=default_registry(),
        task_handler=lambda _text, _msg: None,
    )


def _unsafe_bridge_with_role(role: str) -> TelegramBridge:
    bridge = object.__new__(TelegramBridge)
    object.__setattr__(bridge, "_coordinator_role", role)
    return bridge


def _msg() -> IncomingMessage:
    return IncomingMessage(
        chat_id=11111,
        user_id=11111,
        message_id=1,
        text="привет",
    )


def _group_msg() -> IncomingMessage:
    return IncomingMessage(
        chat_id=-100123,
        user_id=11111,
        message_id=1,
        text="привет",
    )


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------


def test_multi_bot_bridge_happy_path_legacy_runtime():
    coordinator = _identity(
        token_env_key="TELEGRAM_BOT_TOKEN",
        token="123:legacy",
    )
    bridge = MultiBotBridge(
        runtime_spec=_runtime_spec(
            coordinator,
            source="single_token_legacy",
        ),
        primary_bridge=_primary_bridge(),
    )

    assert bridge.primary_role == COORDINATOR_ROLE
    assert bridge.primary_identity == coordinator
    assert bridge.enabled_roles() == (COORDINATOR_ROLE,)


def test_multi_bot_bridge_happy_path_multi_identity_runtime():
    coordinator = _identity(
        token_env_key="TELEGRAM_BOT_TOKEN",
        token="123:coord",
    )
    writer = _identity(
        "writer_agent",
        token_env_key="TELEGRAM_WRITER_BOT_TOKEN",
        token="456:writer",
    )
    bridge = MultiBotBridge(
        runtime_spec=_runtime_spec(
            coordinator,
            writer,
            source="telegram_agent_tokens",
        ),
        primary_bridge=_primary_bridge(),
    )

    assert bridge.enabled_roles() == (
        COORDINATOR_ROLE,
        "writer_agent",
    )
    assert bridge.is_multi_identity_runtime() is True


def test_multi_bot_bridge_rejects_bad_runtime_spec():
    with pytest.raises(
        ValueError,
        match="invalid_multi_bot_runtime_spec_type:str",
    ):
        MultiBotBridge(  # type: ignore[arg-type]
            runtime_spec="not-a-runtime-spec",
            primary_bridge=_primary_bridge(),
        )


def test_multi_bot_bridge_rejects_bad_primary_bridge():
    coordinator = _identity()
    with pytest.raises(
        ValueError,
        match="invalid_primary_bridge_type:str",
    ):
        MultiBotBridge(  # type: ignore[arg-type]
            runtime_spec=_runtime_spec(
                coordinator,
                source="single_token_legacy",
            ),
            primary_bridge="not-a-bridge",
        )


def test_multi_bot_bridge_rejects_non_coordinator_primary_runtime_identity():
    coordinator = _identity(token="123:coord")
    writer = _identity(
        "writer_agent",
        token_env_key="TELEGRAM_WRITER_BOT_TOKEN",
        token="456:writer",
    )
    invalid_spec = _unsafe_runtime_spec(
        primary_bot=writer,
        role_map=_role_map(coordinator, writer),
        source="telegram_agent_tokens",
    )

    with pytest.raises(
        ValueError,
        match="primary_runtime_identity_must_be_coordinator_agent:writer_agent",
    ):
        MultiBotBridge(
            runtime_spec=invalid_spec,
            primary_bridge=_primary_bridge(),
        )


def test_multi_bot_bridge_rejects_primary_bridge_with_non_coordinator_role():
    coordinator = _identity()
    invalid_bridge = _unsafe_bridge_with_role("writer_agent")

    with pytest.raises(
        ValueError,
        match="primary_bridge_must_use_coordinator_agent:writer_agent",
    ):
        MultiBotBridge(
            runtime_spec=_runtime_spec(
                coordinator,
                source="single_token_legacy",
            ),
            primary_bridge=invalid_bridge,
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_enabled_roles_returns_deterministic_order():
    coordinator = _identity(token="123:coord")
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
    bridge = MultiBotBridge(
        runtime_spec=_runtime_spec(
            writer,
            reviewer,
            coordinator,
            source="telegram_agent_tokens",
        ),
        primary_bridge=_primary_bridge(),
    )

    assert bridge.enabled_roles() == (
        COORDINATOR_ROLE,
        "reviewer_agent",
        "writer_agent",
    )


def test_resolve_identity_supports_coordinator_role():
    coordinator = _identity(token="123:coord")
    bridge = MultiBotBridge(
        runtime_spec=_runtime_spec(
            coordinator,
            source="single_token_legacy",
        ),
        primary_bridge=_primary_bridge(),
    )

    assert bridge.resolve_identity(COORDINATOR_ROLE) == coordinator


def test_resolve_identity_supports_writer_role():
    coordinator = _identity(token="123:coord")
    writer = _identity(
        "writer_agent",
        token_env_key="TELEGRAM_WRITER_BOT_TOKEN",
        token="456:writer",
    )
    bridge = MultiBotBridge(
        runtime_spec=_runtime_spec(
            coordinator,
            writer,
            source="telegram_agent_tokens",
        ),
        primary_bridge=_primary_bridge(),
    )

    assert bridge.resolve_identity("writer_agent") == writer


def test_resolve_identity_rejects_unknown_role():
    coordinator = _identity()
    bridge = MultiBotBridge(
        runtime_spec=_runtime_spec(
            coordinator,
            source="single_token_legacy",
        ),
        primary_bridge=_primary_bridge(),
    )

    with pytest.raises(ValueError, match="unknown_bot_role:ghost_agent"):
        bridge.resolve_identity("ghost_agent")


def test_is_multi_identity_runtime_false_for_legacy_runtime():
    coordinator = _identity()
    bridge = MultiBotBridge(
        runtime_spec=_runtime_spec(
            coordinator,
            source="single_token_legacy",
        ),
        primary_bridge=_primary_bridge(),
    )

    assert bridge.is_multi_identity_runtime() is False


# ---------------------------------------------------------------------------
# handle_incoming
# ---------------------------------------------------------------------------


def test_handle_incoming_coordinator_delegates_to_primary_bridge():
    coordinator = _identity(token="123:coord")
    primary_bridge = _primary_bridge()
    expected = BridgeResult(
        chat_id=11111,
        handled=True,
        reason="delegated",
        sent_count=1,
        extracted_text="привет",
    )
    primary_bridge.handle = MagicMock(return_value=expected)
    bridge = MultiBotBridge(
        runtime_spec=_runtime_spec(
            coordinator,
            source="single_token_legacy",
        ),
        primary_bridge=primary_bridge,
    )
    msg = _msg()

    result = bridge.handle_incoming(COORDINATOR_ROLE, msg)

    assert result is expected
    delegated_msg = primary_bridge.handle.call_args.args[0]
    assert delegated_msg.chat_id == msg.chat_id
    assert delegated_msg.incoming_bot_role == COORDINATOR_ROLE


def test_handle_incoming_secondary_private_dm_delegates_to_primary_bridge():
    coordinator = _identity(token="123:coord")
    writer = _identity(
        "writer_agent",
        token_env_key="TELEGRAM_WRITER_BOT_TOKEN",
        token="456:writer",
    )
    primary_bridge = _primary_bridge()
    expected = BridgeResult(
        chat_id=11111,
        handled=True,
        reason="delegated",
        sent_count=1,
        extracted_text="привет",
    )
    primary_bridge.handle = MagicMock(return_value=expected)
    bridge = MultiBotBridge(
        runtime_spec=_runtime_spec(
            coordinator,
            writer,
            source="telegram_agent_tokens",
        ),
        primary_bridge=primary_bridge,
    )

    result = bridge.handle_incoming("writer_agent", _msg())

    assert result is expected
    delegated_msg = primary_bridge.handle.call_args.args[0]
    assert delegated_msg.chat_id == 11111
    assert delegated_msg.incoming_bot_role == "writer_agent"


def test_handle_incoming_secondary_group_role_returns_not_enabled_without_delegation():
    coordinator = _identity(token="123:coord")
    writer = _identity(
        "writer_agent",
        token_env_key="TELEGRAM_WRITER_BOT_TOKEN",
        token="456:writer",
    )
    primary_bridge = _primary_bridge()
    primary_bridge.handle = MagicMock()
    bridge = MultiBotBridge(
        runtime_spec=_runtime_spec(
            coordinator,
            writer,
            source="telegram_agent_tokens",
        ),
        primary_bridge=primary_bridge,
    )

    result = bridge.handle_incoming("writer_agent", _group_msg())

    assert result.handled is False
    assert result.reason == "secondary_bot_inbound_not_enabled"
    assert result.sent_count == 0
    primary_bridge.handle.assert_not_called()


def test_handle_incoming_rejects_unknown_role():
    coordinator = _identity()
    bridge = MultiBotBridge(
        runtime_spec=_runtime_spec(
            coordinator,
            source="single_token_legacy",
        ),
        primary_bridge=_primary_bridge(),
    )

    with pytest.raises(ValueError, match="unknown_bot_role:ghost_agent"):
        bridge.handle_incoming("ghost_agent", _msg())
