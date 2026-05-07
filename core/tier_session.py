"""
core/tier_session.py

Step 14b-4: per-chat tier state.
Step 19  : optional JSON-file persistence so bot restart doesn't drop choices.

Each Telegram chat keeps its own active tier (ECONOMY / STANDARD / PREMIUM
or any custom tier registered in the TierRegistry). When a chat first asks
the bot to do work, the bot's task_handler checks `needs_choice(chat_id)`
and either prompts for a choice or proceeds with the saved tier.

Persistence (Step 19): pass `persistence_path=Path(...)` to the constructor
and every mutation atomically writes the current state to that file. On
construction, the store loads any existing file (silent recovery on corrupt
JSON — bot starts with empty state, never crashes).

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
7. When persistence_path is set: every set_active and reset triggers an
   atomic save (write to .tmp + os.replace). I/O errors are swallowed so
   the in-memory state is always source of truth — disk is best-effort
   durability. A corrupt JSON file at startup is silently dropped.
"""

import contextlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from core.model_tier import TierRegistry
from core.state_db import StateDB

_PERSISTENCE_SCHEMA_VERSION = 1


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

    def __init__(
        self,
        registry: TierRegistry,
        *,
        persistence_path: Path | None = None,
        state_db: StateDB | None = None,
    ) -> None:
        if not isinstance(registry, TierRegistry):
            raise ValueError(
                f"invalid_registry_type:{type(registry).__name__}"
            )
        if persistence_path is not None and not isinstance(persistence_path, Path):
            raise ValueError(
                f"persistence_path_must_be_path_or_none:"
                f"{type(persistence_path).__name__}"
            )
        if state_db is not None and not isinstance(state_db, StateDB):
            raise ValueError(
                f"state_db_must_be_state_db_or_none:{type(state_db).__name__}"
            )
        if persistence_path is not None and state_db is not None:
            raise ValueError("cannot_mix_persistence_path_and_state_db")
        self._registry = registry
        self._lock = threading.Lock()
        self._by_chat: dict[int, TierSession] = {}
        self._persistence_path = persistence_path
        self._state_db = state_db
        if state_db is not None:
            self._load_from_state_db()
        elif persistence_path is not None:
            self._load_from_disk()

    @property
    def registry(self) -> TierRegistry:
        return self._registry

    @property
    def persistence_path(self) -> Path | None:
        return self._persistence_path

    @property
    def state_db(self) -> StateDB | None:
        return self._state_db

    # -------------------------------------------------------------------
    # Persistence helpers (atomic, error-swallowing).
    # -------------------------------------------------------------------

    def _save_to_disk_locked(self) -> None:
        """Atomic JSON dump. Call ONLY while holding self._lock."""
        if self._persistence_path is None:
            return
        try:
            data = {
                "schema_version": _PERSISTENCE_SCHEMA_VERSION,
                "sessions": [
                    {
                        "chat_id": s.chat_id,
                        "active_tier": s.active_tier,
                        "last_changed_at": s.last_changed_at,
                    }
                    for s in self._by_chat.values()
                    if s.active_tier is not None  # don't persist unset chats
                ],
            }
            self._persistence_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persistence_path.with_suffix(
                self._persistence_path.suffix + ".tmp"
            )
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self._persistence_path)
        except (OSError, TypeError, ValueError):
            # Persistence is best-effort — never break the bot because the
            # disk is full / read-only / etc. Caller logs separately if needed.
            pass

    def _load_from_disk(self) -> None:
        """Best-effort restore. Silently ignores missing/corrupt files.

        Drops entries whose tier is unknown to the current registry — that
        way a renamed/removed tier from a previous session doesn't leave
        zombie state lying around.
        """
        if self._persistence_path is None or not self._persistence_path.exists():
            return
        try:
            raw = self._persistence_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict) or data.get("schema_version") != _PERSISTENCE_SCHEMA_VERSION:
            return
        sessions = data.get("sessions")
        if not isinstance(sessions, list):
            return
        with self._lock:
            for entry in sessions:
                if not isinstance(entry, dict):
                    continue
                try:
                    chat_id = entry["chat_id"]
                    tier = entry["active_tier"]
                    ts = entry["last_changed_at"]
                except (KeyError, TypeError):
                    continue
                # Drop entries whose tier no longer exists.
                if tier is None:
                    continue
                try:
                    self._registry.get(tier)
                except (KeyError, TypeError):
                    continue
                try:
                    self._by_chat[int(chat_id)] = TierSession(
                        chat_id=int(chat_id),
                        active_tier=tier,
                        last_changed_at=float(ts),
                    )
                except (ValueError, TypeError):
                    continue

    def _load_from_state_db(self) -> None:
        if self._state_db is None:
            return
        with self._lock:
            for chat_id, tier_name, last_changed_at in self._state_db.list_tiers():
                try:
                    self._registry.get(tier_name)
                except (KeyError, TypeError):
                    continue
                try:
                    self._by_chat[chat_id] = TierSession(
                        chat_id=chat_id,
                        active_tier=tier_name,
                        last_changed_at=last_changed_at,
                    )
                except (ValueError, TypeError):
                    continue

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
            if self._state_db is not None:
                self._state_db.set_tier(
                    chat_id,
                    normalised,
                    last_changed_at=session.last_changed_at,
                )
            else:
                self._save_to_disk_locked()
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
            if self._state_db is not None:
                self._state_db.reset_tier(chat_id)
            else:
                self._save_to_disk_locked()

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


def migrate_legacy_tier_sessions_json(
    registry: TierRegistry,
    *,
    persistence_path: Path,
    state_db: StateDB,
) -> int:
    """Import legacy tier_sessions.json into StateDB, then delete the file.

    Best-effort semantics mirror the old JSON loader:
      - missing / corrupt / wrong-schema file -> no-op, file kept
      - invalid rows / unknown tiers -> skipped
      - existing StateDB rows win over legacy JSON rows
      - on successful parse, the legacy file is deleted best-effort
    """
    if not isinstance(registry, TierRegistry):
        raise ValueError(
            f"invalid_registry_type:{type(registry).__name__}"
        )
    if not isinstance(persistence_path, Path):
        raise ValueError(
            "persistence_path_must_be_path"
        )
    if not isinstance(state_db, StateDB):
        raise ValueError(
            f"state_db_must_be_state_db:{type(state_db).__name__}"
        )
    if not persistence_path.exists() or not persistence_path.is_file():
        return 0
    try:
        raw = persistence_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return 0
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != _PERSISTENCE_SCHEMA_VERSION
    ):
        return 0
    sessions = data.get("sessions")
    if not isinstance(sessions, list):
        return 0

    imported = 0
    for entry in sessions:
        if not isinstance(entry, dict):
            continue
        try:
            chat_id = int(entry["chat_id"])
            active_tier = entry["active_tier"]
            last_changed_at = float(entry["last_changed_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if active_tier is None:
            continue
        try:
            registry.get(active_tier)
            session = TierSession(
                chat_id=chat_id,
                active_tier=active_tier,
                last_changed_at=last_changed_at,
            )
        except (KeyError, TypeError, ValueError):
            continue
        if state_db.get_tier(session.chat_id) is not None:
            continue
        state_db.set_tier(
            session.chat_id,
            session.active_tier,
            last_changed_at=session.last_changed_at,
        )
        imported += 1

    with contextlib.suppress(OSError):
        os.remove(persistence_path)
    return imported


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
