"""Tests for core.telegram_bridge (Step 14a: glue layer between Telegram and orchestrator).

Bridge is fully testable without networking — every dependency (whisper,
vision, send-callable, task_handler, command registry) is injected.
"""

import pytest

from core.agent_personas import default_registry
from core.bot_commands import CommandName, CommandRegistry
from core.confirmation_gate import (
    ActionDescriptor,
    ActionKind,
    ConfirmationGate,
)
from core.telegram_bridge import (
    DEFAULT_DENIAL_MESSAGE,
    DEFAULT_GENERIC_ACK,
    BridgeReply,
    BridgeResult,
    IncomingMessage,
    OutgoingMessage,
    TelegramBridge,
)
from core.vision_client import VisionError, VisionResult
from core.whisper_client import TranscriptionResult, WhisperError

OWNER_CHAT_ID = 11111
INTRUDER_CHAT_ID = 22222

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class CapturingSender:
    def __init__(self, raise_exc=None):
        self.raise_exc = raise_exc
        self.sent: list[OutgoingMessage] = []

    def __call__(self, msg: OutgoingMessage) -> None:
        if self.raise_exc is not None:
            raise self.raise_exc
        self.sent.append(msg)


class FakeWhisper:
    """Minimal stand-in for WhisperClient."""

    def __init__(self, result_text="расшифровано", raise_exc=None):
        self.result_text = result_text
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def transcribe(self, audio_bytes, *, mime_type, filename, language=None):
        self.calls.append({
            "bytes_len": len(audio_bytes),
            "mime_type": mime_type,
            "filename": filename,
            "language": language,
        })
        if self.raise_exc is not None:
            raise self.raise_exc
        return TranscriptionResult(
            text=self.result_text,
            duration_seconds=3.0,
            cost_usd=0.0003,
            cost_estimated=False,
            language="ru",
        )


class FakeVision:
    def __init__(self, result_text="на скриншоте — TypeError", raise_exc=None):
        self.result_text = result_text
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def describe(self, image_bytes, *, mime_type, prompt=None, model=None, max_tokens=None):
        self.calls.append({
            "bytes_len": len(image_bytes),
            "mime_type": mime_type,
        })
        if self.raise_exc is not None:
            raise self.raise_exc
        return VisionResult(
            text=self.result_text,
            model="openai/gpt-4o-mini",
            prompt_tokens=100,
            completion_tokens=20,
        )


def _make_bridge(
    *,
    whisper=None,
    vision=None,
    commands=None,
    task_handler=None,
    gate=None,
    sender=None,
):
    return TelegramBridge(
        owner_chat_ids=frozenset({OWNER_CHAT_ID}),
        send=sender or CapturingSender(),
        whisper=whisper,
        vision=vision,
        personas=default_registry(),
        gate=gate,
        commands=commands,
        task_handler=task_handler,
    )


def _msg(
    *,
    chat_id=OWNER_CHAT_ID,
    user_id=OWNER_CHAT_ID,
    message_id=1,
    text=None,
    voice_bytes=None,
    photo_bytes=None,
):
    return IncomingMessage(
        chat_id=chat_id,
        user_id=user_id,
        message_id=message_id,
        text=text,
        voice_bytes=voice_bytes,
        photo_bytes=photo_bytes,
    )


# ---------------------------------------------------------------------------
# IncomingMessage validation
# ---------------------------------------------------------------------------


def test_incoming_message_text_only():
    m = _msg(text="hi")
    assert m.text == "hi"


def test_incoming_message_voice_only():
    m = _msg(voice_bytes=b"\x00")
    assert m.voice_bytes == b"\x00"


def test_incoming_message_photo_only():
    m = _msg(photo_bytes=b"\x00")
    assert m.photo_bytes == b"\x00"


def test_incoming_message_rejects_empty_all_modalities():
    with pytest.raises(ValueError, match="empty_message_all_modalities"):
        IncomingMessage(chat_id=1, user_id=1, message_id=1)


def test_incoming_message_rejects_only_whitespace_text():
    with pytest.raises(ValueError, match="empty_message_all_modalities"):
        IncomingMessage(chat_id=1, user_id=1, message_id=1, text="   ")


def test_incoming_message_rejects_bool_chat_id():
    with pytest.raises(ValueError, match="invalid_chat_id"):
        IncomingMessage(chat_id=True, user_id=1, message_id=1, text="hi")  # type: ignore[arg-type]


