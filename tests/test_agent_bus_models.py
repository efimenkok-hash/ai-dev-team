"""Tests for core.agent_bus_models."""

from __future__ import annotations

import pytest

from core.agent_bus_models import (
    VALID_AGENT_MESSAGE_KINDS,
    VALID_PROJECT_THREAD_STATUSES,
    AgentMessage,
    AgentMessageRef,
    AgentReply,
    AgentRequest,
    ProjectThread,
)


def _message_ref(**overrides: object) -> AgentMessageRef:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "thread_id": "thread_000001",
        "message_id": "msg_000001",
    }
    data.update(overrides)
    return AgentMessageRef(**data)


def _thread(**overrides: object) -> ProjectThread:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "thread_id": "thread_000001",
        "opened_by_role": "coordinator_agent",
        "status": "open",
        "created_at": 1000.0,
        "last_message_at": 1000.0,
        "task_id": None,
    }
    data.update(overrides)
    return ProjectThread(**data)


def _request(**overrides: object) -> AgentRequest:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "thread_id": "thread_000001",
        "sender_role": "coordinator_agent",
        "recipient_role": "writer_agent",
        "body": "Need a first draft",
        "created_at": 1001.0,
    }
    data.update(overrides)
    return AgentRequest(**data)


def _reply(**overrides: object) -> AgentReply:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "thread_id": "thread_000001",
        "sender_role": "writer_agent",
        "recipient_role": "coordinator_agent",
        "in_reply_to": _message_ref(),
        "body": "Draft is ready",
        "created_at": 1002.0,
    }
    data.update(overrides)
    return AgentReply(**data)


def _message(**overrides: object) -> AgentMessage:
    data: dict[str, object] = {
        "project_id": "alpha_project",
        "thread_id": "thread_000001",
        "message_id": "msg_000001",
        "sender_role": "coordinator_agent",
        "recipient_role": "writer_agent",
        "message_kind": "request",
        "body": "Need a first draft",
        "created_at": 1001.0,
        "in_reply_to": None,
    }
    data.update(overrides)
    return AgentMessage(**data)


def test_agent_bus_constants_are_stable():
    assert VALID_PROJECT_THREAD_STATUSES == {"open", "closed"}
    assert VALID_AGENT_MESSAGE_KINDS == {"request", "reply"}


def test_project_thread_happy_path_normalizes_fields():
    thread = _thread(
        project_id="  Alpha_Project  ",
        thread_id="  Thread_000001  ",
        opened_by_role="  Coordinator_Agent  ",
        status="  Closed  ",
        created_at=1000,
        last_message_at=1001,
        task_id="  Task-abc-001  ",
    )

    assert thread.project_id == "alpha_project"
    assert thread.thread_id == "thread_000001"
    assert thread.opened_by_role == "coordinator_agent"
    assert thread.status == "closed"
    assert thread.created_at == 1000.0
    assert thread.last_message_at == 1001.0
    assert thread.task_id == "task-abc-001"


def test_agent_request_happy_path_normalizes_fields():
    request = _request(
        project_id="  Alpha_Project  ",
        thread_id="  Thread_000001  ",
        sender_role="  Coordinator_Agent  ",
        recipient_role="  Writer_Agent  ",
        body="  Need a first draft  ",
        created_at=1001,
    )

    assert request.project_id == "alpha_project"
    assert request.thread_id == "thread_000001"
    assert request.sender_role == "coordinator_agent"
    assert request.recipient_role == "writer_agent"
    assert request.body == "Need a first draft"
    assert request.created_at == 1001.0


def test_agent_reply_happy_path_normalizes_fields():
    reply = _reply(
        project_id="  Alpha_Project  ",
        thread_id="  Thread_000001  ",
        sender_role="  Writer_Agent  ",
        recipient_role="  Coordinator_Agent  ",
        body="  Draft is ready  ",
        created_at=1002,
        in_reply_to=_message_ref(
            project_id="  Alpha_Project  ",
            thread_id="  Thread_000001  ",
            message_id="  Msg_000001  ",
        ),
    )

    assert reply.project_id == "alpha_project"
    assert reply.thread_id == "thread_000001"
    assert reply.sender_role == "writer_agent"
    assert reply.recipient_role == "coordinator_agent"
    assert reply.in_reply_to == _message_ref()
    assert reply.body == "Draft is ready"
    assert reply.created_at == 1002.0


