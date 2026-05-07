"""Tests for core.tier_session (Step 14b-4: per-chat tier state)."""

import threading
import time

import pytest

from core.model_tier import (
    DEFAULT_TIERS,
    ModelTierName,
    TierRegistry,
    default_registry,
)
from core.state_db import StateDB
from core.tier_session import (
    TierSession,
    TierSessionStore,
    format_tier_summary,
    migrate_legacy_tier_sessions_json,
)

# ---------------------------------------------------------------------------
# TierSession dataclass
# ---------------------------------------------------------------------------


def test_session_happy_path():
    s = TierSession(chat_id=42)
    assert s.chat_id == 42
    assert s.active_tier is None
    assert s.last_changed_at == 0.0


def test_session_with_active_tier():
    s = TierSession(chat_id=1, active_tier="STANDARD", last_changed_at=12.5)
    assert s.active_tier == "STANDARD"
    assert s.last_changed_at == 12.5


def test_session_is_frozen():
    s = TierSession(chat_id=1)
    with pytest.raises(Exception):
        s.chat_id = 2  # type: ignore[misc]


@pytest.mark.parametrize("bad", [0, -1, True, False, "1", 1.5, None])
def test_session_rejects_invalid_chat_id(bad):
    with pytest.raises(ValueError, match="invalid_chat_id"):
        TierSession(chat_id=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["", "   "])
def test_session_rejects_empty_active_tier_string(bad):
    with pytest.raises(ValueError, match="empty_active_tier_string"):
        TierSession(chat_id=1, active_tier=bad)


def test_session_rejects_non_string_active_tier():
    with pytest.raises(ValueError, match="empty_active_tier_string"):
        TierSession(chat_id=1, active_tier=42)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [-1, -0.5, True, "1.0", None])
def test_session_rejects_invalid_last_changed_at(bad):
    with pytest.raises(ValueError, match="invalid_last_changed_at"):
        TierSession(chat_id=1, last_changed_at=bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TierSessionStore — construction
# ---------------------------------------------------------------------------


def test_store_construction_happy_path():
    reg = default_registry()
    store = TierSessionStore(reg)
    assert len(store) == 0
    assert store.registry is reg


def test_store_rejects_non_registry():
    with pytest.raises(ValueError, match="invalid_registry_type"):
        TierSessionStore("not a registry")  # type: ignore[arg-type]


def test_store_rejects_non_state_db():
    with pytest.raises(ValueError, match="state_db_must_be_state_db_or_none"):
        TierSessionStore(default_registry(), state_db="bad")  # type: ignore[arg-type]


def test_store_rejects_mixing_json_and_state_db(tmp_path):
    db = StateDB(tmp_path / "state.db")
    with pytest.raises(ValueError, match="cannot_mix_persistence_path_and_state_db"):
        TierSessionStore(
            default_registry(),
            persistence_path=tmp_path / "tier_sessions.json",
            state_db=db,
        )


# ---------------------------------------------------------------------------
# get_or_create
# ---------------------------------------------------------------------------


def test_get_or_create_lazily_creates_session():
    store = TierSessionStore(default_registry())
    s = store.get_or_create(123)
    assert s.chat_id == 123
    assert s.active_tier is None
    assert 123 in store


def test_get_or_create_returns_same_session_on_repeat():
    store = TierSessionStore(default_registry())
    s1 = store.get_or_create(7)
    s2 = store.get_or_create(7)
    assert s1 is s2  # same instance — frozen, identity is fine
    assert len(store) == 1


@pytest.mark.parametrize("bad", [0, -1, True, "x", 1.5, None])
def test_get_or_create_rejects_invalid_chat_id(bad):
    store = TierSessionStore(default_registry())
    with pytest.raises(ValueError, match="invalid_chat_id"):
        store.get_or_create(bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# set_active
# ---------------------------------------------------------------------------


def test_set_active_records_tier_and_timestamp():
    store = TierSessionStore(default_registry())
    before = time.time()
    s = store.set_active(42, ModelTierName.PREMIUM.value)
    after = time.time()
    assert s.active_tier == "PREMIUM"
    assert before <= s.last_changed_at <= after
    assert store.active_tier_name(42) == "PREMIUM"


def test_set_active_strips_whitespace():
    store = TierSessionStore(default_registry())
    s = store.set_active(1, "  STANDARD  ")
    assert s.active_tier == "STANDARD"


def test_set_active_overwrites_previous_choice():
    store = TierSessionStore(default_registry())
    store.set_active(1, "ECONOMY")
    s = store.set_active(1, "PREMIUM")
    assert s.active_tier == "PREMIUM"
    # Only one session per chat
    assert len(store) == 1


def test_set_active_rejects_unknown_tier():
    store = TierSessionStore(default_registry())
    with pytest.raises(KeyError, match="unknown_tier"):
        store.set_active(1, "WHATEVER")


@pytest.mark.parametrize("bad", ["", "   "])
def test_set_active_rejects_empty_tier_name(bad):
    store = TierSessionStore(default_registry())
    with pytest.raises(ValueError, match="empty_tier_name"):
        store.set_active(1, bad)


def test_set_active_rejects_non_string_tier():
    store = TierSessionStore(default_registry())
    with pytest.raises(ValueError, match="empty_tier_name"):
        store.set_active(1, 42)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0, -1, True])
