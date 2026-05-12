"""Tests for core.agent_dm_models."""

from __future__ import annotations

import pytest

from core.agent_dm_models import (
    DEFAULT_AGENT_DM_MESSAGE_MAXLEN,
    VALID_AGENT_DM_CHAT_PROVIDERS,
    VALID_AGENT_DM_MESSAGE_SENDER_KINDS,
    VALID_AGENT_DM_SESSION_STATUSES,
    AgentDmMessage,
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


def _message(**overrides: object) -> AgentDmMessage:
    data: dict[str, object] = {
        "owner_user_id": 101,
        "project_id": "alpha_project",
        "agent_role": "writer_agent",
        "sender_kind": "owner",
        "sender_role": "owner",
        "body": "Need a first draft",
        "created_at": 1001.0,
    }
    data.update(overrides)
    return AgentDmMessage(**data)


def test_valid_agent_dm_constants_are_stable():
    assert VALID_AGENT_DM_CHAT_PROVIDERS == {"telegram"}
    assert VALID_AGENT_DM_SESSION_STATUSES == {"active", "closed"}
    assert VALID_AGENT_DM_MESSAGE_SENDER_KINDS == {"owner", "agent"}
    assert DEFAULT_AGENT_DM_MESSAGE_MAXLEN == 20


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


def test_agent_dm_message_owner_happy_path_normalizes_fields():
    message = _message(
        project_id="  Alpha_Project  ",
        agent_role="  Writer_Agent  ",
        sender_kind="  Owner  ",
        sender_role="  Owner  ",
        body="  Need a first draft  ",
        created_at=1234,
    )

    assert message.project_id == "alpha_project"
    assert message.agent_role == "writer_agent"
    assert message.sender_kind == "owner"
    assert message.sender_role == "owner"
    assert message.body == "Need a first draft"
    assert message.created_at == 1234.0


def test_agent_dm_message_agent_happy_path():
    message = _message(
        sender_kind="agent",
        sender_role="writer_agent",
        body="Draft is ready",
    )

    assert message.sender_kind == "agent"
    assert message.sender_role == "writer_agent"
    assert message.body == "Draft is ready"


def test_agent_dm_message_is_frozen():
    message = _message()
    with pytest.raises(Exception):
        message.body = "mutated"  # type: ignore[misc]


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "7"])
def test_agent_dm_message_rejects_invalid_owner_user_id(bad: object):
    with pytest.raises(ValueError, match="invalid_owner_user_id"):
        _message(owner_user_id=bad)


@pytest.mark.parametrize(
    "bad",
    ["", "  ", None, "alpha-project", "123alpha", "русский"],
)
def test_agent_dm_message_rejects_invalid_project_id(bad: object):
    with pytest.raises(ValueError):
        _message(project_id=bad)


@pytest.mark.parametrize("bad", ["", "  ", None, "writer-agent", "русский"])
def test_agent_dm_message_rejects_invalid_agent_role(bad: object):
    with pytest.raises(ValueError):
        _message(agent_role=bad)


@pytest.mark.parametrize("bad", ["", "  ", None, "system", "русский"])
def test_agent_dm_message_rejects_invalid_sender_kind(bad: object):
    with pytest.raises(ValueError):
        _message(sender_kind=bad)


@pytest.mark.parametrize("bad", ["", "  ", None, "writer-agent", "русский"])
def test_agent_dm_message_rejects_invalid_sender_role(bad: object):
    with pytest.raises(ValueError):
        _message(sender_role=bad)


@pytest.mark.parametrize("bad", ["", "  ", None, 123])
def test_agent_dm_message_rejects_empty_body(bad: object):
    with pytest.raises(ValueError, match="empty_body"):
        _message(body=bad)


@pytest.mark.parametrize(
    "bad",
    [0, -1, True, float("nan"), float("inf"), "123.4"],
)
def test_agent_dm_message_rejects_invalid_created_at(bad: object):
    with pytest.raises(ValueError, match="invalid_created_at"):
        _message(created_at=bad)


def test_agent_dm_message_rejects_owner_sender_role_mismatch():
    with pytest.raises(
        ValueError,
        match="owner_sender_role_must_be_owner:writer_agent",
    ):
        _message(sender_kind="owner", sender_role="writer_agent")


def test_agent_dm_message_rejects_agent_sender_role_mismatch():
    with pytest.raises(
        ValueError,
        match="agent_sender_role_must_match_agent_role:reviewer_agent!=writer_agent",
    ):
        _message(sender_kind="agent", sender_role="reviewer_agent")
