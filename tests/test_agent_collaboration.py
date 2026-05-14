"""Tests for bounded backend-bus agent collaboration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.agent_bus import StateBackedAgentBus
from core.agent_bus_projection import AgentBusProjectionService, ProjectingAgentBus
from core.agent_bus_projection_throttle import ThrottledProjectingAgentBus
from core.agent_collaboration import (
    AgentCollaborationContext,
    AgentCollaborationPolicy,
    AgentCollaborationService,
)
from core.agent_role_catalog import SPECIALIST_ROLE_ORDER
from core.coordinator_role import COORDINATOR_ROLE
from core.llm_dispatcher import LLMAttempt, LLMDispatcher, LLMResponse
from core.model_tier import REQUIRED_ROLES, TierConfig
from core.project_models import Project, ProjectChatBinding, ProjectPolicy
from core.project_registry import ProjectRegistry, ProjectSnapshot
from core.state_db import StateDB


def _make_tier() -> TierConfig:
    return TierConfig(
        name="test",
        description="test",
        estimated_cost_usd=0.5,
        models_per_role={role: ("model-x",) for role in REQUIRED_ROLES},
        specialist_models_per_role={
            role: ("specialist-model-x",) for role in SPECIALIST_ROLE_ORDER
        },
    )


def _make_dispatcher() -> LLMDispatcher:
    return LLMDispatcher(api_key="sk-test-key-1234")


def _make_response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        model_used="model-x",
        prompt_tokens=11,
        completion_tokens=7,
        attempts=(
            LLMAttempt(model="model-x", ok=True, reason="ok", duration_ms=1),
        ),
    )


def _make_registry(tmp_path: Path, *, with_chat_binding: bool = True) -> ProjectRegistry:
    db = StateDB(tmp_path / "collaboration.db")
    registry = ProjectRegistry(db)
    registry.register_project(
        ProjectSnapshot(
            project=Project(
                project_id="alpha_project",
                slug="alpha-project",
                name="Alpha",
                description="Alpha project",
                owner_user_id=101,
                status="active",
            ),
            policy=ProjectPolicy(
                project_id="alpha_project",
                allow_hiring=True,
                allow_agent_dm=False,
                require_owner_approval_for_hires=True,
            ),
            chat_binding=(
                ProjectChatBinding(
                    project_id="alpha_project",
                    chat_id=-100123,
                    chat_provider="telegram",
                )
                if with_chat_binding
                else None
            ),
        )
    )
    return registry


def _make_bus(
    tmp_path: Path,
    *,
    throttled: bool = False,
    with_chat_binding: bool = True,
):
    sent_envelopes = []
    registry = _make_registry(tmp_path, with_chat_binding=with_chat_binding)
    backend_bus = StateBackedAgentBus(registry.state_db)
    projecting_bus = ProjectingAgentBus(
        backend_bus,
        AgentBusProjectionService(
            registry,
            lambda envelope: sent_envelopes.append(envelope),
        ),
    )
    bus = (
        ThrottledProjectingAgentBus(projecting_bus)
        if throttled
        else projecting_bus
    )
    thread = projecting_bus.get_or_open_task_thread(
        "alpha_project",
        "task-42",
        opened_by_role=COORDINATOR_ROLE,
        created_at=1000.0,
    )
    return registry, bus, projecting_bus, thread, sent_envelopes


def test_capability_instruction_is_deterministic(tmp_path: Path):
    _, bus, _projecting_bus, _thread, _ = _make_bus(tmp_path)
    service = AgentCollaborationService(bus, _make_dispatcher(), _make_tier())

    instruction = service.build_capability_instruction("writer_agent")

    assert instruction == service.build_capability_instruction("writer_agent")
    assert '"action":"ask_another_agent"' in instruction
    assert "максимум консультаций" in instruction


def test_parse_consultation_request_happy_path(tmp_path: Path):
    _, bus, _projecting_bus, _thread, _ = _make_bus(tmp_path)
    service = AgentCollaborationService(bus, _make_dispatcher(), _make_tier())

    request = service.parse_consultation_request(
        '{"action":"ask_another_agent","recipient_role":"reviewer_agent","question":"Проверь API"}'
    )

    assert request is not None
    assert request.recipient_role == "reviewer_agent"
    assert request.question == "Проверь API"


def test_parse_consultation_request_returns_none_for_normal_final_output(
    tmp_path: Path,
):
    _, bus, _projecting_bus, _thread, _ = _make_bus(tmp_path)
    service = AgentCollaborationService(bus, _make_dispatcher(), _make_tier())

    assert service.parse_consultation_request('{"plan":"ok"}') is None
    assert service.parse_consultation_request("plain final answer") is None


def test_parse_bad_consultation_json_raises_value_error(tmp_path: Path):
    _, bus, _projecting_bus, _thread, _ = _make_bus(tmp_path)
    service = AgentCollaborationService(bus, _make_dispatcher(), _make_tier())

    with pytest.raises(ValueError, match="empty_question"):
        service.parse_consultation_request(
            '{"action":"ask_another_agent","recipient_role":"reviewer_agent","question":"   "}'
        )


def test_run_consultation_rejects_self_ask(tmp_path: Path):
    _, bus, _projecting_bus, thread, _ = _make_bus(tmp_path)
    service = AgentCollaborationService(bus, _make_dispatcher(), _make_tier())
    context = AgentCollaborationContext(
        project_id="alpha_project",
        task_id="task-42",
        thread=thread,
        caller_role="writer_agent",
        owner_task_text="Сделай API",
    )
    request = service.parse_consultation_request(
        '{"action":"ask_another_agent","recipient_role":"writer_agent","question":"Сам себя спроси"}'
    )
    assert request is not None

    with pytest.raises(ValueError, match="self_consultation_forbidden:writer_agent"):
        service.run_consultation(context, request, created_at=1001.0)


def test_run_consultation_rejects_unknown_recipient_role(tmp_path: Path):
    _, bus, _projecting_bus, thread, _ = _make_bus(tmp_path)
    service = AgentCollaborationService(bus, _make_dispatcher(), _make_tier())
    context = AgentCollaborationContext(
        project_id="alpha_project",
        task_id="task-42",
        thread=thread,
        caller_role="writer_agent",
        owner_task_text="Сделай API",
    )
    request = service.parse_consultation_request(
        '{"action":"ask_another_agent","recipient_role":"ghost_agent","question":"?"}'
    )
    assert request is not None

    with pytest.raises(ValueError, match="unknown_recipient_role:ghost_agent"):
        service.run_consultation(context, request, created_at=1001.0)


@pytest.mark.parametrize(
    "recipient_role",
    ("security_agent", "devops_agent", "data_agent"),
)
def test_run_consultation_allows_selectable_specialist_recipient_roles(
    tmp_path: Path,
    recipient_role: str,
):
    _, bus, projecting_bus, thread, sent_envelopes = _make_bus(
        tmp_path,
        throttled=True,
    )
    dispatcher = _make_dispatcher()
    dispatcher.dispatch = MagicMock(return_value=_make_response("Короткий ответ"))  # type: ignore[method-assign]
    service = AgentCollaborationService(bus, dispatcher, _make_tier())
    context = AgentCollaborationContext(
        project_id="alpha_project",
        task_id="task-42",
        thread=thread,
        caller_role="writer_agent",
        owner_task_text="Сделай API",
    )
    request = service.parse_consultation_request(
        '{"action":"ask_another_agent","recipient_role":"'
        f'{recipient_role}'
        '","question":"Нужна экспертная консультация"}'
    )
    assert request is not None

    result = service.run_consultation(context, request, created_at=1001.0)

    assert result.recipient_role == recipient_role
    assert result.request_message.recipient_role == recipient_role
    assert result.reply_message.sender_role == recipient_role
    assert dispatcher.dispatch.call_args[0][0].agent_role == recipient_role
    messages = projecting_bus.list_thread_messages("alpha_project", thread.thread_id)
    assert tuple(message.message_kind for message in messages) == ("request", "reply")
    assert len(sent_envelopes) >= 2
    assert sent_envelopes[1].sender_role == recipient_role


def test_run_consultation_writes_request_and_reply_and_reuses_task_thread(
    tmp_path: Path,
):
    registry, bus, projecting_bus, thread, sent_envelopes = _make_bus(
        tmp_path,
        throttled=True,
    )
    dispatcher = _make_dispatcher()
    dispatcher.dispatch = MagicMock(return_value=_make_response("Короткий ответ"))  # type: ignore[method-assign]
    service = AgentCollaborationService(bus, dispatcher, _make_tier())
    context = AgentCollaborationContext(
        project_id="alpha_project",
        task_id="task-42",
        thread=thread,
        caller_role="writer_agent",
        owner_task_text="Сделай API",
    )
    request = service.parse_consultation_request(
        '{"action":"ask_another_agent","recipient_role":"reviewer_agent","question":"Нужен review"}'
    )
    assert request is not None

    first = service.run_consultation(context, request, created_at=1001.0)
    second = service.run_consultation(context, request, created_at=1002.0)

    persisted_thread = projecting_bus.get_task_thread("alpha_project", "task-42")
    assert persisted_thread is not None
    assert persisted_thread.thread_id == thread.thread_id
    messages = projecting_bus.list_thread_messages("alpha_project", thread.thread_id)
    assert tuple(message.message_kind for message in messages) == (
        "request",
        "reply",
        "request",
        "reply",
    )
    assert first.reply_message.in_reply_to is not None
    assert (
        first.reply_message.in_reply_to.message_id
        == first.request_message.message_id
    )
    assert second.reply_message.in_reply_to is not None
    assert (
        second.reply_message.in_reply_to.message_id
        == second.request_message.message_id
    )
    assert len(sent_envelopes) >= 2
    assert sent_envelopes[0].sender_role == "writer_agent"
    assert sent_envelopes[1].sender_role == "reviewer_agent"
    assert registry.get_project_snapshot("alpha_project") is not None


def test_run_consultation_supports_rapid_sequential_calls_in_same_task_thread(
    tmp_path: Path,
):
    _, bus, projecting_bus, thread, _ = _make_bus(
        tmp_path,
        throttled=True,
    )
    dispatcher = _make_dispatcher()
    dispatcher.dispatch = MagicMock(return_value=_make_response("Короткий ответ"))  # type: ignore[method-assign]
    service = AgentCollaborationService(bus, dispatcher, _make_tier())
    context = AgentCollaborationContext(
        project_id="alpha_project",
        task_id="task-42",
        thread=thread,
        caller_role="writer_agent",
        owner_task_text="Сделай API",
    )
    request = service.parse_consultation_request(
        '{"action":"ask_another_agent","recipient_role":"reviewer_agent","question":"Нужен review"}'
    )
    assert request is not None

    first = service.run_consultation(context, request, created_at=1000.0)
    second = service.run_consultation(context, request, created_at=1000.0005)

    assert first.request_message.created_at == 1000.0
    assert second.request_message.created_at >= first.reply_message.created_at
    messages = projecting_bus.list_thread_messages("alpha_project", thread.thread_id)
    assert tuple(message.message_kind for message in messages) == (
        "request",
        "reply",
        "request",
        "reply",
    )
    assert second.reply_message.in_reply_to is not None
    assert (
        second.reply_message.in_reply_to.message_id
        == second.request_message.message_id
    )


def test_consult_only_dispatch_prompt_contains_expected_context_and_forbids_nested_ask(
    tmp_path: Path,
):
    _, bus, _projecting_bus, thread, _ = _make_bus(tmp_path)
    dispatcher = _make_dispatcher()
    dispatcher.dispatch = MagicMock(return_value=_make_response("Короткий ответ"))  # type: ignore[method-assign]
    service = AgentCollaborationService(bus, dispatcher, _make_tier())
    context = AgentCollaborationContext(
        project_id="alpha_project",
        task_id="task-42",
        thread=thread,
        caller_role="architect_agent",
        owner_task_text="Собери endpoint для billing",
    )
    request = service.parse_consultation_request(
        '{"action":"ask_another_agent","recipient_role":"reviewer_agent","question":"Где риск?"}'
    )
    assert request is not None

    service.run_consultation(context, request, created_at=1001.0)

    llm_request = dispatcher.dispatch.call_args[0][0]
    assert llm_request.agent_role == "reviewer_agent"
    assert llm_request.messages[0]["role"] == "system"
    assert "INTERNAL CONSULTATION MODE" in llm_request.messages[0]["content"]
    assert "не возвращай JSON consultation request".lower() in llm_request.messages[0]["content"].lower()
    user_content = llm_request.messages[1]["content"]
    assert "project_id: alpha_project" in user_content
    assert "task_id: task-42" in user_content
    assert "caller_role: architect_agent" in user_content
    assert "Собери endpoint для billing" in user_content
    assert "Где риск?" in user_content


def test_nested_consultation_from_recipient_is_rejected_without_fake_reply(
    tmp_path: Path,
):
    _, bus, projecting_bus, thread, _ = _make_bus(tmp_path)
    dispatcher = _make_dispatcher()
    dispatcher.dispatch = MagicMock(  # type: ignore[method-assign]
        return_value=_make_response(
            '{"action":"ask_another_agent","recipient_role":"tester_agent","question":"ещё вопрос"}'
        )
    )
    service = AgentCollaborationService(bus, dispatcher, _make_tier())
    context = AgentCollaborationContext(
        project_id="alpha_project",
        task_id="task-42",
        thread=thread,
        caller_role="writer_agent",
        owner_task_text="Сделай API",
    )
    request = service.parse_consultation_request(
        '{"action":"ask_another_agent","recipient_role":"reviewer_agent","question":"Нужен review"}'
    )
    assert request is not None

    with pytest.raises(ValueError, match="nested_consultation_not_allowed:reviewer_agent"):
        service.run_consultation(context, request, created_at=1001.0)

    messages = projecting_bus.list_thread_messages("alpha_project", thread.thread_id)
    assert tuple(message.message_kind for message in messages) == ("request",)


def test_specialist_recipient_failure_does_not_write_fake_reply(tmp_path: Path):
    _, bus, projecting_bus, thread, _ = _make_bus(tmp_path)
    dispatcher = _make_dispatcher()
    dispatcher.dispatch = MagicMock(side_effect=RuntimeError("specialist failed"))  # type: ignore[method-assign]
    service = AgentCollaborationService(bus, dispatcher, _make_tier())
    context = AgentCollaborationContext(
        project_id="alpha_project",
        task_id="task-42",
        thread=thread,
        caller_role="writer_agent",
        owner_task_text="Сделай API",
    )
    request = service.parse_consultation_request(
        '{"action":"ask_another_agent","recipient_role":"security_agent","question":"Проверь риск"}'
    )
    assert request is not None

    with pytest.raises(RuntimeError, match="specialist failed"):
        service.run_consultation(context, request, created_at=1001.0)

    messages = projecting_bus.list_thread_messages("alpha_project", thread.thread_id)
    assert tuple(message.message_kind for message in messages) == ("request",)


def test_policy_rejects_non_positive_limits():
    with pytest.raises(ValueError, match="invalid_max_consultations_per_call"):
        AgentCollaborationPolicy(max_consultations_per_call=0)
    with pytest.raises(ValueError, match="invalid_max_question_chars"):
        AgentCollaborationPolicy(max_question_chars=0)
