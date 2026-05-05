"""
core/tier_session.py

Step 14b-4: per-chat tier state.

Each Telegram chat keeps its own active tier (ECONOMY / STANDARD / PREMIUM
or any custom tier registered in the TierRegistry). When a chat first asks
the bot to do work, the bot's task_handler checks `needs_choice(chat_id)`
and either prompts for a choice or proceeds with the saved tier.

Storage is in-memory only for MVP. A persistent store (sqlite or json on
disk) is a drop-in replacement later — the API surface here is the
contract.

CONTRACTS:
1. TierSession is frozen; chat_id is a positive int; active_tier may be
   None (means "no tier picked yet" — bot will ask).
2. TierSessionStore is thread-safe; all mutations under a Lock.
3. get_or_create(chat_id) lazily creates a session with active_tier=None.
4. set_active(chat_id, name) requires the name to be present in the
   provided TierRegistry; raises KeyError otherwise.
5. needs_choice(chat_id) returns True iff the active_tier is None
   (bot uses this to decide between "ask for tier" and "go ahead").
6. snapshot() returns a frozen tuple of all sessions (for /admin views,
   debugging, and future persistence).
"""

import threading
import time
from dataclasses import dataclass

from core.model_tier import TierRegistry


@dataclass(frozen=True)
class TierSession:
    chat_id: int
    active_tier: str | None = None
    last_changed_at: float = 0.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.chat_id, bool)
            or not isinstance(self.chat_id, int)
            or self.chat_id <= 0
        ):
            raise ValueError(f"invalid_chat_id:{self.chat_id!r}")
        if self.active_tier is not None and (
            not isinstance(self.active_tier, str)
            or not self.active_tier.strip()
        ):
            raise ValueError("empty_active_tier_string")
        if (
            isinstance(self.last_changed_at, bool)
            or not isinstance(self.last_changed_at, (int, float))
            or self.last_changed_at < 0
        ):
            raise ValueError(
                f"invalid_last_changed_at:{self.last_changed_at!r}"
            )


class TierSessionStore:
    """Thread-safe per-chat tier selection store.

    Used by the Telegram bridge to remember which tier each chat picked,
    so the team doesn't ask "cheap or expensive?" on every single task.
    """

    def __init__(self, registry: TierRegistry) -> None:
        if not isinstance(registry, TierRegistry):
            raise ValueError(
                f"invalid_registry_type:{type(registry).__name__}"
            )
        self._registry = registry
        self._lock = threading.Lock()
        self._by_chat: dict[int, TierSession] = {}

    @property
    def registry(self) -> TierRegistry:
        return self._registry

    def get_or_create(self, chat_id: int) -> TierSession:
        if (
            isinstance(chat_id, bool)
            or not isinstance(chat_id, int)
            or chat_id <= 0
        ):
            raise ValueError(f"invalid_chat_id:{chat_id!r}")
        with self._lock:
            session = self._by_chat.get(chat_id)
            if session is None:
                session = TierSession(chat_id=chat_id)
                self._by_chat[chat_id] = session
            return session

    def set_active(self, chat_id: int, tier_name: str) -> TierSession:
        """Sets the active tier for a chat. Raises KeyError on unknown tier."""
        if (
            isinstance(chat_id, bool)
            or not isinstance(chat_id, int)
            or chat_id <= 0
        ):
            raise ValueError(f"invalid_chat_id:{chat_id!r}")
        if not isinstance(tier_name, str) or not tier_name.strip():
            raise ValueError("empty_tier_name")
        normalised = tier_name.strip()
        # Validate against the registry — raises KeyError if unknown.
        self._registry.get(normalised)
        with self._lock:
            session = TierSession(
                chat_id=chat_id,
                active_tier=normalised,
                last_changed_at=time.time(),
            )
            self._by_chat[chat_id] = session
            return session

    def reset(self, chat_id: int) -> None:
        """Forget the chat's tier choice (next task will ask again)."""
        if (
            isinstance(chat_id, bool)
            or not isinstance(chat_id, int)
            or chat_id <= 0
        ):
            raise ValueError(f"invalid_chat_id:{chat_id!r}")
        with self._lock:
            self._by_chat.pop(chat_id, None)

    def needs_choice(self, chat_id: int) -> bool:
        """True if no tier has been picked for this chat yet."""
        session = self.get_or_create(chat_id)
        return session.active_tier is None

    def active_tier_name(self, chat_id: int) -> str | None:
        return self.get_or_create(chat_id).active_tier

    def snapshot(self) -> tuple[TierSession, ...]:
        with self._lock:
            return tuple(
                self._by_chat[k] for k in sorted(self._by_chat.keys())
            )

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_chat)

    def __contains__(self, chat_id: object) -> bool:
        with self._lock:
            return chat_id in self._by_chat


def format_tier_summary(
    registry: TierRegistry,
    *,
    active_name: str | None = None,
) -> str:
    """Renders a human-readable summary of available tiers for /tier output.

    `active_name` highlights the currently active tier (if known for this
    chat); otherwise falls back to the registry's global active.
    """
    if not isinstance(registry, TierRegistry):
        raise ValueError(
            f"invalid_registry_type:{type(registry).__name__}"
        )
    if active_name is not None and (
        not isinstance(active_name, str) or not active_name.strip()
    ):
        raise ValueError("empty_active_name")

    effective_active = active_name or registry.active_name()
    lines: list[str] = ["💼 Тарифы стека моделей", ""]
    for tier in registry.all():
        marker = "▸ " if tier.name == effective_active else "  "
        lines.append(f"{marker}{tier.name}  ·  ~${tier.estimated_cost_usd:.2f}/задача")
        # Indent description on next line for readability.
        for desc_line in tier.description.split("\n"):
            lines.append(f"     {desc_line}")
        lines.append("")
    # Trim trailing empty line.
    while lines and lines[-1] == "":
        lines.pop()
    lines.append("")
    lines.append("Сменить:  /tier set <имя>")
    lines.append("Сбросить: /tier reset")
    return "\n".join(lines)
