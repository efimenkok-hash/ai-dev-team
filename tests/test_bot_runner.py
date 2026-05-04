"""Tests for core.bot_runner (Step 14a Module 7: builders + default handlers)."""

import pytest

from core.agent_personas import default_registry
from core.bot_commands import (
    CommandName,
    parse_command,
)
from core.bot_runner import (
    _BudgetState,
    build_bridge_from_env,
    build_command_registry,
    build_confirmation_gate,
    build_vision_client,
    build_whisper_client,
    get_required_env,
    make_agents_handler,
    make_budget_handler,
    make_help_handler,
    make_log_handler,
    make_projects_handler,
    make_retry_handler,
    make_simple_task_handler,
    make_stop_handler,
    make_switch_handler,
    parse_owner_chat_ids,
)
from core.confirmation_gate import ConfirmationGate
from core.telegram_bridge import (
    BridgeReply,
    IncomingMessage,
    TelegramBridge,
)
from core.vision_client import VisionClient
from core.whisper_client import WhisperClient

# ---------------------------------------------------------------------------
# parse_owner_chat_ids
# ---------------------------------------------------------------------------


def test_parse_single_owner_id():
    assert parse_owner_chat_ids("12345") == frozenset({12345})


def test_parse_multiple_owner_ids():
    assert parse_owner_chat_ids("1, 2, 3") == frozenset({1, 2, 3})


def test_parse_strips_whitespace():
    assert parse_owner_chat_ids("  100  ") == frozenset({100})


def test_parse_dedupes():
    assert parse_owner_chat_ids("5,5,5") == frozenset({5})


def test_parse_rejects_non_string():
    with pytest.raises(ValueError, match="owner_chat_id_must_be_string"):
        parse_owner_chat_ids(123)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["", "  ", ",", " , , "])
def test_parse_rejects_empty(bad):
    with pytest.raises(ValueError, match="empty_owner_chat_id"):
        parse_owner_chat_ids(bad)


def test_parse_rejects_non_int():
    with pytest.raises(ValueError, match="invalid_owner_chat_id"):
        parse_owner_chat_ids("abc")


def test_parse_rejects_zero():
    with pytest.raises(ValueError, match="non_positive_owner_chat_id"):
        parse_owner_chat_ids("0")


def test_parse_rejects_negative():
    with pytest.raises(ValueError, match="non_positive_owner_chat_id"):
        parse_owner_chat_ids("-1")


# ---------------------------------------------------------------------------
# get_required_env
# ---------------------------------------------------------------------------


def test_get_required_env_returns_value():
    assert get_required_env({"X": "value"}, "X") == "value"


def test_get_required_env_strips():
    assert get_required_env({"X": "  value  "}, "X") == "value"


def test_get_required_env_missing_raises():
    with pytest.raises(ValueError, match="missing_env"):
        get_required_env({}, "X")


def test_get_required_env_empty_raises():
    with pytest.raises(ValueError, match="missing_env"):
        get_required_env({"X": "  "}, "X")


def test_get_required_env_rejects_non_mapping():
    with pytest.raises(ValueError, match="env_must_be_mapping"):
        get_required_env(["x"], "X")  # type: ignore[arg-type]


def test_get_required_env_rejects_empty_key():
    with pytest.raises(ValueError, match="empty_env_key"):
        get_required_env({"X": "v"}, "")


# ---------------------------------------------------------------------------
# build_whisper_client / build_vision_client (optional clients)
# ---------------------------------------------------------------------------


def test_build_whisper_client_with_key():
    c = build_whisper_client({"OPENAI_API_KEY": "sk-test"})
    assert isinstance(c, WhisperClient)


def test_build_whisper_client_without_key_returns_none():
    assert build_whisper_client({}) is None


def test_build_whisper_client_empty_key_returns_none():
    assert build_whisper_client({"OPENAI_API_KEY": "  "}) is None


def test_build_vision_client_with_key():
    c = build_vision_client({"OPENROUTER_API_KEY": "sk-or-test"})
    assert isinstance(c, VisionClient)


def test_build_vision_client_without_key_returns_none():
    assert build_vision_client({}) is None


# ---------------------------------------------------------------------------
# build_confirmation_gate
# ---------------------------------------------------------------------------


def test_build_confirmation_gate_default():
    g = build_confirmation_gate({})
    assert isinstance(g, ConfirmationGate)
    assert g.cost_threshold_usd == 1.0