def test_set_active_rejects_invalid_chat_id(bad):
    store = TierSessionStore(default_registry())
    with pytest.raises(ValueError, match="invalid_chat_id"):
        store.set_active(bad, "STANDARD")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------


def test_reset_removes_session():
    store = TierSessionStore(default_registry())
    store.set_active(1, "STANDARD")
    assert 1 in store
    store.reset(1)
    assert 1 not in store


def test_reset_idempotent_for_unknown_chat():
    store = TierSessionStore(default_registry())
    # Should not raise
    store.reset(999)


@pytest.mark.parametrize("bad", [0, -1, True])
def test_reset_rejects_invalid_chat_id(bad):
    store = TierSessionStore(default_registry())
    with pytest.raises(ValueError, match="invalid_chat_id"):
        store.reset(bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# needs_choice / active_tier_name
# ---------------------------------------------------------------------------


def test_needs_choice_true_for_fresh_chat():
    store = TierSessionStore(default_registry())
    assert store.needs_choice(1) is True


def test_needs_choice_false_after_set_active():
    store = TierSessionStore(default_registry())
    store.set_active(1, "STANDARD")
    assert store.needs_choice(1) is False


def test_needs_choice_true_after_reset():
    store = TierSessionStore(default_registry())
    store.set_active(1, "STANDARD")
    store.reset(1)
    assert store.needs_choice(1) is True


def test_active_tier_name_none_when_unset():
    store = TierSessionStore(default_registry())
    assert store.active_tier_name(1) is None


def test_active_tier_name_returns_choice():
    store = TierSessionStore(default_registry())
    store.set_active(1, "PREMIUM")
    assert store.active_tier_name(1) == "PREMIUM"


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def test_snapshot_empty():
    store = TierSessionStore(default_registry())
    assert store.snapshot() == ()


def test_snapshot_returns_sorted_tuple():
    store = TierSessionStore(default_registry())
    store.set_active(7, "STANDARD")
    store.set_active(3, "ECONOMY")
    store.set_active(99, "PREMIUM")
    snap = store.snapshot()
    assert tuple(s.chat_id for s in snap) == (3, 7, 99)


def test_snapshot_returns_tuple_type():
    store = TierSessionStore(default_registry())
    store.get_or_create(1)
    snap = store.snapshot()
    assert isinstance(snap, tuple)


# ---------------------------------------------------------------------------
# thread safety
# ---------------------------------------------------------------------------


def test_concurrent_set_active_no_corruption():
    store = TierSessionStore(default_registry())

    def worker(chat_id: int):
        for _ in range(50):
            store.set_active(chat_id, "STANDARD")
            store.active_tier_name(chat_id)

    threads = [
        threading.Thread(target=worker, args=(cid,))
        for cid in range(1, 11)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(store) == 10
    for cid in range(1, 11):
        assert store.active_tier_name(cid) == "STANDARD"


# ---------------------------------------------------------------------------
# format_tier_summary
# ---------------------------------------------------------------------------


def test_format_summary_lists_all_default_tiers():
    reg = default_registry()
    text = format_tier_summary(reg)
    for tier in DEFAULT_TIERS:
        assert tier.name in text


def test_format_summary_marks_active_with_arrow():
    reg = default_registry()
    text = format_tier_summary(reg, active_name="PREMIUM")
    # Active tier line starts with ▸
    lines = text.split("\n")
    premium_line = next(line for line in lines if "PREMIUM" in line and "$" in line)
    assert premium_line.startswith("▸")


def test_format_summary_uses_registry_active_when_no_override():
    reg = default_registry()
    # default active is STANDARD
    text = format_tier_summary(reg, active_name=None)
    lines = text.split("\n")
    standard_line = next(line for line in lines if "STANDARD" in line and "$" in line)
    assert standard_line.startswith("▸")


def test_format_summary_includes_cost_and_description():
    reg = default_registry()
    text = format_tier_summary(reg)
    # All three default tiers carry an estimated cost in dollars.
    assert "$0.20" in text
    assert "$1.00" in text
    assert "$4.00" in text


def test_format_summary_includes_help_footer():
    text = format_tier_summary(default_registry())
    assert "/tier set" in text
    assert "/tier reset" in text


def test_format_summary_rejects_non_registry():
    with pytest.raises(ValueError, match="invalid_registry_type"):
        format_tier_summary("not a registry")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["", "   "])
def test_format_summary_rejects_empty_active_name(bad):
    with pytest.raises(ValueError, match="empty_active_name"):
        format_tier_summary(default_registry(), active_name=bad)


def test_format_summary_works_with_custom_registry():
    """User-defined tier registries must also work for /tier output."""
    reg = TierRegistry(DEFAULT_TIERS, active_name="ECONOMY")
    text = format_tier_summary(reg)
    lines = text.split("\n")
    economy_line = next(line for line in lines if "ECONOMY" in line and "$" in line)
    assert economy_line.startswith("▸")


# ---------------------------------------------------------------------------
# Persistence (Step 19)
# ---------------------------------------------------------------------------


def test_persistence_path_optional_in_memory_default():
    """Without persistence_path, store works as before — pure in-memory."""
    s = TierSessionStore(default_registry())
    assert s.persistence_path is None
    s.set_active(1, "ECONOMY")
    assert s.active_tier_name(1) == "ECONOMY"


def test_persistence_path_must_be_path_or_none():
    with pytest.raises(ValueError, match="persistence_path_must_be_path_or_none"):
        TierSessionStore(default_registry(), persistence_path="not a path")  # type: ignore[arg-type]


def test_persistence_set_active_writes_file(tmp_path):
    state_file = tmp_path / "tier_sessions.json"
    s = TierSessionStore(default_registry(), persistence_path=state_file)
    assert not state_file.exists()  # empty store does NOT write
    s.set_active(42, "PREMIUM")
    assert state_file.exists()
    import json
    raw = json.loads(state_file.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    sessions = raw["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["chat_id"] == 42
    assert sessions[0]["active_tier"] == "PREMIUM"


def test_persistence_round_trip(tmp_path):
    """Restart simulation: write via store A, read via store B."""
    state_file = tmp_path / "tier_sessions.json"

    a = TierSessionStore(default_registry(), persistence_path=state_file)
    a.set_active(1, "ECONOMY")
    a.set_active(2, "STANDARD")
    a.set_active(3, "PREMIUM")

    # Fresh store reads the same file.
    b = TierSessionStore(default_registry(), persistence_path=state_file)
    assert b.active_tier_name(1) == "ECONOMY"
    assert b.active_tier_name(2) == "STANDARD"
    assert b.active_tier_name(3) == "PREMIUM"


def test_persistence_reset_writes_file(tmp_path):
    state_file = tmp_path / "tier_sessions.json"
    s = TierSessionStore(default_registry(), persistence_path=state_file)
    s.set_active(42, "PREMIUM")
    s.set_active(7, "ECONOMY")
    s.reset(42)

    # New store should see only chat 7
    b = TierSessionStore(default_registry(), persistence_path=state_file)
    assert b.active_tier_name(42) is None
    assert b.active_tier_name(7) == "ECONOMY"


def test_persistence_corrupt_json_starts_fresh(tmp_path):
    """Corrupt or unreadable JSON must NOT crash the bot — start with empty state."""
    state_file = tmp_path / "tier_sessions.json"
    state_file.write_text("this is not valid json {{{ ", encoding="utf-8")

    s = TierSessionStore(default_registry(), persistence_path=state_file)
    assert len(s) == 0  # corrupt file ignored, empty store


def test_persistence_unknown_tier_dropped_on_load(tmp_path):
    """If a saved tier name no longer exists in the registry, drop that entry
    cleanly rather than failing or restoring zombie state."""
    import json
    state_file = tmp_path / "tier_sessions.json"
    state_file.write_text(json.dumps({
        "schema_version": 1,
        "sessions": [
            {"chat_id": 1, "active_tier": "PREMIUM", "last_changed_at": 1234.5},
            {"chat_id": 2, "active_tier": "GHOST_TIER", "last_changed_at": 2345.6},
        ],
    }), encoding="utf-8")

    s = TierSessionStore(default_registry(), persistence_path=state_file)
    assert s.active_tier_name(1) == "PREMIUM"
    assert s.active_tier_name(2) is None  # GHOST_TIER dropped


def test_persistence_missing_file_starts_fresh(tmp_path):
    """No file at construction time → empty store, no crash."""
    state_file = tmp_path / "does_not_exist.json"
    s = TierSessionStore(default_registry(), persistence_path=state_file)
    assert len(s) == 0
    # And first set_active creates the file.
    s.set_active(1, "ECONOMY")
    assert state_file.exists()


def test_persistence_creates_parent_directory(tmp_path):
    """If parent directory doesn't exist, save creates it."""
    state_file = tmp_path / "nested" / "deeper" / "tier_sessions.json"
    s = TierSessionStore(default_registry(), persistence_path=state_file)
    s.set_active(42, "STANDARD")
    assert state_file.exists()


def test_persistence_atomic_via_tmp_replace(tmp_path):
    """Save uses .tmp + os.replace so a crash mid-write doesn't corrupt the
    main file. Verify by checking that an existing valid file is replaced
    only with another valid file."""
    state_file = tmp_path / "tier_sessions.json"
    s = TierSessionStore(default_registry(), persistence_path=state_file)
    s.set_active(1, "ECONOMY")
    first_content = state_file.read_text(encoding="utf-8")
    s.set_active(1, "PREMIUM")
    second_content = state_file.read_text(encoding="utf-8")
    assert first_content != second_content
    # And no .tmp left behind on disk
    tmp_files = list(state_file.parent.glob("*.tmp"))
    assert tmp_files == []


def test_persistence_io_errors_swallowed_after_construction(tmp_path):
    """If the disk goes read-only mid-operation, set_active still updates
    in-memory state — disk is best-effort durability."""
    state_file = tmp_path / "tier_sessions.json"
    s = TierSessionStore(default_registry(), persistence_path=state_file)
    s.set_active(1, "ECONOMY")

    # Make the file unwritable to simulate disk issues.
    # Then calling set_active again must not raise — in-memory must update.
    import os as _os
    state_file.chmod(0o444)  # read-only
    state_file.parent.chmod(0o555)  # read+execute, no write
    try:
        s.set_active(1, "PREMIUM")
        assert s.active_tier_name(1) == "PREMIUM"  # in-memory still works
    finally:
        # Restore so tmp_path can be cleaned up.
        state_file.parent.chmod(0o755)
        state_file.chmod(0o644)
        _ = _os  # silence ruff unused-import in CI (noop)


# ---------------------------------------------------------------------------
# SQLite-backed persistence
# ---------------------------------------------------------------------------


def test_state_db_round_trip(tmp_path):
    db = StateDB(tmp_path / "state.db")
    a = TierSessionStore(default_registry(), state_db=db)
    a.set_active(1, "ECONOMY")
    a.set_active(2, "STANDARD")

    b = TierSessionStore(default_registry(), state_db=db)
    assert b.active_tier_name(1) == "ECONOMY"
    assert b.active_tier_name(2) == "STANDARD"


def test_state_db_loads_existing_rows_into_snapshot(tmp_path):
    db = StateDB(tmp_path / "state.db")
    db.set_tier(7, "STANDARD", last_changed_at=7.0)
    db.set_tier(3, "ECONOMY", last_changed_at=3.0)

    store = TierSessionStore(default_registry(), state_db=db)
    snap = store.snapshot()

    assert tuple(s.chat_id for s in snap) == (3, 7)
    assert snap[0].active_tier == "ECONOMY"
    assert snap[1].active_tier == "STANDARD"


def test_state_db_reset_persists(tmp_path):
    db = StateDB(tmp_path / "state.db")
    store = TierSessionStore(default_registry(), state_db=db)
    store.set_active(1, "PREMIUM")
    store.reset(1)

    restarted = TierSessionStore(default_registry(), state_db=db)
    assert restarted.active_tier_name(1) is None


def test_state_db_unknown_tier_dropped_on_load(tmp_path):
    db = StateDB(tmp_path / "state.db")
    db.set_tier(1, "PREMIUM", last_changed_at=1.0)
    db.set_tier(2, "GHOST_TIER", last_changed_at=2.0)

    store = TierSessionStore(default_registry(), state_db=db)
    assert store.active_tier_name(1) == "PREMIUM"
    assert store.active_tier_name(2) is None


def test_migrate_legacy_tier_sessions_json_imports_and_deletes_file(tmp_path):
    import json

    legacy = tmp_path / "tier_sessions.json"
    legacy.write_text(json.dumps({
        "schema_version": 1,
        "sessions": [
            {"chat_id": 1, "active_tier": "ECONOMY", "last_changed_at": 11.0},
            {"chat_id": 2, "active_tier": "PREMIUM", "last_changed_at": 22.0},
        ],
    }), encoding="utf-8")
    db = StateDB(tmp_path / "state.db")

    imported = migrate_legacy_tier_sessions_json(
        default_registry(),
        persistence_path=legacy,
        state_db=db,
    )

    assert imported == 2
    assert db.get_tier(1) == "ECONOMY"
    assert db.get_tier(2) == "PREMIUM"
    assert not legacy.exists()


def test_migrate_legacy_tier_sessions_json_keeps_corrupt_file(tmp_path):
    legacy = tmp_path / "tier_sessions.json"
    legacy.write_text("{not-json", encoding="utf-8")
    db = StateDB(tmp_path / "state.db")

    imported = migrate_legacy_tier_sessions_json(
        default_registry(),
        persistence_path=legacy,
        state_db=db,
    )

    assert imported == 0
    assert legacy.exists()
    assert db.list_tiers() == ()


def test_migrate_legacy_tier_sessions_json_does_not_overwrite_existing_rows(tmp_path):
    import json

    legacy = tmp_path / "tier_sessions.json"
    legacy.write_text(json.dumps({
        "schema_version": 1,
        "sessions": [
            {"chat_id": 1, "active_tier": "ECONOMY", "last_changed_at": 11.0},
            {"chat_id": 2, "active_tier": "STANDARD", "last_changed_at": 22.0},
            {"chat_id": 3, "active_tier": "GHOST_TIER", "last_changed_at": 33.0},
        ],
    }), encoding="utf-8")
    db = StateDB(tmp_path / "state.db")
    db.set_tier(1, "PREMIUM", last_changed_at=5.0)

    imported = migrate_legacy_tier_sessions_json(
        default_registry(),
        persistence_path=legacy,
        state_db=db,
    )

    assert imported == 1
    assert db.get_tier(1) == "PREMIUM"
    assert db.get_tier(2) == "STANDARD"
    assert db.get_tier(3) is None
    assert not legacy.exists()
