from __future__ import annotations

from dataclasses import dataclass, replace

from core.coordinator_role import COORDINATOR_ROLE
from core.multi_bot_runtime import BotIdentity, MultiBotRuntimeSpec
from core.owner_dm_routing import OwnerDmRoutingService
from core.telegram_bridge import BridgeResult, IncomingMessage, TelegramBridge


@dataclass(frozen=True)
class MultiBotBridge:
    runtime_spec: MultiBotRuntimeSpec
    primary_bridge: TelegramBridge

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_spec, MultiBotRuntimeSpec):
            raise ValueError(
                "invalid_multi_bot_runtime_spec_type:"
                f"{type(self.runtime_spec).__name__}"
            )
        if not isinstance(self.primary_bridge, TelegramBridge):
            raise ValueError(
                "invalid_primary_bridge_type:"
                f"{type(self.primary_bridge).__name__}"
            )
        if self.runtime_spec.primary_bot.agent_role != COORDINATOR_ROLE:
            raise ValueError(
                "primary_runtime_identity_must_be_coordinator_agent:"
                f"{self.runtime_spec.primary_bot.agent_role}"
            )
        if self.primary_bridge.coordinator_role != COORDINATOR_ROLE:
            raise ValueError(
                "primary_bridge_must_use_coordinator_agent:"
                f"{self.primary_bridge.coordinator_role}"
            )
        if self.primary_bridge.coordinator_role != self.runtime_spec.primary_bot.agent_role:
            raise ValueError(
                "primary_bridge_runtime_role_mismatch:"
                f"{self.primary_bridge.coordinator_role}"
                f"!="
                f"{self.runtime_spec.primary_bot.agent_role}"
            )
        object.__setattr__(self, "_owner_dm_routing", OwnerDmRoutingService())

    @property
    def primary_role(self) -> str:
        return self.runtime_spec.primary_bot.agent_role

    @property
    def primary_identity(self) -> BotIdentity:
        return self.runtime_spec.primary_bot

    def enabled_roles(self) -> tuple[str, ...]:
        return tuple(self.runtime_spec.role_map.by_role.keys())

    def resolve_identity(self, role: str) -> BotIdentity:
        if not isinstance(role, str):
            raise ValueError(f"unknown_bot_role:{role!r}")
        normalized_role = role.strip().lower()
        identity = self.runtime_spec.role_map.by_role.get(normalized_role)
        if identity is None:
            raise ValueError(f"unknown_bot_role:{normalized_role}")
        return identity

    def is_multi_identity_runtime(self) -> bool:
        return (
            self.runtime_spec.source == "telegram_agent_tokens"
            and len(self.runtime_spec.role_map.by_role) > 1
        )

    def handle_incoming(
        self,
        agent_role: str,
        msg: IncomingMessage,
    ) -> BridgeResult:
        identity = self.resolve_identity(agent_role)
        delegated_msg = msg
        if identity.agent_role == COORDINATOR_ROLE:
            if msg.incoming_bot_role != COORDINATOR_ROLE:
                delegated_msg = replace(msg, incoming_bot_role=COORDINATOR_ROLE)
            return self.primary_bridge.handle(delegated_msg)
        if self._owner_dm_routing.is_owner_dm_message(msg):
            if msg.incoming_bot_role != identity.agent_role:
                delegated_msg = replace(
                    msg,
                    incoming_bot_role=identity.agent_role,
                )
            return self.primary_bridge.handle(delegated_msg)
        return BridgeResult(
            chat_id=getattr(msg, "chat_id", 0),
            handled=False,
            reason="secondary_bot_inbound_not_enabled",
            sent_count=0,
            extracted_text=None,
        )
