from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.telegram_bridge import IncomingMessage

_ROLE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _normalize_role_id(role: str) -> str:
    if not isinstance(role, str):
        raise ValueError(f"invalid_incoming_bot_role_type:{type(role).__name__}")
    normalized = role.strip().lower()
    if not normalized:
        raise ValueError("empty_incoming_bot_role")
    if not normalized.isascii():
        raise ValueError(f"non_ascii_incoming_bot_role:{normalized}")
    if not _ROLE_ID_RE.fullmatch(normalized):
        raise ValueError(f"invalid_incoming_bot_role:{normalized}")
    return normalized


@dataclass(frozen=True)
class OwnerDmRoutingContext:
    chat_id: int
    user_id: int
    incoming_bot_role: str

    def __post_init__(self) -> None:
        if not isinstance(self.chat_id, int) or isinstance(self.chat_id, bool):
            raise ValueError(f"invalid_owner_dm_chat_id:{self.chat_id!r}")
        if self.chat_id <= 0:
            raise ValueError(f"invalid_owner_dm_chat_id:{self.chat_id!r}")
        if not isinstance(self.user_id, int) or isinstance(self.user_id, bool):
            raise ValueError(f"invalid_owner_dm_user_id:{self.user_id!r}")
        if self.user_id <= 0:
            raise ValueError(f"invalid_owner_dm_user_id:{self.user_id!r}")
        if self.chat_id != self.user_id:
            raise ValueError(
                "owner_dm_requires_private_chat_shape:"
                f"{self.chat_id}!={self.user_id}"
            )
        object.__setattr__(
            self,
            "incoming_bot_role",
            _normalize_role_id(self.incoming_bot_role),
        )


class OwnerDmRoutingService:
    def is_owner_dm_message(self, msg: IncomingMessage) -> bool:
        if not hasattr(msg, "chat_id") or not hasattr(msg, "user_id"):
            return False
        if not isinstance(msg.chat_id, int) or isinstance(msg.chat_id, bool):
            return False
        if not isinstance(msg.user_id, int) or isinstance(msg.user_id, bool):
            return False
        return msg.chat_id > 0 and msg.user_id > 0 and msg.chat_id == msg.user_id

    def build_context(self, msg: IncomingMessage) -> OwnerDmRoutingContext:
        if (
            not hasattr(msg, "chat_id")
            or not hasattr(msg, "user_id")
            or not hasattr(msg, "incoming_bot_role")
        ):
            raise ValueError(
                "invalid_owner_dm_incoming_message_type:"
                f"{type(msg).__name__}"
            )
        return OwnerDmRoutingContext(
            chat_id=msg.chat_id,
            user_id=msg.user_id,
            incoming_bot_role=msg.incoming_bot_role,
        )

    def resolve_delivery_role(
        self,
        context: OwnerDmRoutingContext,
        sender_role: str,
    ) -> str:
        if not isinstance(context, OwnerDmRoutingContext):
            raise ValueError(
                "invalid_owner_dm_routing_context_type:"
                f"{type(context).__name__}"
            )
        if not isinstance(sender_role, str) or not sender_role.strip():
            raise ValueError("invalid_owner_dm_sender_role")
        return context.incoming_bot_role
