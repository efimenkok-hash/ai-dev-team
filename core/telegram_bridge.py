"""
core/telegram_bridge.py

Step 14a: glue layer between Telegram and the orchestrator. Pure logic —
no python-telegram-bot dependency in this module. Real Telegram I/O lives
in scripts/run_telegram_bot.py which converts PTB updates into the abstract
IncomingMessage / SendMessage interfaces this module consumes.

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
3. Whitelist check fires FIRST. Non-whitelisted senders get a single
   denial line (no further processing, no resource consumption).
4. Modality precedence inside one IncomingMessage: text > voice > photo.
   Multi-modal messages fall through in that order.
5. Voice/photo failure (Whisper/Vision exception) is reported as an
   apology from the Менеджер persona, NOT propagated to task_handler.
6. Slash commands are parsed via core.bot_commands.parse_command;
   dispatch errors (unknown handler, handler exception, non-string return)
   become apology replies.
7. Free-text messages call task_handler(text, msg). If task_handler returns
   None, bridge sends a generic "принял задачу" ack from Менеджер.
   If it returns BridgeReply, bridge formats and sends. If it raises,
   bridge sends an apology with class+message of the exception.
8. send() callable is invoked exactly once per outbound reply. Bridge
   never batches or reorders.
"""

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.agent_personas import AgentPersona, PersonaRegistry, default_registry
from core.bot_commands import CommandRegistry, parse_command
from core.confirmation_gate import (
    BatchDecision,
    ConfirmationGate,
)
from core.observability import Observability
from core.vision_client import VisionClient, VisionError
from core.whisper_client import WhisperClient, WhisperError