def test_incoming_message_rejects_non_int_user_id():
    with pytest.raises(ValueError, match="invalid_user_id"):
        IncomingMessage(chat_id=1, user_id="x", message_id=1, text="hi")  # type: ignore[arg-type]


def test_incoming_message_rejects_non_string_text():
    with pytest.raises(ValueError, match="non_string_text"):
        IncomingMessage(chat_id=1, user_id=1, message_id=1, text=123)  # type: ignore[arg-type]


def test_incoming_message_rejects_non_bytes_voice():
    with pytest.raises(ValueError, match="non_bytes_voice"):
        IncomingMessage(chat_id=1, user_id=1, message_id=1, voice_bytes="x")  # type: ignore[arg-type]


def test_incoming_message_is_frozen():
    m = _msg(text="hi")
    with pytest.raises(Exception):
        m.text = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# OutgoingMessage validation
# ---------------------------------------------------------------------------


def test_outgoing_message_happy_path():
    o = OutgoingMessage(chat_id=1, text="hi")
    assert o.text == "hi"
    assert o.reply_to_message_id is None


def test_outgoing_message_rejects_empty_text():
    with pytest.raises(ValueError, match="empty_text"):
        OutgoingMessage(chat_id=1, text="  ")


def test_outgoing_message_is_frozen():
    o = OutgoingMessage(chat_id=1, text="hi")
    with pytest.raises(Exception):
        o.chat_id = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# BridgeReply validation
# ---------------------------------------------------------------------------


def test_bridge_reply_happy_path():
    r = BridgeReply(persona_role="architect_agent", body="ответ")
    assert r.persona_role == "architect_agent"
    assert r.body == "ответ"
    assert r.pending_actions == ()


def test_bridge_reply_rejects_empty_body():
    with pytest.raises(ValueError, match="empty_body"):
        BridgeReply(persona_role="architect_agent", body="  ")


def test_bridge_reply_rejects_empty_persona_role():
    with pytest.raises(ValueError, match="empty_persona_role"):
        BridgeReply(persona_role="", body="x")


def test_bridge_reply_rejects_non_tuple_actions():
    with pytest.raises(ValueError, match="pending_actions_must_be_tuple"):
        BridgeReply(persona_role="architect_agent", body="x", pending_actions=[])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Bridge construction
# ---------------------------------------------------------------------------


def test_construction_happy_path():
    b = _make_bridge()
    assert b.manager_persona.human_name == "Менеджер"


def test_construction_rejects_non_frozenset_owners():
    with pytest.raises(ValueError, match="owner_chat_ids_must_be_frozenset"):
        TelegramBridge(
            owner_chat_ids={1},  # type: ignore[arg-type]
            send=CapturingSender(),
        )


def test_construction_rejects_empty_owners():
    with pytest.raises(ValueError, match="empty_owner_chat_ids"):
        TelegramBridge(
            owner_chat_ids=frozenset(),
            send=CapturingSender(),
        )


def test_construction_rejects_bool_in_owners():
    with pytest.raises(ValueError, match="invalid_owner_chat_id"):
        TelegramBridge(
            owner_chat_ids=frozenset({True}),
            send=CapturingSender(),
        )


def test_construction_rejects_non_callable_send():
    with pytest.raises(ValueError, match="send_not_callable"):
        TelegramBridge(
            owner_chat_ids=frozenset({1}),
            send="not callable",  # type: ignore[arg-type]
        )


def test_construction_rejects_non_callable_task_handler():
    with pytest.raises(ValueError, match="task_handler_not_callable"):
        TelegramBridge(
            owner_chat_ids=frozenset({1}),
            send=CapturingSender(),
            task_handler="x",  # type: ignore[arg-type]
        )


def test_construction_rejects_unknown_manager_role():
    with pytest.raises(ValueError, match="unknown_manager_role"):
        TelegramBridge(
            owner_chat_ids=frozenset({1}),
            send=CapturingSender(),
            manager_role="ceo_agent",
        )


# ---------------------------------------------------------------------------
# Whitelist enforcement
# ---------------------------------------------------------------------------


def test_handle_rejects_intruder_chat():
    sender = CapturingSender()
    bridge = _make_bridge(sender=sender)
    msg = _msg(chat_id=INTRUDER_CHAT_ID, user_id=INTRUDER_CHAT_ID, text="hi")
    result = bridge.handle(msg)
    assert result.handled is False
    assert result.reason == "not_owner"
    assert len(sender.sent) == 1
    assert DEFAULT_DENIAL_MESSAGE in sender.sent[0].text


