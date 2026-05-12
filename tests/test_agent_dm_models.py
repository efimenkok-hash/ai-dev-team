"""Tests for core.agent_dm_models."""

from __future__ import annotations

import pytest

from core.agent_dm_models import (
    VALID_AGENT_DM_CHAT_PROVIDERS,
    VALID_AGENT_DM_SESSION_STATUSES,
    AgentDmSession,
)


def _session(**overrides: object) -> AgentDmSession:
    data: dict[str, object] = {
        "owner_user_id": 101,
        "project_id": "alpha_project",
        "agent_role": "writer_agent",
        "thread_bot_role": "writer_agent",
        "dm_chat_id": 101,
        "chat_provider": "telegram",
        "status": "active",
        "created_at": 1000.0,
        "last_interaction_at": 1005.0,
    }
    data.update(overrides)
    return AgentDmSession(**data)


def test_valid_agent_dm_constants_are_stable():
    assert VALID_AGENT_DM_CHAT_PROVIDERS == {"telegram"}
    assert VALID_AGENT_DM_SESSION_STATUSES == {"active", "closed"}


def test_agent_dm_session_happy_path_normalizes_fields():
    session = _session(
        project_id="  Alpha_Project  ",
        agent_role="  Writer_Agent  ",
        thread_bot_role="  Reviewer_Agent  ",
        chat_provider="  Telegram  ",
        status="  Closed  ",
        created_at=1234,
        last_interaction_at=1235,
    )

    assert session.project_id == "alpha_project"
    assert session.agent_role == "writer_agent"
    assert session.thread_bot_role == "reviewer_agent"
    assert session.chat_provider == "telegram"
    assert session.status == "closed"
    assert session.created_at == 1234.0
    assert session.last_interaction_at == 1235.0


def test_agent_dm_session_is_frozen():
    session = _session()
    with pytest.raises(Exception):
        session.status = "closed"  # type: ignore[misc]


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "7"])
def test_agent_dm_session_rejects_invalid_owner_user_id(bad: object):
    with pytest.raises(ValueError, match="invalid_owner_user_id"):
        _session(owner_user_id=bad)


@pytest.mark.parametrize(
    "bad",
    ["", "  ", None, "alpha-project", "123alpha", "русский"],
)
def test_agent_dm_session_rejects_invalid_project_id(bad: object):
    with pytest.raises(ValueError):
        _session(project_id=bad)


@pytest.mark.parametrize(
    ("field_name", "bad"),
    [
        ("agent_role", ""),
        ("agent_role", "writer-agent"),
        ("agent_role", "русский"),
        ("thread_bot_role", ""),
        ("thread_bot_role", "reviewer-agent"),
        ("thread_bot_role", "русский"),
    ],
)
def test_agent_dm_session_rejects_invalid_role_fields(
    field_name: str,
    bad: object,
):
    with pytest.raises(ValueError):
        _session(**{field_name: bad})


@pytest.mark.parametrize("bad", [0, -1, True, 2.5, "7"])
def test_agent_dm_session_rejects_invalid_dm_chat_id(bad: object):
    with pytest.raises(ValueError, match="invalid_dm_chat_id"):
        _session(dm_chat_id=bad)


@pytest.mark.parametrize("bad", ["", "  ", None, "discord", "русский"])
def test_agent_dm_session_rejects_invalid_chat_provider(bad: object):
    with pytest.raises(ValueError):
        _session(chat_provider=bad)


@pytest.mark.parametrize("bad", ["", "  ", None, "paused", "русский"])
def test_agent_dm_session_rejects_invalid_status(bad: object):
    with pytest.raises(ValueError):
        _session(status=bad)


@pytest.mark.parametrize(
    ("field_name", "bad"),
    [
        ("created_at", 0),
        ("created_at", -1),
        ("created_at", True),
        ("created_at", float("nan")),
        ("created_at", float("inf")),
        ("last_interaction_at", 0),
        ("last_interaction_at", -1),
        ("last_interaction_at", True),
        ("last_interaction_at", float("-inf")),
    ],
)
def test_agent_dm_session_rejects_invalid_timestamps(
    field_name: str,
    bad: object,
):
    with pytest.raises(ValueError, match=f"invalid_{field_name}"):
        _session(**{field_name: bad})


def test_agent_dm_session_rejects_last_interaction_before_created_at():
    with pytest.raises(ValueError, match="last_interaction_at_before_created_at"):
        _session(created_at=100.0, last_interaction_at=99.0)


def test_agent_dm_session_rejects_telegram_session_with_dm_chat_mismatch():
    with pytest.raises(
        ValueError,
        match="telegram_dm_chat_must_match_owner_user_id:202!=101",
    ):
        _session(dm_chat_id=202)