DEFAULT_DENIAL_MESSAGE = (
    "🔒 Доступ ограничен\n"
    "\n"
    "Этот бот обслуживает только владельца проекта."
)
DEFAULT_GENERIC_ACK = "👋 Принял задачу. Сейчас разберём."


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
        send: SendMessage,
        whisper: WhisperClient | None = None,
        vision: VisionClient | None = None,
        personas: PersonaRegistry | None = None,
        gate: ConfirmationGate | None = None,
        commands: CommandRegistry | None = None,
        task_handler: TaskHandler | None = None,
        observability: Observability | None = None,
        manager_role: str = "pm_agent",
        denial_message: str = DEFAULT_DENIAL_MESSAGE,
    ) -> None:
        if not isinstance(owner_chat_ids, frozenset):
            raise ValueError("owner_chat_ids_must_be_frozenset")
        if not owner_chat_ids:
            raise ValueError("empty_owner_chat_ids")
        for cid in owner_chat_ids:
            if not isinstance(cid, int) or isinstance(cid, bool):
                raise ValueError(f"invalid_owner_chat_id:{cid!r}")
        if not callable(send):
            raise ValueError("send_not_callable")
        if task_handler is not None and not callable(task_handler):
            raise ValueError("task_handler_not_callable")
        if not isinstance(manager_role, str) or not manager_role.strip():
            raise ValueError("empty_manager_role")
        if not isinstance(denial_message, str) or not denial_message.strip():
            raise ValueError("empty_denial_message")

        self._owner_chat_ids = owner_chat_ids
        self._send = send
        self._whisper = whisper
        self._vision = vision
        self._personas = personas if personas is not None else default_registry()
        self._gate = gate
        self._commands = commands
        self._task_handler = task_handler
        self._obs = observability
        self._denial_message = denial_message

        # Eagerly resolve the manager persona so we fail at construction
        # if manager_role is unknown.
        try:
            self._manager = self._personas.for_role(manager_role)
        except KeyError as exc:
            raise ValueError(f"unknown_manager_role:{manager_role}") from exc

    @property
    def manager_persona(self) -> AgentPersona:
        return self._manager

    def handle(self, msg: IncomingMessage) -> BridgeResult:
        """Process one incoming message. Total — never raises."""
        ctx = _BridgeContext()

        try:
            if not isinstance(msg, IncomingMessage):
                self._safe_send(
                    OutgoingMessage(
                        chat_id=getattr(msg, "chat_id", 0) or 0,
                        text=self._sign_manager(
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

            # 1. Whitelist
            if not self._is_owner(msg):
                self._safe_send(
                    OutgoingMessage(chat_id=msg.chat_id, text=self._denial_message),
                    ctx,
                )
                return BridgeResult(
                    chat_id=msg.chat_id,
                    handled=False,
                    reason="not_owner",
                    sent_count=ctx.sent_count,
                )

            # 2. Resolve text from text/voice/photo
            text = self._resolve_text(msg, ctx)
            if text is None:
                # _resolve_text already sent an apology if needed
                return BridgeResult(
                    chat_id=msg.chat_id,
                    handled=False,
                    reason="no_text_resolved",
                    sent_count=ctx.sent_count,
                    extracted_text=None,
                )
            ctx.extracted_text = text

            # 3. Slash command?
            cmd = parse_command(text)
            if cmd is not None:
                self._handle_command(cmd, msg, ctx)
                return BridgeResult(
                    chat_id=msg.chat_id,
                    handled=True,
                    reason="command",
                    sent_count=ctx.sent_count,
                    extracted_text=text,
                )

            # 4. Free-text task
            self._handle_task(text, msg, ctx)
            return BridgeResult(
                chat_id=msg.chat_id,
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
                        text=self._sign_manager(
                            f"Внутренняя ошибка моста: "
                            f"{type(exc).__name__}: {str(exc)[:200]}"
                        ),
                    ),
                    ctx,
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
                        text=self._sign_manager(
                            "🎙 Голосовые сообщения сейчас не обрабатываются.\n"
                            "\n"
                            "Подключите OPENAI_API_KEY в .env, чтобы включить."
                        ),
                    ),
                    ctx,
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
                        text=self._sign_manager(
                            f"🎙 Не удалось расшифровать голосовое\n"
                            f"\n"
                            f"Причина: {_short_err(exc)}\n"
                            f"\n"
                            f"Попробуйте, пожалуйста, набрать текстом."
                        ),
                    ),
                    ctx,
                )
                return None
            return result.text

        if msg.photo_bytes:
            if self._vision is None:
                self._safe_send(
                    OutgoingMessage(
                        chat_id=msg.chat_id,
                        text=self._sign_manager(
                            "🖼 Изображения сейчас не обрабатываются.\n"
                            "\n"
                            "Подключите OPENROUTER_API_KEY в .env, чтобы включить."
                        ),
                    ),
                    ctx,
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
                        text=self._sign_manager(
                            f"🖼 Не удалось распознать изображение\n"
                            f"\n"
                            f"Причина: {_short_err(exc)}\n"
                            f"\n"
                            f"Попробуйте описать проблему текстом."
                        ),
                    ),
                    ctx,
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
                    text=self._sign_manager(
                        "Команды сейчас не зарегистрированы."
                    ),
                ),
                ctx,
            )
            return
        try:
            reply_text = self._commands.dispatch(cmd, ctx=msg)
        except KeyError:
            self._safe_send(
                OutgoingMessage(
                    chat_id=msg.chat_id,
                    text=self._sign_manager(
                        f"Команда /{cmd.name.value} не имеет хендлера."
                    ),
                ),
                ctx,
            )
            return
        except Exception as exc:
            self._safe_send(
                OutgoingMessage(
                    chat_id=msg.chat_id,
                    text=self._sign_manager(
                        f"Ошибка при выполнении /{cmd.name.value}: "
                        f"{_short_err(exc)}"
                    ),
                ),
                ctx,
            )
            return
        self._safe_send(
            OutgoingMessage(
                chat_id=msg.chat_id,
                text=self._sign_manager(reply_text),
            ),
            ctx,
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
                    text=self._sign_manager(
                        "Задачи сейчас не обрабатываются: "
                        "task_handler не подключён."
                    ),
                ),
                ctx,
            )
            return
        try:
            reply = self._task_handler(text, msg)
        except Exception as exc:
            self._safe_send(
                OutgoingMessage(
                    chat_id=msg.chat_id,
                    text=self._sign_manager(
                        f"Не удалось обработать задачу: {_short_err(exc)}"
                    ),
                ),
                ctx,
            )
            return

        if reply is None:
            self._safe_send(
                OutgoingMessage(
                    chat_id=msg.chat_id,
                    text=self._sign_manager(DEFAULT_GENERIC_ACK),
                ),
                ctx,
            )
            return
        if not isinstance(reply, BridgeReply):
            self._safe_send(
                OutgoingMessage(
                    chat_id=msg.chat_id,
                    text=self._sign_manager(
                        "Внутренняя ошибка: некорректный формат ответа от обработчика."
                    ),
                ),
                ctx,
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
                )
                return

        # Plain reply.
        self._safe_send(
            OutgoingMessage(
                chat_id=msg.chat_id,
                text=self._sign_with_role(reply.persona_role, reply.body),
            ),
            ctx,
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

    def _sign_manager(self, body: str) -> str:
        return self._manager.format_signature(body)

    def _sign_with_role(self, role: str, body: str) -> str:
        try:
            persona = self._personas.for_role(role)
        except KeyError:
            # Fall back to manager if the role is unknown.
            return self._sign_manager(
                f"[неизвестная роль '{role}'] {body}"
            )
        return persona.format_signature(body)

    def _safe_send(
        self,
        out: OutgoingMessage,
        ctx: _BridgeContext,
    ) -> None:
        """Calls send() once. Suppresses transport errors so handle() stays total."""
        try:
            self._send(out)
            ctx.sent_count += 1
        except Exception as exc:
            ctx.notes.append(f"send_failed:{type(exc).__name__}:{exc}")


def _short_err(exc: Any, limit: int = 200) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= limit else text[:limit] + "...[truncated]"