def test_agent_message_happy_path():
    message = _message(
        message_id="  Msg_000001  ",
        sender_role="  Coordinator_Agent  ",
        recipient_role="  Writer_Agent  ",
        message_kind="  Reply  ",
        body="  Draft is ready  ",
        created_at=1003,
        in_reply_to=_message_ref(),
    )

    assert message.message_id == "msg_000001"
    assert message.sender_role == "coordinator_agent"
    assert message.recipient_role == "writer_agent"
    assert message.message_kind == "reply"
    assert message.body == "Draft is ready"
    assert message.created_at == 1003.0
    assert message.in_reply_to == _message_ref()


@pytest.mark.parametrize(
    "factory",
    [_thread, _request, _reply, _message, _message_ref],
)
def test_bus_models_are_frozen(factory):
    value = factory()
    field_name = next(iter(value.__dataclass_fields__))
    with pytest.raises(Exception):
        setattr(value, field_name, "mutated")


@pytest.mark.parametrize("bad", ["", "  ", None, "alpha-project", "Русский"])
def test_project_thread_rejects_invalid_project_id(bad: object):
    with pytest.raises(ValueError):
        _thread(project_id=bad)


@pytest.mark.parametrize("bad", ["", "  ", None, "thread-1", "Русский"])
def test_project_thread_rejects_invalid_thread_id(bad: object):
    with pytest.raises(ValueError):
        _thread(thread_id=bad)


@pytest.mark.parametrize("bad", ["", "  ", "task 42", "task@42", "Русский"])
def test_project_thread_rejects_invalid_task_id(bad: object):
    with pytest.raises(ValueError):
        _thread(task_id=bad)


@pytest.mark.parametrize("bad", ["", "  ", None, "writer-agent", "Русский"])
def test_agent_request_rejects_invalid_sender_role(bad: object):
    with pytest.raises(ValueError):
        _request(sender_role=bad)


@pytest.mark.parametrize("bad", ["", "  ", None, "reviewer-agent", "Русский"])
def test_agent_request_rejects_invalid_recipient_role(bad: object):
    with pytest.raises(ValueError):
        _request(recipient_role=bad)


@pytest.mark.parametrize("bad", ["", "  ", None, 123])
def test_agent_request_rejects_empty_body(bad: object):
    with pytest.raises(ValueError, match="empty_body"):
        _request(body=bad)


@pytest.mark.parametrize(
    ("factory", "field_name", "bad"),
    [
        (_thread, "created_at", 0),
        (_thread, "last_message_at", float("inf")),
        (_request, "created_at", -1),
        (_reply, "created_at", True),
        (_message, "created_at", float("nan")),
    ],
)
def test_bus_models_reject_invalid_timestamps(factory, field_name: str, bad: object):
    with pytest.raises(ValueError, match=f"invalid_{field_name}"):
        factory(**{field_name: bad})


def test_agent_message_rejects_invalid_message_kind():
    with pytest.raises(ValueError, match="invalid_message_kind:note"):
        _message(message_kind="note")


def test_agent_reply_rejects_invalid_in_reply_to_type():
    with pytest.raises(ValueError, match="invalid_in_reply_to_type:str"):
        _reply(in_reply_to="msg_000001")


def test_agent_reply_rejects_project_scope_mismatch():
    with pytest.raises(
        ValueError,
        match="in_reply_to_project_id_mismatch:beta_project!=alpha_project",
    ):
        _reply(
            in_reply_to=_message_ref(project_id="beta_project"),
        )


def test_agent_reply_rejects_thread_scope_mismatch():
    with pytest.raises(
        ValueError,
        match="in_reply_to_thread_id_mismatch:thread_000002!=thread_000001",
    ):
        _reply(
            in_reply_to=_message_ref(thread_id="thread_000002"),
        )


def test_agent_message_reply_requires_reference():
    with pytest.raises(ValueError, match="reply_message_requires_in_reply_to"):
        _message(message_kind="reply", in_reply_to=None)


def test_agent_message_request_forbids_reference():
    with pytest.raises(
        ValueError,
        match="request_message_cannot_have_in_reply_to",
    ):
        _message(message_kind="request", in_reply_to=_message_ref())


def test_agent_message_rejects_reference_project_scope_mismatch():
    with pytest.raises(
        ValueError,
        match="in_reply_to_project_id_mismatch:beta_project!=alpha_project",
    ):
        _message(
            message_kind="reply",
            in_reply_to=_message_ref(project_id="beta_project"),
        )


def test_agent_message_rejects_reference_thread_scope_mismatch():
    with pytest.raises(
        ValueError,
        match="in_reply_to_thread_id_mismatch:thread_000002!=thread_000001",
    ):
        _message(
            message_kind="reply",
            in_reply_to=_message_ref(thread_id="thread_000002"),
        )


def test_project_thread_rejects_last_message_before_created_at():
    with pytest.raises(ValueError, match="last_message_at_before_created_at"):
        _thread(created_at=1000.0, last_message_at=999.0)
