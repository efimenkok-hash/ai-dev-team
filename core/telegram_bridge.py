"""
core/telegram_bridge.py

Step 14a: glue layer between Telegram and the orchestrator. Pure logic —
no python-telegram-bot dependency in this module. Real Telegram I/O lives
in scripts/run_telegram_bot.py which converts PTB updates into the abstract
IncomingMessage / OutgoingEnvelope transport seam this module consumes.

The bridge orchestrates:
  - whitelist enforcement (only owner_chat_ids can drive the bot)
  - input modality routing (text / voice → Whisper / photo → Vision)
  - slash-command dispatch via CommandRegistry
  - free-text task delegation to a user-supplied task_handler
  - persona-signed replies through PersonaRegistry
  - confirmation gating with Russian-language rationale

Everything I/O-ish is injected; tests run without network or PTB.

CONTRACTS:
1. IncomingMessage / OutgoingMessage / BridgeReply / BridgeResult are
   frozen dataclasses with __post_init__ validation.
2. handle(msg) is total: never raises. All exceptions become persona-signed
   apology replies. The bridge logs to observability if available, then
   recovers.
3. Legacy mode (without ProjectContextResolver) keeps the original owner
   whitelist behaviour. Project-aware mode resolves Telegram chat context
   first and gates only project-sensitive actions when context is missing.
4. Modality precedence inside one IncomingMessage: text > voice > photo.
   Multi-modal messages fall through in that order.
5. Voice/photo failure (Whisper/Vision exception) is reported as an
   apology from the Coordinator persona, NOT propagated to task_handler.
6. Slash commands are parsed via core.bot_commands.parse_command;
   dispatch errors (unknown handler, handler exception, non-string return)
   become apology replies.
7. Free-text messages call task_handler(text, msg). If task_handler returns
   None, bridge sends a generic "принял задачу" ack from Coordinator.
   If it returns BridgeReply, bridge formats and sends. If it raises,
   bridge sends an apology with class+message of the exception.
8. In project-aware mode, free-text plus project-sensitive commands
   (`/push`, `/pr`) are blocked before handler execution when project context
   cannot be resolved.
9. The outbound transport callable is invoked exactly once per outbound
   reply. Bridge never batches or reorders, and it preserves sender_role
   inside OutgoingEnvelope for the transport boundary.
"""

import contextlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from core.agent_personas import AgentPersona, PersonaRegistry, default_registry
from core.bot_commands import CommandRegistry, parse_command
from core.confirmation_gate import (
    BatchDecision,
    ConfirmationGate,
)
from core.coordinator_role import (
    COORDINATOR_ROLE,
    normalize_coordinator_role,
    resolve_coordinator_persona,
)
from core.observability import Observability
from core.owner_dm_routing import OwnerDmRoutingService
from core.project_context import VALID_PROJECT_CONTEXT_SOURCES, ProjectContextResolver
from core.vision_client import VisionClient, VisionError
from core.whisper_client import WhisperClient, WhisperError

DEFAULT_DENIAL_MESSAGE = (
    "🔒 Доступ ограничен\n"
    "\n"
    "Этот бот обслуживает только владельца проекта."
)
DEFAULT_GENERIC_ACK = "👋 Принял задачу. Сейчас разберём."
_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ROLE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PROJECT_CONTEXT_BLOCKED_COMMANDS = frozenset({"push", "pr"})
_DIRECT_AGENT_DM_PROJECT_CONTEXT_SOURCES = frozenset(
    {
        "agent_dm_explicit_project",
        "agent_dm_active_session",
        "agent_dm_single_candidate",
    }
)