def test_build_confirmation_gate_custom_threshold():
    g = build_confirmation_gate({"BOT_COST_THRESHOLD_USD": "5.5"})
    assert g.cost_threshold_usd == 5.5


def test_build_confirmation_gate_invalid_threshold():
    with pytest.raises(ValueError, match="invalid_BOT_COST_THRESHOLD_USD"):
        build_confirmation_gate({"BOT_COST_THRESHOLD_USD": "abc"})


# ---------------------------------------------------------------------------
# command handlers
# ---------------------------------------------------------------------------


def test_help_handler_lists_commands():
    handler = make_help_handler((CommandName.HELP, CommandName.STOP))
    text = handler(parse_command("/help"), None)
    assert "/help" in text
    assert "/stop" in text


def test_help_handler_rejects_non_tuple():
    with pytest.raises(ValueError, match="registered_must_be_tuple"):
        make_help_handler([CommandName.HELP])  # type: ignore[arg-type]


def test_projects_handler_returns_active_project():
    handler = make_projects_handler(active_project="hedgekeeper")
    text = handler(parse_command("/projects"), None)
    assert "hedgekeeper" in text
    assert "Активный проект" in text


def test_projects_handler_rejects_empty_project():
    with pytest.raises(ValueError, match="empty_active_project"):
        make_projects_handler(active_project="")


def test_switch_handler_no_args():
    handler = make_switch_handler()
    text = handler(parse_command("/switch"), None)
    assert "<имя_проекта>" in text


def test_switch_handler_with_arg():
    handler = make_switch_handler()
    text = handler(parse_command("/switch hedgekeeper"), None)
    assert "hedgekeeper" in text
    assert "7b" in text


def test_budget_handler_show_default():
    state = _BudgetState(initial_usd=10.0)
    handler = make_budget_handler(state)
    text = handler(parse_command("/budget"), None)
    assert "$10.00" in text


def test_budget_handler_set_amount():
    state = _BudgetState(initial_usd=10.0)
    handler = make_budget_handler(state)
    text = handler(parse_command("/budget 25.5"), None)
    assert "$25.50" in text
    assert state.budget_usd == 25.5


def test_budget_handler_invalid_amount():
    state = _BudgetState(initial_usd=10.0)
    handler = make_budget_handler(state)
    text = handler(parse_command("/budget abc"), None)
    assert "Не удалось разобрать" in text
    assert state.budget_usd == 10.0  # unchanged


def test_budget_handler_rejects_non_state():
    with pytest.raises(ValueError, match="invalid_budget_state"):
        make_budget_handler("not a state")  # type: ignore[arg-type]


def test_agents_handler_lists_all_eight():
    personas = default_registry()
    handler = make_agents_handler(personas)
    text = handler(parse_command("/agents"), None)
    for p in personas.all():
        assert p.callsign in text
    assert "Состав команды" in text


def test_agents_handler_rejects_non_personas():
    with pytest.raises(ValueError, match="invalid_personas"):
        make_agents_handler("not personas")  # type: ignore[arg-type]


def test_log_handler_returns_string():
    handler = make_log_handler()
    text = handler(parse_command("/log"), None)
    assert isinstance(text, str)
    assert "лог" in text.lower()


def test_stop_handler_returns_string():
    handler = make_stop_handler()
    text = handler(parse_command("/stop"), None)
    assert isinstance(text, str)
    assert "/stop" in text or "Остановка" in text


def test_retry_handler_default():
    handler = make_retry_handler()
    text = handler(parse_command("/retry"), None)
    assert "Повтор" in text


def test_retry_handler_with_different_flag():
    handler = make_retry_handler()
    text = handler(parse_command("/retry --different"), None)
    assert "другой моделью" in text or "стратегией" in text


# ---------------------------------------------------------------------------
# build_command_registry
# ---------------------------------------------------------------------------


def test_build_command_registry_has_all_eight():
    personas = default_registry()
    reg = build_command_registry(personas)
    assert len(reg) == 8
    for cmd_name in CommandName:
        assert cmd_name in reg


def test_build_command_registry_help_lists_all():
    personas = default_registry()
    reg = build_command_registry(personas)
    text = reg.dispatch(parse_command("/help"))
    for cmd_name in CommandName:
        assert f"/{cmd_name.value}" in text


def test_build_command_registry_rejects_non_personas():
    with pytest.raises(ValueError, match="invalid_personas"):
        build_command_registry("not personas")  # type: ignore[arg-type]


