"""Tests for core.agent_bus."""

from __future__ import annotations

import pytest

from core.agent_bus import InMemoryAgentBus
from core.agent_bus_models import (
    AgentMessageRef,
    AgentReply,
    AgentRequest,
    ProjectThread,
)


def _request(thread: ProjectThread, **overrides: object) -> AgentRequest:
    data: dict[str, object] = {
        "project_id": thread.project_id,
        "thread_id": thread.thread_id,
        "sender_role": "coordinator_agent",
        "recipient_role": "writer_agent",
        "body": "Need a first draft",
        "created_at": 1001.0,
    }
    data.update(overrides)
    return AgentRequest(**data)


def _reply(
    thread: ProjectThread,
    *,
    in_reply_to: AgentMessageRef,
    **overrides: object,
) -> AgentReply:
    data: dict[str, object] = {
        "project_id": thread.project_id,
        "thread_id": thread.thread_id,
        "sender_role": "writer_agent",
        "recipient_role": "coordinator_agent",
        "in_reply_to": in_reply_to,
        "body": "Draft is ready",
        "created_at": 1002.0,
    }
    data.update(overrides)
    return AgentReply(**data)


def test_open_thread_creates_deterministic_backend_threads():
    bus = InMemoryAgentBus()

    first = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    second = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="writer_agent",
        created_at=1001.0,
    )

    assert first.thread_id == "thread_000001"
    assert second.thread_id == "thread_000002"
    assert first.status == "open"
    assert second.opened_by_role == "writer_agent"


def test_publish_request_records_message():
    bus = InMemoryAgentBus()
    thread = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )

    message = bus.publish_request(_request(thread))

    assert message.message_id == "msg_000001"
    assert message.message_kind == "request"
    assert message.in_reply_to is None
    assert bus.list_thread_messages(thread.project_id, thread.thread_id) == (
        message,
    )


def test_publish_reply_records_message():
    bus = InMemoryAgentBus()
    thread = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    request_message = bus.publish_request(_request(thread))

    reply_message = bus.publish_reply(
        _reply(
            thread,
            in_reply_to=AgentMessageRef(
                project_id=thread.project_id,
                thread_id=thread.thread_id,
                message_id=request_message.message_id,
            ),
        )
    )

    assert reply_message.message_id == "msg_000002"
    assert reply_message.message_kind == "reply"
    assert reply_message.in_reply_to == AgentMessageRef(
        project_id=thread.project_id,
        thread_id=thread.thread_id,
        message_id=request_message.message_id,
    )
    assert bus.list_thread_messages(thread.project_id, thread.thread_id) == (
        request_message,
        reply_message,
    )


def test_publish_reply_rejects_unknown_request():
    bus = InMemoryAgentBus()
    thread = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )

    with pytest.raises(ValueError, match="unknown_in_reply_to:msg_999999"):
        bus.publish_reply(
            _reply(
                thread,
                in_reply_to=AgentMessageRef(
                    project_id=thread.project_id,
                    thread_id=thread.thread_id,
                    message_id="msg_999999",
                ),
            )
        )


def test_publish_reply_rejects_cross_thread_boundary():
    bus = InMemoryAgentBus()
    first = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    second = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1001.0,
    )
    request_message = bus.publish_request(_request(first))

    with pytest.raises(ValueError, match="unknown_in_reply_to:msg_000001"):
        bus.publish_reply(
            _reply(
                second,
                in_reply_to=AgentMessageRef(
                    project_id=second.project_id,
                    thread_id=second.thread_id,
                    message_id=request_message.message_id,
                ),
                created_at=1003.0,
            )
        )


def test_publish_reply_rejects_cross_project_boundary():
    bus = InMemoryAgentBus()
    alpha_thread = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    beta_thread = bus.open_thread(
        project_id="beta_project",
        opened_by_role="coordinator_agent",
        created_at=1001.0,
    )
    request_message = bus.publish_request(_request(alpha_thread))

    with pytest.raises(ValueError, match="unknown_in_reply_to:msg_000001"):
        bus.publish_reply(
            _reply(
                beta_thread,
                in_reply_to=AgentMessageRef(
                    project_id=beta_thread.project_id,
                    thread_id=beta_thread.thread_id,
                    message_id=request_message.message_id,
                ),
                created_at=1003.0,
            )
        )