@dataclass(frozen=True)
class IncomingMessage:
    chat_id: int
    user_id: int
    message_id: int
    text: str | None = None
    voice_bytes: bytes | None = None
    voice_mime: str = "audio/ogg"
    voice_duration_seconds: float | None = None
    photo_bytes: bytes | None = None
    photo_mime: str = "image/jpeg"
    timestamp: float = 0.0
    project_id: str | None = None
    project_slug: str | None = None
    project_context_source: str | None = None
    project_context_reason: str | None = None
    incoming_bot_role: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.chat_id, int) or isinstance(self.chat_id, bool):
            raise ValueError(f"invalid_chat_id:{self.chat_id!r}")
        if not isinstance(self.user_id, int) or isinstance(self.user_id, bool):
            raise ValueError(f"invalid_user_id:{self.user_id!r}")
        if not isinstance(self.message_id, int) or isinstance(self.message_id, bool):
            raise ValueError(f"invalid_message_id:{self.message_id!r}")
        if self.text is not None and not isinstance(self.text, str):
            raise ValueError("non_string_text")
        if self.voice_bytes is not None and not isinstance(self.voice_bytes, (bytes, bytearray)):
            raise ValueError("non_bytes_voice")
        if self.photo_bytes is not None and not isinstance(self.photo_bytes, (bytes, bytearray)):
            raise ValueError("non_bytes_photo")
        if not isinstance(self.voice_mime, str) or not self.voice_mime.strip():
            raise ValueError("empty_voice_mime")
        if not isinstance(self.photo_mime, str) or not self.photo_mime.strip():
            raise ValueError("empty_photo_mime")
        if not isinstance(self.timestamp, (int, float)) or isinstance(self.timestamp, bool):
            raise ValueError("invalid_timestamp")
        if self.project_id is not None:
            if not isinstance(self.project_id, str) or not self.project_id.strip():
                raise ValueError("empty_project_id")
            normalized_project_id = self.project_id.strip().lower()
            if not normalized_project_id.isascii():
                raise ValueError(f"non_ascii_project_id:{normalized_project_id}")
            if not _PROJECT_ID_RE.fullmatch(normalized_project_id):
                raise ValueError(f"invalid_project_id:{normalized_project_id}")
            object.__setattr__(self, "project_id", normalized_project_id)
        if self.project_slug is not None:
            if not isinstance(self.project_slug, str) or not self.project_slug.strip():
                raise ValueError("empty_project_slug")
            object.__setattr__(self, "project_slug", self.project_slug.strip())
        if (
            self.project_context_source is not None
            and (
                not isinstance(self.project_context_source, str)
                or self.project_context_source
                not in (
                    VALID_PROJECT_CONTEXT_SOURCES
                    | _DIRECT_AGENT_DM_PROJECT_CONTEXT_SOURCES
                )
            )
        ):
            raise ValueError(
                "invalid_project_context_source:"
                f"{self.project_context_source!r}"
            )
        if self.project_context_reason is not None:
            if (
                not isinstance(self.project_context_reason, str)
                or not self.project_context_reason.strip()
            ):
                raise ValueError("invalid_project_context_reason")
            object.__setattr__(
                self,
                "project_context_reason",
                self.project_context_reason.strip(),
            )
        if self.incoming_bot_role is not None:
            if not isinstance(self.incoming_bot_role, str):
                raise ValueError(
                    "invalid_incoming_bot_role_type:"
                    f"{type(self.incoming_bot_role).__name__}"
                )
            normalized_role = self.incoming_bot_role.strip().lower()
            if not normalized_role:
                raise ValueError("empty_incoming_bot_role")
            if not normalized_role.isascii():
                raise ValueError(f"non_ascii_incoming_bot_role:{normalized_role}")
            if not _ROLE_ID_RE.fullmatch(normalized_role):
                raise ValueError(f"invalid_incoming_bot_role:{normalized_role}")
            object.__setattr__(self, "incoming_bot_role", normalized_role)
        if self.project_id is None and self.project_slug is not None:
            raise ValueError("project_slug_requires_project_id")
        if self.project_context_source == "none" and self.project_id is not None:
            raise ValueError("none_project_context_forbids_project_id")
        if self.project_context_source in {
            "bound_chat",
            "owner_dm_single_project",
            *_DIRECT_AGENT_DM_PROJECT_CONTEXT_SOURCES,
        } and self.project_id is None:
            raise ValueError(
                "resolved_project_context_requires_project_id"
            )
        # Must contain at least one modality.
        if (
            (self.text is None or not self.text.strip())
            and not self.voice_bytes
            and not self.photo_bytes
        ):
            raise ValueError("empty_message_all_modalities")