def test_handle_accepts_owner_via_chat_id():
    sender = CapturingSender()
    bridge = _make_bridge(
        sender=sender,
        task_handler=lambda t, m: BridgeReply(persona_role="architect_agent", body=t),
    )
    msg = _msg(chat_id=OWNER_CHAT_ID, user_id=99, text="hello")
    result = bridge.handle(msg)
    assert result.handled is True


def test_handle_accepts_owner_via_user_id():
    sender = CapturingSender()
    bridge = _make_bridge(
        sender=sender,
        task_handler=lambda t, m: BridgeReply(persona_role="architect_agent", body=t),
    )
    msg = _msg(chat_id=99, user_id=OWNER_CHAT_ID, text="hello")
    result = bridge.handle(msg)
    assert result.handled is True


# ---------------------------------------------------------------------------
# Text resolution: text > voice > photo
# ---------------------------------------------------------------------------


def test_resolve_text_takes_precedence_over_voice():
    sender = CapturingSender()
    captured_text = []
    bridge = _make_bridge(
        sender=sender,
        whisper=FakeWhisper(),
        task_handler=lambda t, m: (captured_text.append(t), BridgeReply(
            persona_role="architect_agent", body="ok"))[1],
    )
    msg = _msg(text="real text", voice_bytes=b"\x00\x01")
    bridge.handle(msg)
    assert captured_text == ["real text"]


def test_voice_transcribed_when_no_text():
    sender = CapturingSender()
    whisper = FakeWhisper(result_text="голос → текст")
    captured = []
    bridge = _make_bridge(
        sender=sender,
        whisper=whisper,
        task_handler=lambda t, m: (captured.append(t), BridgeReply(
            persona_role="architect_agent", body="ok"))[1],
    )
    msg = _msg(voice_bytes=b"audio_data")
    result = bridge.handle(msg)
    assert result.handled is True
    assert captured == ["голос → текст"]
    assert len(whisper.calls) == 1
    assert whisper.calls[0]["mime_type"] == "audio/ogg"
    assert whisper.calls[0]["language"] == "ru"


def test_voice_without_whisper_apologies():
    sender = CapturingSender()
    bridge = _make_bridge(sender=sender, whisper=None)
    msg = _msg(voice_bytes=b"audio")
    result = bridge.handle(msg)
    assert result.handled is False
    assert result.reason == "no_text_resolved"
    assert len(sender.sent) == 1
    assert "голосовые" in sender.sent[0].text.lower()


def test_voice_failure_apologies_with_persona():
    sender = CapturingSender()
    bridge = _make_bridge(
        sender=sender,
        whisper=FakeWhisper(raise_exc=WhisperError("timeout", "30s")),
    )
    msg = _msg(voice_bytes=b"audio")
    result = bridge.handle(msg)
    assert result.handled is False
    assert len(sender.sent) == 1
    assert "Менеджер:" in sender.sent[0].text
    assert "расшифровать" in sender.sent[0].text.lower()


def test_photo_described_when_no_text_no_voice():
    sender = CapturingSender()
    vision = FakeVision(result_text="скрин ошибки TypeError")
    captured = []
    bridge = _make_bridge(
        sender=sender,
        vision=vision,
        task_handler=lambda t, m: (captured.append(t), BridgeReply(
            persona_role="architect_agent", body="ok"))[1],
    )
    msg = _msg(photo_bytes=b"\x89PNG")
    result = bridge.handle(msg)
    assert result.handled is True
    assert captured == ["скрин ошибки TypeError"]
    assert len(vision.calls) == 1


def test_photo_without_vision_apologies():
    sender = CapturingSender()
    bridge = _make_bridge(sender=sender, vision=None)
    msg = _msg(photo_bytes=b"png")
    result = bridge.handle(msg)
    assert result.handled is False
    assert "Изображения" in sender.sent[0].text


def test_photo_failure_apologies():
    sender = CapturingSender()
    bridge = _make_bridge(
        sender=sender,
        vision=FakeVision(raise_exc=VisionError("rate_limited")),
    )
    msg = _msg(photo_bytes=b"png")
    result = bridge.handle(msg)
    assert result.handled is False
    assert "распознать" in sender.sent[0].text.lower()


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