def test_build_command_registry_rejects_negative_budget():
    personas = default_registry()
    with pytest.raises(ValueError, match="invalid_initial_budget"):
        build_command_registry(personas, initial_budget_usd=-1.0)


def test_build_command_registry_dispatches_each_command():
    personas = default_registry()
    reg = build_command_registry(personas)
    for cmd_name in CommandName:
        result = reg.dispatch(parse_command(f"/{cmd_name.value}"))
        assert isinstance(result, str)
        assert result.strip()


# ---------------------------------------------------------------------------
# make_simple_task_handler
# ---------------------------------------------------------------------------


def test_simple_task_handler_returns_bridge_reply():
    personas = default_registry()
    handler = make_simple_task_handler(personas)
    msg = IncomingMessage(chat_id=1, user_id=1, message_id=1, text="hello")
    reply = handler("hello", msg)
    assert isinstance(reply, BridgeReply)
    assert reply.persona_role == "pm_agent"
    assert "hello" in reply.body


def test_simple_task_handler_truncates_long_text():
    personas = default_registry()
    handler = make_simple_task_handler(personas)
    long_text = "x" * 500
    msg = IncomingMessage(chat_id=1, user_id=1, message_id=1, text=long_text)
    reply = handler(long_text, msg)
    assert "обрезано" in reply.body


# ---------------------------------------------------------------------------
# build_bridge_from_env (top-level integration)
# ---------------------------------------------------------------------------


def _captured_send():
    captured = []

    def _send(out):
        captured.append(out)

    return _send, captured


def test_build_bridge_from_env_minimal():
    env = {"TELEGRAM_OWNER_CHAT_ID": "12345"}
    send, _ = _captured_send()
    bridge = build_bridge_from_env(env, send_callable=send)
    assert isinstance(bridge, TelegramBridge)


def test_build_bridge_from_env_with_all_keys():
    env = {
        "TELEGRAM_OWNER_CHAT_ID": "12345",
        "OPENAI_API_KEY": "sk-test",
        "OPENROUTER_API_KEY": "sk-or-test",
        "BOT_COST_THRESHOLD_USD": "2.5",
    }
    send, _ = _captured_send()
    bridge = build_bridge_from_env(env, send_callable=send)
    assert isinstance(bridge, TelegramBridge)


def test_build_bridge_from_env_missing_owner_id():
    send, _ = _captured_send()
    with pytest.raises(ValueError, match="missing_env:TELEGRAM_OWNER_CHAT_ID"):
        build_bridge_from_env({}, send_callable=send)


def test_build_bridge_from_env_requires_send_callable():
    env = {"TELEGRAM_OWNER_CHAT_ID": "12345"}
    with pytest.raises(ValueError, match="send_callable_required"):
        build_bridge_from_env(env, send_callable=None)


def test_build_bridge_from_env_requires_callable_send():
    env = {"TELEGRAM_OWNER_CHAT_ID": "12345"}
    with pytest.raises(ValueError, match="send_callable_required"):
        build_bridge_from_env(env, send_callable="not callable")


def test_build_bridge_from_env_end_to_end_flow():
    """Smoke: a full message flow through the assembled bridge."""
    env = {"TELEGRAM_OWNER_CHAT_ID": "777"}
    send, captured = _captured_send()
    bridge = build_bridge_from_env(env, send_callable=send)
    msg = IncomingMessage(chat_id=777, user_id=777, message_id=1, text="привет")
    bridge.handle(msg)
    assert len(captured) == 1
    assert captured[0].text.startswith("Менеджер:")
    assert "привет" in captured[0].text


def test_build_bridge_from_env_command_flow():
    env = {"TELEGRAM_OWNER_CHAT_ID": "777"}
    send, captured = _captured_send()
    bridge = build_bridge_from_env(env, send_callable=send)
    msg = IncomingMessage(chat_id=777, user_id=777, message_id=1, text="/help")
    bridge.handle(msg)
    assert len(captured) == 1
    assert "/help" in captured[0].text
    assert captured[0].text.startswith("Менеджер:")


def test_build_bridge_from_env_intruder_denied():
    env = {"TELEGRAM_OWNER_CHAT_ID": "777"}
    send, captured = _captured_send()
    bridge = build_bridge_from_env(env, send_callable=send)
    msg = IncomingMessage(chat_id=999, user_id=999, message_id=1, text="привет")
    bridge.handle(msg)
    assert len(captured) == 1
    assert "Доступ запрещён" in captured[0].text
