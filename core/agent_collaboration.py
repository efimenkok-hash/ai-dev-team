"""
core/agent_collaboration.py

Bounded backend-bus consultation layer for project-aware pipeline agents.

Scope for roadmap step P4.7:
1. Define a strict typed contract for one-hop agent consultation.
2. Route request/reply through the backend agent bus, not Telegram.
3. Keep the capability bounded: no self-ask, no recursion, no hidden retries.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field

from core.agent_bus_models import (
    AgentMessage,
    AgentMessageRef,
    AgentReply,
    AgentRequest,
    ProjectThread,
)
from core.agent_bus_projection import ProjectingAgentBus
from core.agent_bus_projection_throttle import ThrottledProjectingAgentBus
from core.agent_role_catalog import is_selectable_agent_role, is_specialist_role
from core.json_extractor import extract_json_object
from core.llm_dispatcher import LLMDispatcher, LLMRequest
from core.model_tier import REQUIRED_ROLES, TierConfig
from core.specialization_hints import SpecializationHints
from core.specialization_prompt_augmentation import (
    SpecializationPromptAugmentor,
)

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SPECIALIZATION_PROMPT_AUGMENTOR = SpecializationPromptAugmentor()


def _normalize_identifier(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"empty_{field_name}")
    normalized = value.strip().lower()
    if not normalized.isascii():
        raise ValueError(f"non_ascii_{field_name}")
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"invalid_{field_name}:{normalized}")
    return normalized


def _normalize_task_id(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"empty_{field_name}")
    normalized = value.strip().lower()
    if not normalized.isascii():
        raise ValueError(f"non_ascii_{field_name}")
    if not _TASK_ID_RE.fullmatch(normalized):
        raise ValueError(f"invalid_{field_name}:{normalized}")
    return normalized


def _normalize_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"empty_{field_name}")
    return value.strip()


def _normalize_positive_float(value: float, *, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"invalid_{field_name}:{value!r}")
    return float(value)


@dataclass(frozen=True)
class AgentCollaborationContext:
    project_id: str
    task_id: str
    thread: ProjectThread
    caller_role: str
    owner_task_text: str
    specialization_hints: SpecializationHints = field(
        default_factory=SpecializationHints.empty
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_id",
            _normalize_identifier(self.project_id, field_name="project_id"),
        )
        object.__setattr__(
            self,
            "task_id",
            _normalize_task_id(self.task_id, field_name="task_id"),
        )
        if not isinstance(self.thread, ProjectThread):
            raise ValueError(
                "invalid_project_thread_type:"
                f"{type(self.thread).__name__}"
            )
        object.__setattr__(
            self,
            "caller_role",
            _normalize_identifier(self.caller_role, field_name="caller_role"),
        )
        object.__setattr__(
            self,
            "owner_task_text",
            _normalize_text(self.owner_task_text, field_name="owner_task_text"),
        )
        if not isinstance(self.specialization_hints, SpecializationHints):
            raise ValueError(
                "invalid_specialization_hints_type:"
                f"{type(self.specialization_hints).__name__}"
            )
        if self.thread.project_id != self.project_id:
            raise ValueError(
                "thread_project_id_mismatch:"
                f"{self.thread.project_id}!={self.project_id}"
            )
        if self.thread.task_id != self.task_id:
            raise ValueError(
                "thread_task_id_mismatch:"
                f"{self.thread.task_id!r}!={self.task_id!r}"
            )


@dataclass(frozen=True)
class AgentConsultationRequest:
    recipient_role: str
    question: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "recipient_role",
            _normalize_identifier(
                self.recipient_role,
                field_name="recipient_role",
            ),
        )
        object.__setattr__(
            self,
            "question",
            _normalize_text(self.question, field_name="question"),
        )


@dataclass(frozen=True)
class AgentConsultationResult:
    request_message: AgentMessage
    reply_message: AgentMessage
    recipient_role: str
    answer_text: str

    def __post_init__(self) -> None:
        if not isinstance(self.request_message, AgentMessage):
            raise ValueError(
                "invalid_request_message_type:"
                f"{type(self.request_message).__name__}"
            )
        if not isinstance(self.reply_message, AgentMessage):
            raise ValueError(
                "invalid_reply_message_type:"
                f"{type(self.reply_message).__name__}"
            )
        object.__setattr__(
            self,
            "recipient_role",
            _normalize_identifier(
                self.recipient_role,
                field_name="recipient_role",
            ),
        )
        object.__setattr__(
            self,
            "answer_text",
            _normalize_text(self.answer_text, field_name="answer_text"),
        )
        if self.request_message.message_kind != "request":
            raise ValueError("request_message_kind_must_be_request")
        if self.reply_message.message_kind != "reply":
            raise ValueError("reply_message_kind_must_be_reply")
        if self.reply_message.in_reply_to is None:
            raise ValueError("reply_message_missing_in_reply_to")
        if self.reply_message.in_reply_to != AgentMessageRef(
            project_id=self.request_message.project_id,
            thread_id=self.request_message.thread_id,
            message_id=self.request_message.message_id,
        ):
            raise ValueError("reply_message_in_reply_to_mismatch")
        if self.request_message.project_id != self.reply_message.project_id:
            raise ValueError("consultation_project_id_mismatch")
        if self.request_message.thread_id != self.reply_message.thread_id:
            raise ValueError("consultation_thread_id_mismatch")
        if self.recipient_role != self.request_message.recipient_role:
            raise ValueError(
                "recipient_role_request_message_mismatch:"
                f"{self.recipient_role}!={self.request_message.recipient_role}"
            )
        if self.recipient_role != self.reply_message.sender_role:
            raise ValueError(
                "recipient_role_reply_sender_mismatch:"
                f"{self.recipient_role}!={self.reply_message.sender_role}"
            )


@dataclass(frozen=True)
class AgentCollaborationPolicy:
    max_consultations_per_call: int = 1
    max_question_chars: int = 2000

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_consultations_per_call, bool)
            or not isinstance(self.max_consultations_per_call, int)
            or self.max_consultations_per_call <= 0
        ):
            raise ValueError(
                "invalid_max_consultations_per_call:"
                f"{self.max_consultations_per_call!r}"
            )
        if (
            isinstance(self.max_question_chars, bool)
            or not isinstance(self.max_question_chars, int)
            or self.max_question_chars <= 0
        ):
            raise ValueError(
                f"invalid_max_question_chars:{self.max_question_chars!r}"
            )


class AgentCollaborationService:
    def __init__(
        self,
        bus: ThrottledProjectingAgentBus | ProjectingAgentBus,
        dispatcher: LLMDispatcher,
        tier: TierConfig,
        policy: AgentCollaborationPolicy | None = None,
    ) -> None:
        if not isinstance(bus, (ThrottledProjectingAgentBus, ProjectingAgentBus)):
            raise ValueError(
                "invalid_collaboration_bus_type:"
                f"{type(bus).__name__}"
            )
        if not isinstance(dispatcher, LLMDispatcher):
            raise ValueError(
                "invalid_dispatcher_type:"
                f"{type(dispatcher).__name__}"
            )
        if not isinstance(tier, TierConfig):
            raise ValueError(f"invalid_tier_type:{type(tier).__name__}")
        if (
            policy is not None
            and not isinstance(policy, AgentCollaborationPolicy)
        ):
            raise ValueError(
                "invalid_agent_collaboration_policy_type:"
                f"{type(policy).__name__}"
            )
        self._bus = bus
        self._dispatcher = dispatcher
        self._tier = tier
        self._policy = policy if policy is not None else AgentCollaborationPolicy()
        self._last_dispatch_usage: tuple[int, int, int] | None = None

    @property
    def policy(self) -> AgentCollaborationPolicy:
        return self._policy

    @property
    def last_dispatch_usage(self) -> tuple[int, int, int] | None:
        return self._last_dispatch_usage

    def build_capability_instruction(self, caller_role: str) -> str:
        normalized_caller_role = _normalize_identifier(
            caller_role,
            field_name="caller_role",
        )
        return (
            "INTERNAL CONSULTATION CAPABILITY\n"
            f"Ты работаешь как {normalized_caller_role}. Если можешь завершить задачу "
            "самостоятельно, верни обычный финальный ответ строго в твоём "
            "стандартном формате.\n"
            "Если без другого эксперта нельзя дать качественный результат, ты "
            "можешь вместо финального ответа вернуть ТОЛЬКО JSON-объект:\n"
            '{"action":"ask_another_agent","recipient_role":"reviewer_agent",'
            '"question":"Нужна короткая консультация по ...","reason":"optional"}\n'
            "Ограничения:\n"
            f"- максимум консультаций за этот вызов: {self._policy.max_consultations_per_call}\n"
            "- только один recipient_role за запрос\n"
            "- нельзя спрашивать самого себя\n"
            "- после консультации нужно вернуть обычный финальный ответ\n"
            "- не запускай рекурсивные консультации"
        )

    def parse_consultation_request(
        self,
        raw_output: str,
    ) -> AgentConsultationRequest | None:
        if not isinstance(raw_output, str) or not raw_output.strip():
            return None
        payload = extract_json_object(raw_output)
        if payload is None or not isinstance(payload, dict):
            return None
        action = payload.get("action")
        if action != "ask_another_agent":
            return None
        return AgentConsultationRequest(
            recipient_role=payload.get("recipient_role"),
            question=payload.get("question"),
        )

    def run_consultation(
        self,
        context: AgentCollaborationContext,
        request: AgentConsultationRequest,
        *,
        created_at: float,
    ) -> AgentConsultationResult:
        if not isinstance(context, AgentCollaborationContext):
            raise ValueError(
                "invalid_agent_collaboration_context_type:"
                f"{type(context).__name__}"
            )
        if not isinstance(request, AgentConsultationRequest):
            raise ValueError(
                "invalid_agent_consultation_request_type:"
                f"{type(request).__name__}"
            )
        created_at = _normalize_positive_float(
            created_at,
            field_name="created_at",
        )
        self._ensure_pipeline_caller_role(context.caller_role)
        self._ensure_selectable_recipient_role(
            request.recipient_role,
        )
        if request.recipient_role == context.caller_role:
            raise ValueError(
                f"self_consultation_forbidden:{context.caller_role}"
            )
        if len(request.question) > self._policy.max_question_chars:
            raise ValueError(
                "consultation_question_too_long:"
                f"{len(request.question)}>{self._policy.max_question_chars}"
            )

        thread = self._resolve_task_thread(context, created_at=created_at)
        request_created_at = max(created_at, thread.last_message_at)
        request_message = self._publish_request_message(
            AgentRequest(
                project_id=context.project_id,
                thread_id=thread.thread_id,
                sender_role=context.caller_role,
                recipient_role=request.recipient_role,
                body=request.question,
                created_at=request_created_at,
            )
        )

        self._last_dispatch_usage = None
        augmentation_block = self._build_specialist_augmentation_block(
            request.recipient_role,
            context.specialization_hints,
        )
        consult_request = LLMRequest(
            agent_role=request.recipient_role,
            messages=(
                {
                    "role": "system",
                    "content": self._build_consult_system_prompt(
                        caller_role=context.caller_role,
                        recipient_role=request.recipient_role,
                    ),
                },
                {
                    "role": "user",
                    "content": self._build_consult_user_prompt(
                        context,
                        request,
                        augmentation_block=augmentation_block,
                    ),
                },
            ),
        )
        response = self._dispatcher.dispatch(consult_request, self._tier)
        self._last_dispatch_usage = (
            response.prompt_tokens,
            response.completion_tokens,
            len(response.attempts),
        )
        nested_request = self.parse_consultation_request(response.text)
        if nested_request is not None:
            raise ValueError(
                "nested_consultation_not_allowed:"
                f"{request.recipient_role}"
            )
        answer_text = _normalize_text(
            response.text,
            field_name="answer_text",
        )
        reply_message = self._publish_reply_message(
            AgentReply(
                project_id=context.project_id,
                thread_id=thread.thread_id,
                sender_role=request.recipient_role,
                recipient_role=context.caller_role,
                in_reply_to=AgentMessageRef(
                    project_id=request_message.project_id,
                    thread_id=request_message.thread_id,
                    message_id=request_message.message_id,
                ),
                body=answer_text,
                created_at=math.nextafter(
                    request_message.created_at,
                    math.inf,
                ),
            )
        )
        return AgentConsultationResult(
            request_message=request_message,
            reply_message=reply_message,
            recipient_role=request.recipient_role,
            answer_text=answer_text,
        )

    def _ensure_pipeline_caller_role(self, role: str) -> None:
        normalized_role = _normalize_identifier(role, field_name="caller_role")
        if normalized_role not in REQUIRED_ROLES:
            raise ValueError(f"unknown_caller_role:{normalized_role}")
        try:
            self._tier.dispatch_chain_for(normalized_role)
        except Exception as exc:
            raise ValueError(f"unknown_caller_role:{normalized_role}") from exc

    def _ensure_selectable_recipient_role(self, role: str) -> None:
        normalized_role = _normalize_identifier(role, field_name="recipient_role")
        if not is_selectable_agent_role(normalized_role):
            raise ValueError(f"unknown_recipient_role:{normalized_role}")
        try:
            self._tier.dispatch_chain_for(normalized_role)
        except Exception as exc:
            raise ValueError(f"unknown_recipient_role:{normalized_role}") from exc

    def _resolve_task_thread(
        self,
        context: AgentCollaborationContext,
        *,
        created_at: float,
    ) -> ProjectThread:
        if isinstance(self._bus, ThrottledProjectingAgentBus):
            thread = self._bus.projecting_bus.get_or_open_task_thread(
                context.project_id,
                context.task_id,
                opened_by_role=context.thread.opened_by_role,
                created_at=created_at,
            )
        else:
            thread = self._bus.get_or_open_task_thread(
                context.project_id,
                context.task_id,
                opened_by_role=context.thread.opened_by_role,
                created_at=created_at,
            )
        if thread.thread_id != context.thread.thread_id:
            raise ValueError(
                "consultation_thread_mismatch:"
                f"{thread.thread_id}!={context.thread.thread_id}"
            )
        return thread

    def _publish_request_message(self, request: AgentRequest) -> AgentMessage:
        if isinstance(self._bus, ThrottledProjectingAgentBus):
            return self._bus.publish_request(request).source_message
        return self._bus.publish_request(request).message

    def _publish_reply_message(self, reply: AgentReply) -> AgentMessage:
        if isinstance(self._bus, ThrottledProjectingAgentBus):
            return self._bus.publish_reply(reply).source_message
        return self._bus.publish_reply(reply).message

    @staticmethod
    def _build_specialist_augmentation_block(
        recipient_role: str,
        specialization_hints: SpecializationHints,
    ) -> str | None:
        if not is_specialist_role(recipient_role):
            return None
        return _SPECIALIZATION_PROMPT_AUGMENTOR.render_block(
            recipient_role,
            specialization_hints,
        )

    @staticmethod
    def _build_consult_system_prompt(
        *,
        caller_role: str,
        recipient_role: str,
    ) -> str:
        return (
            "INTERNAL CONSULTATION MODE\n"
            f"Ты сейчас отвечаешь как {recipient_role} на короткий внутренний "
            f"вопрос от {caller_role}.\n"
            "Это не полноценный pipeline state и не owner-facing ответ.\n"
            "Верни краткий, truthful, plain-text экспертный ответ.\n"
            "Не заявляй, что код уже изменён, тесты уже запущены или задача уже "
            "выполнена, если этого нет в контексте.\n"
            "Не инициируй новых ask_another_agent запросов и не возвращай JSON "
            "consultation request."
        )

    @staticmethod
    def _build_consult_user_prompt(
        context: AgentCollaborationContext,
        request: AgentConsultationRequest,
        *,
        augmentation_block: str | None = None,
    ) -> str:
        lines = [
            "Internal consultation context",
            f"project_id: {context.project_id}",
            f"task_id: {context.task_id}",
            f"caller_role: {context.caller_role}",
        ]
        if augmentation_block is not None:
            lines.extend(
                (
                    "",
                    augmentation_block,
                )
            )
        lines.extend(
            (
                "",
                "Owner task text:",
                context.owner_task_text,
                "",
                "Caller question:",
                request.question,
                "",
                "Ответь кратко и по существу.",
            )
        )
        return "\n".join(lines)


def format_consultation_followup_block(
    result: AgentConsultationResult,
) -> str:
    if not isinstance(result, AgentConsultationResult):
        raise ValueError(
            "invalid_agent_consultation_result_type:"
            f"{type(result).__name__}"
        )
    payload = {
        "recipient_role": result.recipient_role,
        "question": result.request_message.body,
        "answer": result.answer_text,
    }
    return (
        "INTERNAL CONSULTATION TRANSCRIPT\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "Теперь верни финальный ответ строго в обычном формате для своей роли."
    )