def test_command_dispatched_via_registry():
    sender = CapturingSender()
    reg = CommandRegistry()
    reg.register(CommandName.HELP, lambda c, ctx: "Список команд: ...")
    bridge = _make_bridge(sender=sender, commands=reg)
    msg = _msg(text="/help")
    result = bridge.handle(msg)
    assert result.handled is True
    assert result.reason == "command"
    assert "Список команд" in sender.sent[0].text
    # signed by manager
    assert sender.sent[0].text.startswith("Менеджер:")


def test_command_without_registry_apologies():
    sender = CapturingSender()
    bridge = _make_bridge(sender=sender, commands=None)
    msg = _msg(text="/help")
    result = bridge.handle(msg)
    assert result.handled is True
    assert "не зарегистрированы" in sender.sent[0].text


def test_command_without_registered_handler_apologies():
    sender = CapturingSender()
    reg = CommandRegistry()  # empty
    bridge = _make_bridge(sender=sender, commands=reg)
    msg = _msg(text="/help")
    bridge.handle(msg)
    assert "не имеет хендлера" in sender.sent[0].text


def test_command_handler_exception_apologies():
    sender = CapturingSender()
    reg = CommandRegistry()

    def boom(c, ctx):
        raise RuntimeError("kaboom")

    reg.register(CommandName.HELP, boom)
    bridge = _make_bridge(sender=sender, commands=reg)
    bridge.handle(_msg(text="/help"))
    assert "Ошибка" in sender.sent[0].text
    assert "kaboom" in sender.sent[0].text


# ---------------------------------------------------------------------------
# Free-text task delegation
# ---------------------------------------------------------------------------


def test_task_handler_invoked_with_text_and_msg():
    sender = CapturingSender()
    captured = []

    def handler(text, msg):
        captured.append((text, msg.message_id))
        return BridgeReply(persona_role="architect_agent", body="готов")

    bridge = _make_bridge(sender=sender, task_handler=handler)
    bridge.handle(_msg(text="сделай X", message_id=42))
    assert captured == [("сделай X", 42)]


def test_task_reply_signed_by_persona():
    sender = CapturingSender()
    bridge = _make_bridge(
        sender=sender,
        task_handler=lambda t, m: BridgeReply(
            persona_role="architect_agent", body="предлагаю стек"
        ),
    )
    bridge.handle(_msg(text="новый сервис"))
    assert sender.sent[0].text == "Архитектор: предлагаю стек"


def test_task_handler_returning_none_sends_generic_ack():
    sender = CapturingSender()
    bridge = _make_bridge(
        sender=sender,
        task_handler=lambda t, m: None,
    )
    bridge.handle(_msg(text="сделай"))
    assert DEFAULT_GENERIC_ACK in sender.sent[0].text
    assert sender.sent[0].text.startswith("Менеджер:")


def test_task_handler_returning_invalid_type_apologies():
    sender = CapturingSender()
    bridge = _make_bridge(
        sender=sender,
        task_handler=lambda t, m: "not a BridgeReply",  # type: ignore[return-value]
    )
    bridge.handle(_msg(text="сделай"))
    assert "некорректный формат" in sender.sent[0].text


def test_task_handler_exception_apologies():
    sender = CapturingSender()

    def boom(t, m):
        raise ValueError("invalid stuff")

    bridge = _make_bridge(sender=sender, task_handler=boom)
    bridge.handle(_msg(text="сделай"))
    assert "Не удалось обработать" in sender.sent[0].text
    assert "invalid stuff" in sender.sent[0].text


def test_task_unknown_persona_role_falls_back_to_manager():
    sender = CapturingSender()
    bridge = _make_bridge(
        sender=sender,
        task_handler=lambda t, m: BridgeReply(persona_role="ghost_agent", body="x"),
    )
    bridge.handle(_msg(text="сделай"))
    # persona_role is rejected at BridgeReply construction... wait, no, the
    # constructor only checks non-empty. Unknown roles are caught at signing
    # time and fall back to manager with marker.
    assert "[неизвестная роль" in sender.sent[0].text
    assert sender.sent[0].text.startswith("Менеджер:")


def test_task_without_handler_apologies():
    sender = CapturingSender()
    bridge = _make_bridge(sender=sender, task_handler=None)
    bridge.handle(_msg(text="сделай"))
    assert "task_handler" in sender.sent[0].text


# ---------------------------------------------------------------------------
# Confirmation gate integration
# ---------------------------------------------------------------------------