@dataclass(frozen=True)
class OutgoingMessage:
    chat_id: int
    text: str
    reply_to_message_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.chat_id, int) or isinstance(self.chat_id, bool):
            raise ValueError("invalid_chat_id")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("empty_text")
        if self.reply_to_message_id is not None and (
            not isinstance(self.reply_to_message_id, int)
            or isinstance(self.reply_to_message_id, bool)
        ):
            raise ValueError("invalid_reply_to")


@dataclass(frozen=True)
class OutgoingEnvelope:
    message: OutgoingMessage
    sender_role: str
    delivery_role: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.message, OutgoingMessage):
            raise ValueError(
                "invalid_outgoing_message_type:"
                f"{type(self.message).__name__}"
            )
        if not isinstance(self.sender_role, str):
            raise ValueError(
                "invalid_sender_role_type:"
                f"{type(self.sender_role).__name__}"
            )
        normalized_role = self.sender_role.strip().lower()
        if not normalized_role:
            raise ValueError("empty_sender_role")
        if not normalized_role.isascii():
            raise ValueError(f"non_ascii_sender_role:{normalized_role}")
        if not _ROLE_ID_RE.fullmatch(normalized_role):
            raise ValueError(f"invalid_sender_role:{normalized_role}")
        object.__setattr__(self, "sender_role", normalized_role)
        if self.delivery_role is not None:
            if not isinstance(self.delivery_role, str):
                raise ValueError(
                    "invalid_delivery_role_type:"
                    f"{type(self.delivery_role).__name__}"
                )
            normalized_delivery_role = self.delivery_role.strip().lower()
            if not normalized_delivery_role:
                raise ValueError("empty_delivery_role")
            if not normalized_delivery_role.isascii():
                raise ValueError(
                    f"non_ascii_delivery_role:{normalized_delivery_role}"
                )
            if not _ROLE_ID_RE.fullmatch(normalized_delivery_role):
                raise ValueError(
                    f"invalid_delivery_role:{normalized_delivery_role}"
                )
            object.__setattr__(
                self,
                "delivery_role",
                normalized_delivery_role,
            )

    @property
    def chat_id(self) -> int:
        return self.message.chat_id

    @property
    def text(self) -> str:
        return self.message.text

    @property
    def reply_to_message_id(self) -> int | None:
        return self.message.reply_to_message_id


@dataclass(frozen=True)
class BridgeReply:
    """What task_handler returns. Bridge formats it into OutgoingMessage."""

    persona_role: str
    body: str
    pending_actions: tuple = ()  # tuple of ActionDescriptor; if any, gate runs

    def __post_init__(self) -> None:
        if not isinstance(self.persona_role, str) or not self.persona_role.strip():
            raise ValueError("empty_persona_role")
        if not isinstance(self.body, str) or not self.body.strip():
            raise ValueError("empty_body")
        if not isinstance(self.pending_actions, tuple):
            raise ValueError("pending_actions_must_be_tuple")


@dataclass(frozen=True)
class BridgeResult:
    """Audit trail of one handle() invocation."""

    chat_id: int
    handled: bool
    reason: str
    sent_count: int = 0
    extracted_text: str | None = None


# Type aliases for injected dependencies.
SendMessage = Callable[[OutgoingMessage], None]
SendEnvelope = Callable[[OutgoingEnvelope], None]
TaskHandler = Callable[[str, IncomingMessage], "BridgeReply | None"]


@dataclass
class _BridgeContext:
    """Mutable per-handle scratch — tracks what has been sent so far."""
    sent_count: int = 0
    extracted_text: str | None = None
    notes: list[str] = field(default_factory=list)


