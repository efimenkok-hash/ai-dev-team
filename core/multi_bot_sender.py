from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from core.coordinator_role import COORDINATOR_ROLE
from core.multi_bot_runtime import BotIdentity
from core.telegram_bridge import OutgoingEnvelope

_ROLE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _normalize_role(role: str) -> str:
    if not isinstance(role, str):
        raise ValueError(f"invalid_sender_role_type:{type(role).__name__}")
    normalized = role.strip().lower()
    if not normalized:
        raise ValueError("empty_sender_role")
    if not normalized.isascii():
        raise ValueError(f"non_ascii_sender_role:{normalized}")
    if not _ROLE_ID_RE.fullmatch(normalized):
        raise ValueError(f"invalid_sender_role:{normalized}")
    return normalized


@dataclass(frozen=True)
class RoleBoundSender:
    identity: BotIdentity
    send_envelope: object

    def __post_init__(self) -> None:
        if not isinstance(self.identity, BotIdentity):
            raise ValueError(
                "invalid_role_bound_sender_identity_type:"
                f"{type(self.identity).__name__}"
            )
        if not callable(self.send_envelope):
            raise ValueError("role_bound_send_envelope_not_callable")


@dataclass(frozen=True)
class PerRoleOutboundSender:
    primary_role: str
    senders_by_role: Mapping[str, RoleBoundSender]

    def __post_init__(self) -> None:
        normalized_primary_role = _normalize_role(self.primary_role)
        if normalized_primary_role != COORDINATOR_ROLE:
            raise ValueError(
                "outbound_primary_role_must_be_coordinator_agent:"
                f"{normalized_primary_role}"
            )
        if not isinstance(self.senders_by_role, Mapping):
            raise ValueError(
                "invalid_senders_by_role_type:"
                f"{type(self.senders_by_role).__name__}"
            )
        normalized: dict[str, RoleBoundSender] = {}
        for role in sorted(self.senders_by_role.keys()):
            normalized_role = _normalize_role(role)
            sender = self.senders_by_role[role]
            if not isinstance(sender, RoleBoundSender):
                raise ValueError(
                    "invalid_role_bound_sender_type:"
                    f"{type(sender).__name__}"
                )
            if sender.identity.agent_role != normalized_role:
                raise ValueError(
                    "outbound_sender_role_identity_mismatch:"
                    f"{normalized_role}!={sender.identity.agent_role}"
                )
            normalized[normalized_role] = sender
        if not normalized:
            raise ValueError("empty_outbound_sender_map")
        if COORDINATOR_ROLE not in normalized:
            raise ValueError("missing_primary_outbound_sender:coordinator_agent")
        object.__setattr__(self, "primary_role", normalized_primary_role)
        object.__setattr__(
            self,
            "senders_by_role",
            MappingProxyType(normalized),
        )


class MultiBotOutboundSender:
    def __init__(self, config: PerRoleOutboundSender) -> None:
        if not isinstance(config, PerRoleOutboundSender):
            raise ValueError(
                "invalid_per_role_outbound_sender_type:"
                f"{type(config).__name__}"
            )
        self._config = config

    def enabled_roles(self) -> tuple[str, ...]:
        return tuple(self._config.senders_by_role.keys())

    def resolve_sender(self, role: str) -> RoleBoundSender:
        normalized_role = _normalize_role(role)
        sender = self._config.senders_by_role.get(normalized_role)
        if sender is None:
            raise ValueError(f"unknown_outbound_sender_role:{normalized_role}")
        return sender

    def send(self, envelope: OutgoingEnvelope) -> str:
        if not isinstance(envelope, OutgoingEnvelope):
            raise ValueError(
                "invalid_outgoing_envelope_type:"
                f"{type(envelope).__name__}"
            )
        transport_role = (
            envelope.delivery_role
            if envelope.delivery_role is not None
            else envelope.sender_role
        )
        sender = self._config.senders_by_role.get(transport_role)
        used_role = (
            transport_role
            if sender is not None
            else self._config.primary_role
        )
        resolved_sender = self._config.senders_by_role[used_role]
        resolved_sender.send_envelope(envelope)
        return used_role