def test_pending_actions_trigger_ask_when_gate_says_so():
    sender = CapturingSender()
    gate = ConfirmationGate()
    actions = (
        ActionDescriptor(kind=ActionKind.DELETE, target_path="config.yaml"),
    )

    def handler(t, m):
        return BridgeReply(
            persona_role="architect_agent",
            body="Я предлагаю удалить config.yaml",
            pending_actions=actions,
        )

    bridge = _make_bridge(sender=sender, gate=gate, task_handler=handler)
    bridge.handle(_msg(text="почисти"))
    assert len(sender.sent) == 1
    sent_text = sender.sent[0].text
    assert "Архитектор:" in sent_text
    assert "Требуется ваше подтверждение" in sent_text
    assert "config.yaml" in sent_text
    assert "удаление" in sent_text


def test_safe_actions_dont_trigger_ask():
    sender = CapturingSender()
    gate = ConfirmationGate()
    actions = (
        ActionDescriptor(kind=ActionKind.MODIFY, target_path="docs/guide.md"),
    )

    def handler(t, m):
        return BridgeReply(
            persona_role="architect_agent",
            body="готово",
            pending_actions=actions,
        )

    bridge = _make_bridge(sender=sender, gate=gate, task_handler=handler)
    bridge.handle(_msg(text="обнови гайд"))
    assert sender.sent[0].text == "Архитектор: готово"
    assert "подтверждение" not in sender.sent[0].text


def test_pending_actions_without_gate_pass_through():
    """If gate is None, pending_actions are ignored — reply sent as-is."""
    sender = CapturingSender()
    actions = (
        ActionDescriptor(kind=ActionKind.DELETE, target_path="x.py"),
    )

    def handler(t, m):
        return BridgeReply(
            persona_role="architect_agent",
            body="готово",
            pending_actions=actions,
        )

    bridge = _make_bridge(sender=sender, gate=None, task_handler=handler)
    bridge.handle(_msg(text="очисти"))
    assert sender.sent[0].text == "Архитектор: готово"


# ---------------------------------------------------------------------------
# Robustness: send-failure and exceptional inputs
# ---------------------------------------------------------------------------


def test_send_exception_does_not_crash_handle():
    """Bridge must not propagate transport errors back to the runner."""
    sender = CapturingSender(raise_exc=ConnectionError("network down"))
    bridge = _make_bridge(
        sender=sender,
        task_handler=lambda t, m: BridgeReply(persona_role="architect_agent", body="x"),
    )
    result = bridge.handle(_msg(text="hi"))
    # handled is True because we entered task path — actual delivery failed
    # silently; sent_count stays at 0 because send raised.
    assert result.sent_count == 0


def test_handle_non_incoming_message_returns_invalid_type():
    sender = CapturingSender()
    bridge = _make_bridge(sender=sender)
    result = bridge.handle("not a message")  # type: ignore[arg-type]
    assert result.handled is False
    assert result.reason == "invalid_message_type"


def test_handle_returns_bridge_result():
    sender = CapturingSender()
    bridge = _make_bridge(
        sender=sender,
        task_handler=lambda t, m: BridgeReply(persona_role="architect_agent", body="x"),
    )
    result = bridge.handle(_msg(text="hi"))
    assert isinstance(result, BridgeResult)
    assert result.chat_id == OWNER_CHAT_ID
    assert result.extracted_text == "hi"


def test_bridge_result_is_frozen():
    r = BridgeResult(chat_id=1, handled=True, reason="ok", sent_count=1)
    with pytest.raises(Exception):
        r.handled = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Manager persona signature on system replies
# ---------------------------------------------------------------------------


def test_command_reply_signed_by_manager():
    sender = CapturingSender()
    reg = CommandRegistry()
    reg.register(CommandName.HELP, lambda c, ctx: "/help: список")
    bridge = _make_bridge(sender=sender, commands=reg)
    bridge.handle(_msg(text="/help"))
    assert sender.sent[0].text.startswith("Менеджер:")


def test_denial_message_not_signed():
    """Denial is sent verbatim — no signature, since the user isn't owner
    and shouldn't see internal personas."""
    sender = CapturingSender()
    bridge = _make_bridge(sender=sender)
    bridge.handle(_msg(chat_id=INTRUDER_CHAT_ID, user_id=INTRUDER_CHAT_ID, text="hi"))
    assert sender.sent[0].text == DEFAULT_DENIAL_MESSAGE