class TelegramBridge:
    def __init__(
        self,
        *,
        owner_chat_ids: frozenset[int],
        send: SendMessage | None = None,
        send_envelope: SendEnvelope | None = None,
        whisper: WhisperClient | None = None,
        vision: VisionClient | None = None,
        personas: PersonaRegistry | None = None,
        gate: ConfirmationGate | None = None,
        commands: CommandRegistry | None = None,
        task_handler: TaskHandler | None = None,
        observability: Observability | None = None,
        coordinator_role: str = COORDINATOR_ROLE,
        manager_role: str | None = None,
        denial_message: str = DEFAULT_DENIAL_MESSAGE,
        project_context_resolver: ProjectContextResolver | None = None,
    ) -> None:
        if not isinstance(owner_chat_ids, frozenset):
            raise ValueError("owner_chat_ids_must_be_frozenset")
        if not owner_chat_ids:
            raise ValueError("empty_owner_chat_ids")
        for cid in owner_chat_ids:
            if not isinstance(cid, int) or isinstance(cid, bool):
                raise ValueError(f"invalid_owner_chat_id:{cid!r}")
        if send is not None and not callable(send):
            raise ValueError("send_not_callable")
        if send_envelope is not None and not callable(send_envelope):
            raise ValueError("send_envelope_not_callable")
        if send is None and send_envelope is None:
            raise ValueError("send_not_callable")
        if task_handler is not None and not callable(task_handler):
            raise ValueError("task_handler_not_callable")
        if not isinstance(denial_message, str) or not denial_message.strip():
            raise ValueError("empty_denial_message")
        if (
            project_context_resolver is not None
            and not isinstance(project_context_resolver, ProjectContextResolver)
        ):
            raise ValueError("invalid_project_context_resolver")
        normalized_coordinator_role = normalize_coordinator_role(
            manager_role if manager_role is not None else coordinator_role
        )

        self._owner_chat_ids = owner_chat_ids
        self._send = send
        self._send_envelope = (
            send_envelope
            if send_envelope is not None
            else self._adapt_legacy_send(send)
        )
        self._whisper = whisper
        self._vision = vision
        self._personas = personas if personas is not None else default_registry()
        self._gate = gate
        self._commands = commands
        self._task_handler = task_handler
        self._obs = observability
        self._denial_message = denial_message
        self._project_context_resolver = project_context_resolver
        self._coordinator_role = normalized_coordinator_role
        self._owner_dm_routing = OwnerDmRoutingService()

        self._coordinator = resolve_coordinator_persona(self._personas)

    @property
    def coordinator_role(self) -> str:
        return self._coordinator_role

    @property
    def coordinator_persona(self) -> AgentPersona:
        return self._coordinator

    @property
    def manager_persona(self) -> AgentPersona:
        """Legacy compatibility alias for older call sites/tests."""
        return self._coordinator

    def set_send_envelope(self, send_envelope: SendEnvelope) -> None:
        if not callable(send_envelope):
            raise ValueError("send_envelope_not_callable")
        self._send_envelope = send_envelope

    def handle(self, msg: IncomingMessage) -> BridgeResult:
        """Process one incoming message. Total — never raises."""
        ctx = _BridgeContext()

        try:
            if not isinstance(msg, IncomingMessage):
                self._safe_send(
                    OutgoingMessage(
                        chat_id=getattr(msg, "chat_id", 0) or 0,
                        text=self._sign_coordinator(
                            "Невалидный формат входящего сообщения."
                        ),
                    ),
                    ctx,
                )
                return BridgeResult(
                    chat_id=getattr(msg, "chat_id", 0) or 0,
                    handled=False,
                    reason="invalid_message_type",
                    sent_count=ctx.sent_count,
                )

            # 1. Legacy whitelist or project-aware context resolution.
            resolved_msg = msg
            if self._project_context_resolver is None:
                if not self._is_owner(msg):
                    self._safe_send(
                        OutgoingMessage(
                            chat_id=msg.chat_id,
                            text=self._denial_message,
                        ),
                        ctx,
                        incoming=msg,
                    )
                    return BridgeResult(
                        chat_id=msg.chat_id,
                        handled=False,
                        reason="not_owner",
                        sent_count=ctx.sent_count,
                    )
            else:
                resolved_msg = self._apply_project_context(msg)
                if self._should_block_for_missing_project_context(resolved_msg):
                    self._safe_send(
                        OutgoingMessage(
                            chat_id=resolved_msg.chat_id,
                            text=self._sign_coordinator(
                                self._format_missing_project_context_message(
                                    resolved_msg.project_context_reason
                                )
                            ),
                        ),
                        ctx,
                        incoming=resolved_msg,
                    )
                    return BridgeResult(
                        chat_id=resolved_msg.chat_id,
                        handled=False,
                        reason="project_context_missing",
                        sent_count=ctx.sent_count,
                    )

            # 2. Resolve text from text/voice/photo
            text = self._resolve_text(resolved_msg, ctx)
            if text is None:
                return BridgeResult(
                    chat_id=resolved_msg.chat_id,
                    handled=False,
                    reason="no_text_resolved",
                    sent_count=ctx.sent_count,
                    extracted_text=None,
                )
            ctx.extracted_text = text

            # 3. Slash command?
            cmd = parse_command(text)
            if cmd is not None:
                self._handle_command(cmd, resolved_msg, ctx)
                return BridgeResult(
                    chat_id=resolved_msg.chat_id,
                    handled=True,
                    reason="command",
                    sent_count=ctx.sent_count,
                    extracted_text=text,
                )

            # 4. Free-text task
            self._handle_task(text, resolved_msg, ctx)
            return BridgeResult(
                chat_id=resolved_msg.chat_id,
                handled=True,
                reason="task",
                sent_count=ctx.sent_count,
                extracted_text=text,
            )
        except Exception as exc:
            # Last-resort safety net. Bridge must not crash the runner loop.
            with contextlib.suppress(Exception):
                self._safe_send(
                    OutgoingMessage(
                        chat_id=getattr(msg, "chat_id", 0) or 0,
                        text=self._sign_coordinator(
                            f"Внутренняя ошибка моста: "
                            f"{type(exc).__name__}: {str(exc)[:200]}"
                        ),
                        ),
                        ctx,
                        incoming=msg,
                    )
            return BridgeResult(
                chat_id=getattr(msg, "chat_id", 0) or 0,
                handled=False,
                reason=f"bridge_exception:{type(exc).__name__}",
                sent_count=ctx.sent_count,
            )

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _is_owner(self, msg: IncomingMessage) -> bool:
        return (
            msg.chat_id in self._owner_chat_ids
            or msg.user_id in self._owner_chat_ids
        )

    def _apply_project_context(
        self,
        msg: IncomingMessage,
    ) -> IncomingMessage:
        if self._project_context_resolver is None:
            return msg
        resolution = self._project_context_resolver.resolve_telegram_context(
            chat_id=msg.chat_id,
            user_id=msg.user_id,
        )
        if resolution.snapshot is None:
            return replace(
                msg,
                project_id=None,
                project_slug=None,
                project_context_source=resolution.source,
                project_context_reason=resolution.reason,
            )
        return replace(
            msg,
            project_id=resolution.snapshot.project.project_id,
            project_slug=resolution.snapshot.project.slug,
            project_context_source=resolution.source,
            project_context_reason=resolution.reason,
        )

    def _should_block_for_missing_project_context(
        self,
        msg: IncomingMessage,
    ) -> bool:
        if self._project_context_resolver is None:
            return False
        if msg.project_context_source != "none":
            return False
        if (
            msg.project_context_reason
            == "owner_dm_requires_explicit_project_chat"
            and msg.incoming_bot_role is not None
            and msg.incoming_bot_role != COORDINATOR_ROLE
            and self._owner_dm_routing.is_owner_dm_message(msg)
        ):
            # Secondary owner DMs may resolve project context later via
            # explicit slug or one active session; do not block them here.
            return False
        return _message_requires_project_context(msg)

    def _format_missing_project_context_message(self, reason: str | None) -> str:
        if reason == "owner_dm_requires_explicit_project_chat":
            return (
                "⚠️ Проект не определён.\n"
                "\n"
                "Для owner DM при нескольких проектах нужен явный проектный чат."
            )
        if reason == "project_chat_not_bound":
            return (
                "⚠️ Проект не определён.\n"
                "\n"
                "Этот чат ещё не привязан к проекту."
            )
        return (
            "⚠️ Проект не определён.\n"
            "\n"
            "Не удалось определить проектный контекст для этого действия."
            + (
                f"\n\nТехническая причина: {reason}"
                if isinstance(reason, str) and reason.strip()
                else ""
            )
        )

    def _resolve_text(
        self,
        msg: IncomingMessage,
        ctx: _BridgeContext,
    ) -> str | None:
        # Precedence: text > voice > photo
        if msg.text is not None and msg.text.strip():
            return msg.text.strip()

        if msg.voice_bytes:
            if self._whisper is None:
                self._safe_send(
                    OutgoingMessage(
                        chat_id=msg.chat_id,
                        text=self._sign_coordinator(
                            "🎙 Голосовые сообщения сейчас не обрабатываются.\n"
                            "\n"
                            "Подключите OPENAI_API_KEY в .env, чтобы включить."
                        ),
                        ),
                        ctx,
                        incoming=msg,
                    )
                return None
            try:
                result = self._whisper.transcribe(
                    bytes(msg.voice_bytes),
                    mime_type=msg.voice_mime,
                    filename="voice.ogg",
                    language="ru",
                )
            except (WhisperError, ValueError) as exc:
                self._safe_send(
                    OutgoingMessage(
                        chat_id=msg.chat_id,
                        text=self._sign_coordinator(
                            f"🎙 Не удалось расшифровать голосовое\n"
                            f"\n"
                            f"Причина: {_short_err(exc)}\n"
                            f"\n"
                            f"Попробуйте, пожалуйста, набрать текстом."
                        ),
                        ),
                        ctx,
                        incoming=msg,
                    )
                return None
            return result.text

        if msg.photo_bytes:
            if self._vision is None:
                self._safe_send(
                    OutgoingMessage(
                        chat_id=msg.chat_id,
                        text=self._sign_coordinator(
                            "🖼 Изображения сейчас не обрабатываются.\n"
                            "\n"
                            "Подключите OPENROUTER_API_KEY в .env, чтобы включить."
                        ),
                        ),
                        ctx,
                        incoming=msg,
                    )
                return None
            try:
                result = self._vision.describe(
                    bytes(msg.photo_bytes),
                    mime_type=msg.photo_mime,
                )
            except (VisionError, ValueError) as exc:
                self._safe_send(
                    OutgoingMessage(
                        chat_id=msg.chat_id,
                        text=self._sign_coordinator(
                            f"🖼 Не удалось распознать изображение\n"
                            f"\n"
                            f"Причина: {_short_err(exc)}\n"
                            f"\n"
                            f"Попробуйте описать проблему текстом."
                        ),
                    ),
                    ctx,
                    incoming=msg,
                )
                return None
            return result.text

        # Should be unreachable thanks to IncomingMessage __post_init__.
        return None

    def _handle_command(
        self,
        cmd,
        msg: IncomingMessage,
        ctx: _BridgeContext,
    ) -> None:
        if self._commands is None:
            self._safe_send(
                OutgoingMessage(
                    chat_id=msg.chat_id,
                    text=self._sign_coordinator(
                        "Команды сейчас не зарегистрированы."
                    ),
                ),
                ctx,
                incoming=msg,
            )
            return
        try:
            reply_text = self._commands.dispatch(cmd, ctx=msg)
        except KeyError:
            self._safe_send(
                OutgoingMessage(
                    chat_id=msg.chat_id,
                    text=self._sign_coordinator(
                        f"Команда /{cmd.name.value} не имеет хендлера."
                    ),
                ),
                ctx,
                incoming=msg,
            )
            return
        except Exception as exc:
            self._safe_send(
                OutgoingMessage(
                    chat_id=msg.chat_id,
                    text=self._sign_coordinator(
                        f"Ошибка при выполнении /{cmd.name.value}: "
                        f"{_short_err(exc)}"
                    ),
                ),
                ctx,
                incoming=msg,
            )
            return
        self._safe_send(
            OutgoingMessage(
                chat_id=msg.chat_id,
                text=self._sign_coordinator(reply_text),
            ),
            ctx,
            incoming=msg,
        )

    def _handle_task(
        self,
        text: str,
        msg: IncomingMessage,
        ctx: _BridgeContext,
    ) -> None:
        if self._task_handler is None:
            self._safe_send(
                OutgoingMessage(
                    chat_id=msg.chat_id,
                    text=self._sign_coordinator(
                        "Задачи сейчас не обрабатываются: "
                        "task_handler не подключён."
                    ),
                ),
                ctx,
                incoming=msg,
            )
            return
        try:
            reply = self._task_handler(text, msg)
        except Exception as exc:
            self._safe_send(
                OutgoingMessage(
                    chat_id=msg.chat_id,
                    text=self._sign_coordinator(
                        f"Не удалось обработать задачу: {_short_err(exc)}"
                    ),
                ),
                ctx,
                incoming=msg,
            )
            return

        if reply is None:
            self._safe_send(
                OutgoingMessage(
                    chat_id=msg.chat_id,
                    text=self._sign_coordinator(DEFAULT_GENERIC_ACK),
                ),
                ctx,
                incoming=msg,
            )
            return
        if not isinstance(reply, BridgeReply):
            self._safe_send(
                OutgoingMessage(
                    chat_id=msg.chat_id,
                    text=self._sign_coordinator(
                        "Внутренняя ошибка: некорректный формат ответа от обработчика."
                    ),
                ),
                ctx,
                incoming=msg,
            )
            return

        # Pending actions → run them through the gate first.
        if reply.pending_actions and self._gate is not None:
            batch: BatchDecision = self._gate.evaluate_batch(reply.pending_actions)
            if batch.require_any_confirmation:
                ask_text = self._format_ask_message(reply, batch)
                self._safe_send(
                    OutgoingMessage(
                        chat_id=msg.chat_id,
                        text=self._sign_with_role(reply.persona_role, ask_text),
                    ),
                    ctx,
                    sender_role=reply.persona_role,
                    incoming=msg,
                )
                return

        # Plain reply.
        self._safe_send(
            OutgoingMessage(
                chat_id=msg.chat_id,
                text=self._sign_with_role(reply.persona_role, reply.body),
            ),
            ctx,
            sender_role=reply.persona_role,
            incoming=msg,
        )

    def _format_ask_message(
        self,
        reply: BridgeReply,
        batch: BatchDecision,
    ) -> str:
        lines = [reply.body, "", "⚠️ Требуется ваше подтверждение:", ""]
        for d in batch.asks():
            lines.append(f"  • {d.reason}")
        lines.append("")
        lines.append("Ответьте «да» / «нет».")
        return "\n".join(lines)

    def _sign_coordinator(self, body: str) -> str:
        return self._coordinator.format_signature(body)

    def _sign_with_role(self, role: str, body: str) -> str:
        try:
            persona = self._personas.for_role(role)
        except KeyError:
            # Fall back to the control-plane Coordinator if the role is unknown.
            return self._sign_coordinator(
                f"[неизвестная роль '{role}'] {body}"
            )
        return persona.format_signature(body)

    def _safe_send(
        self,
        out: OutgoingMessage,
        ctx: _BridgeContext,
        *,
        sender_role: str = COORDINATOR_ROLE,
        incoming: IncomingMessage | None = None,
    ) -> bool:
        """Calls send() once. Suppresses transport errors so handle() stays total."""
        envelope = OutgoingEnvelope(
            message=out,
            sender_role=sender_role,
            delivery_role=self._resolve_delivery_role(incoming, sender_role),
        )
        try:
            self._send_envelope(envelope)
            ctx.sent_count += 1
            return True
        except Exception as exc:
            ctx.notes.append(f"send_failed:{type(exc).__name__}:{exc}")
            return False

    def _resolve_delivery_role(
        self,
        msg: IncomingMessage | None,
        sender_role: str,
    ) -> str | None:
        if msg is None or msg.incoming_bot_role is None:
            return None
        if not self._owner_dm_routing.is_owner_dm_message(msg):
            return None
        try:
            context = self._owner_dm_routing.build_context(msg)
            return self._owner_dm_routing.resolve_delivery_role(
                context,
                sender_role,
            )
        except ValueError:
            return None

    def _adapt_legacy_send(self, send: SendMessage | None) -> SendEnvelope:
        if send is None or not callable(send):
            raise ValueError("send_not_callable")

        def _send_envelope(envelope: OutgoingEnvelope) -> None:
            send(envelope.message)

        return _send_envelope


def _short_err(exc: Any, limit: int = 200) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def _command_requires_project_context(cmd) -> bool:
    return cmd.name.value in _PROJECT_CONTEXT_BLOCKED_COMMANDS


def _message_requires_project_context(msg: IncomingMessage) -> bool:
    if msg.text is not None and msg.text.strip():
        cmd = parse_command(msg.text.strip())
        if cmd is not None:
            return _command_requires_project_context(cmd)
        return True
    return True