def test_reply_target_must_be_request():
    bus = InMemoryAgentBus()
    thread = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    request_message = bus.publish_request(_request(thread))
    reply_message = bus.publish_reply(
        _reply(
            thread,
            in_reply_to=AgentMessageRef(
                project_id=thread.project_id,
                thread_id=thread.thread_id,
                message_id=request_message.message_id,
            ),
        )
    )

    with pytest.raises(ValueError, match="reply_target_must_be_request:reply"):
        bus.publish_reply(
            _reply(
                thread,
                in_reply_to=AgentMessageRef(
                    project_id=thread.project_id,
                    thread_id=thread.thread_id,
                    message_id=reply_message.message_id,
                ),
                created_at=1003.0,
            )
        )


def test_list_thread_messages_returns_chronological_history():
    bus = InMemoryAgentBus()
    thread = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    request_message = bus.publish_request(_request(thread))
    reply_message = bus.publish_reply(
        _reply(
            thread,
            in_reply_to=AgentMessageRef(
                project_id=thread.project_id,
                thread_id=thread.thread_id,
                message_id=request_message.message_id,
            ),
        )
    )

    assert bus.list_thread_messages("alpha_project", thread.thread_id) == (
        request_message,
        reply_message,
    )


def test_list_inbox_filters_by_recipient_and_project():
    bus = InMemoryAgentBus()
    alpha_first = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    alpha_second = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1001.0,
    )
    beta_thread = bus.open_thread(
        project_id="beta_project",
        opened_by_role="coordinator_agent",
        created_at=1002.0,
    )

    alpha_writer_1 = bus.publish_request(_request(alpha_first, created_at=1003.0))
    bus.publish_request(
        _request(
            alpha_first,
            recipient_role="reviewer_agent",
            created_at=1004.0,
        )
    )
    alpha_writer_2 = bus.publish_request(_request(alpha_second, created_at=1005.0))
    bus.publish_request(_request(beta_thread, created_at=1006.0))

    assert bus.list_inbox("alpha_project", "writer_agent") == (
        alpha_writer_1,
        alpha_writer_2,
    )


def test_list_thread_messages_do_not_leak_another_thread():
    bus = InMemoryAgentBus()
    first = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    second = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1001.0,
    )
    first_message = bus.publish_request(_request(first))
    bus.publish_request(_request(second, created_at=1002.0))

    assert bus.list_thread_messages(first.project_id, first.thread_id) == (
        first_message,
    )


def test_list_views_return_empty_tuples_for_unknown_scope():
    bus = InMemoryAgentBus()

    assert bus.list_thread_messages("alpha_project", "thread_000001") == ()
    assert bus.list_inbox("alpha_project", "writer_agent") == ()


def test_publish_request_requires_existing_thread():
    bus = InMemoryAgentBus()

    with pytest.raises(
        ValueError,
        match="unknown_thread:alpha_project:thread_000001",
    ):
        bus.publish_request(
            AgentRequest(
                project_id="alpha_project",
                thread_id="thread_000001",
                sender_role="coordinator_agent",
                recipient_role="writer_agent",
                body="Need a first draft",
                created_at=1001.0,
            )
        )


def test_publish_rejects_non_monotonic_message_timestamps():
    bus = InMemoryAgentBus()
    thread = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    bus.publish_request(_request(thread, created_at=1002.0))

    with pytest.raises(
        ValueError,
        match="message_created_at_before_thread_last_message_at:1001.0<1002.0",
    ):
        bus.publish_request(_request(thread, created_at=1001.0))


def test_bus_contract_has_no_telegram_fields():
    bus = InMemoryAgentBus()
    thread = bus.open_thread(
        project_id="alpha_project",
        opened_by_role="coordinator_agent",
        created_at=1000.0,
    )
    message = bus.publish_request(_request(thread))

    assert "chat_id" not in ProjectThread.__dataclass_fields__
    assert "delivery_role" not in type(message).__dataclass_fields__
    assert not hasattr(message, "reply_to_message_id")
