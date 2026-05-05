"""Tests for core.bot_runner (Step 14a Module 7 + 14b-6/7: builders + handlers)."""

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
    build_dispatcher_from_env,
    build_real_task_handler_from_env,
    build_vision_client,
    build_whisper_client,
    get_required_env,
    make_agents_handler,
    make_budget_handler,
    make_help_handler,
    make_log_handler,
    make_projects_handler,
    make_push_handler,
    make_retry_handler,
    make_simple_task_handler,
    make_stop_handler,
    make_switch_handler,
    make_tier_handler,
    parse_owner_chat_ids,
)
from core.confirmation_gate import ConfirmationGate
from core.model_tier import default_registry as default_tier_registry
from core.task_history import TaskHistory
from core.telegram_bridge import (
    BridgeReply,
    IncomingMessage,
    TelegramBridge,
)
from core.tier_session import TierSessionStore
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
# build_dispatcher_from_env
# ---------------------------------------------------------------------------


def test_build_dispatcher_from_env_with_key():
    from core.llm_dispatcher import LLMDispatcher
    d = build_dispatcher_from_env({"OPENROUTER_API_KEY": "sk-or-test"})
    assert isinstance(d, LLMDispatcher)


def test_build_dispatcher_from_env_without_key_returns_none():
    assert build_dispatcher_from_env({}) is None


def test_build_dispatcher_from_env_empty_key_returns_none():
    assert build_dispatcher_from_env({"OPENROUTER_API_KEY": "   "}) is None


def test_build_dispatcher_from_env_rejects_non_mapping():
    with pytest.raises(ValueError, match="env_must_be_mapping"):
        build_dispatcher_from_env("not a mapping")  # type: ignore[arg-type]


def test_build_dispatcher_from_env_strips_whitespace():
    from core.llm_dispatcher import LLMDispatcher
    d = build_dispatcher_from_env({"OPENROUTER_API_KEY": "  sk-or-padded  "})
    assert isinstance(d, LLMDispatcher)


# ---------------------------------------------------------------------------
# build_real_task_handler_from_env
# ---------------------------------------------------------------------------


def _noop_progress(chat_id: int, text: str) -> None:
    pass


def test_build_real_task_handler_no_api_key_returns_none(tmp_path):
    store = TierSessionStore(default_tier_registry())
    result = build_real_task_handler_from_env(
        {"REPO_PATH": str(tmp_path)},
        tier_store=store,
        send_progress=_noop_progress,
    )
    assert result is None


def test_build_real_task_handler_no_repo_path_returns_none():
    store = TierSessionStore(default_tier_registry())
    result = build_real_task_handler_from_env(
        {"OPENROUTER_API_KEY": "sk-or-test"},
        tier_store=store,
        send_progress=_noop_progress,
    )
    assert result is None


def test_build_real_task_handler_invalid_repo_path_returns_none(tmp_path):
    store = TierSessionStore(default_tier_registry())
    result = build_real_task_handler_from_env(
        {
            "OPENROUTER_API_KEY": "sk-or-test",
            "REPO_PATH": str(tmp_path / "nonexistent"),
        },
        tier_store=store,
        send_progress=_noop_progress,
    )
    assert result is None


def test_build_real_task_handler_path_without_git_returns_none(tmp_path):
    # Directory exists but no .git
    repo = tmp_path / "repo"
    repo.mkdir()
    store = TierSessionStore(default_tier_registry())
    result = build_real_task_handler_from_env(
        {"OPENROUTER_API_KEY": "sk-or-test", "REPO_PATH": str(repo)},
        tier_store=store,
        send_progress=_noop_progress,
    )
    assert result is None


def test_build_real_task_handler_full_env_returns_callable(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    store = TierSessionStore(default_tier_registry())
    result = build_real_task_handler_from_env(
        {"OPENROUTER_API_KEY": "sk-or-test", "REPO_PATH": str(repo)},
        tier_store=store,
        send_progress=_noop_progress,
    )
    assert callable(result)


def test_build_real_task_handler_custom_worktree_root(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    worktree = tmp_path / "wt"
    store = TierSessionStore(default_tier_registry())
    result = build_real_task_handler_from_env(
        {
            "OPENROUTER_API_KEY": "sk-or-test",
            "REPO_PATH": str(repo),
            "WORKTREE_ROOT": str(worktree),
        },
        tier_store=store,
        send_progress=_noop_progress,
    )
    assert callable(result)


def test_build_real_task_handler_rejects_non_mapping():
    store = TierSessionStore(default_tier_registry())
    with pytest.raises(ValueError, match="env_must_be_mapping"):
        build_real_task_handler_from_env(
            "bad",  # type: ignore[arg-type]
            tier_store=store,
            send_progress=_noop_progress,
        )


def test_build_real_task_handler_rejects_invalid_tier_store():
    with pytest.raises(ValueError, match="invalid_tier_store"):
        build_real_task_handler_from_env(
            {},
            tier_store="not a store",  # type: ignore[arg-type]
            send_progress=_noop_progress,
        )


def test_build_real_task_handler_rejects_non_callable_progress():
    store = TierSessionStore(default_tier_registry())
    with pytest.raises(ValueError, match="send_progress_not_callable"):
        build_real_task_handler_from_env(
            {},
            tier_store=store,
            send_progress="not callable",  # type: ignore[arg-type]
        )


def test_build_real_task_handler_shuts_down_runner_on_factory_failure(tmp_path):
    """Resource-leak fix: if factory/make_real_task_handler raises after
    BackgroundTaskRunner is created, runner.shutdown(wait=False) must be called
    so the thread-pool worker does not linger.
    """
    from unittest.mock import MagicMock, patch

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    store = TierSessionStore(default_tier_registry())

    mock_runner = MagicMock()

    with (
        patch("core.bot_runner.BackgroundTaskRunner", return_value=mock_runner),
        patch(
            "core.bot_runner.build_dispatcher_agent_registry_factory",
            side_effect=ValueError("factory_boom"),
        ),
    ):
        result = build_real_task_handler_from_env(
            {"OPENROUTER_API_KEY": "sk-or-test", "REPO_PATH": str(repo)},
            tier_store=store,
            send_progress=_noop_progress,
        )

    assert result is None
    mock_runner.shutdown.assert_called_once_with(wait=False)


def test_build_real_task_handler_does_not_shutdown_external_runner_on_failure(tmp_path):
    """When caller passes an external runner and factory fails, the caller
    owns the runner — we must NOT shut it down inside the function."""
    from unittest.mock import MagicMock, patch

    from core.background_runner import BackgroundTaskRunner

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    store = TierSessionStore(default_tier_registry())

    external_runner = MagicMock(spec=BackgroundTaskRunner)

    with patch(
        "core.bot_runner.build_dispatcher_agent_registry_factory",
        side_effect=ValueError("factory_boom"),
    ):
        result = build_real_task_handler_from_env(
            {"OPENROUTER_API_KEY": "sk-or-test", "REPO_PATH": str(repo)},
            tier_store=store,
            send_progress=_noop_progress,
            runner=external_runner,
        )

    assert result is None
    external_runner.shutdown.assert_not_called()


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
    assert "Активный" in text


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


def test_agents_handler_uses_qualified_name_no_redundancy():
    """Default personas have callsign == title; output must NOT show
    'Архитектор (Архитектор)' redundancy."""
    personas = default_registry()
    handler = make_agents_handler(personas)
    text = handler(parse_command("/agents"), None)
    assert "Архитектор (Архитектор)" not in text
    assert "Программист (Программист)" not in text


def test_agents_handler_uses_emojis():
    """Each agent line should be prefixed by its persona emoji."""
    personas = default_registry()
    handler = make_agents_handler(personas)
    text = handler(parse_command("/agents"), None)
    for p in personas.all():
        if p.emoji:
            assert p.emoji in text


def test_agents_handler_orders_by_pipeline_flow():
    """Agents listed in FSM execution order, not alphabetically."""
    personas = default_registry()
    handler = make_agents_handler(personas)
    text = handler(parse_command("/agents"), None)
    # Planner must appear before Architect, which must appear before Fixer
    assert text.index("Планировщик") < text.index("Архитектор")
    assert text.index("Архитектор") < text.index("Программист")
    assert text.index("Программист") < text.index("Ревьюер")
    assert text.index("Ревьюер") < text.index("Фиксер")


def test_agents_handler_rejects_non_personas():
    with pytest.raises(ValueError, match="invalid_personas"):
        make_agents_handler("not personas")  # type: ignore[arg-type]


def test_log_handler_returns_string():
    handler = make_log_handler()
    text = handler(parse_command("/log"), None)
    assert isinstance(text, str)
    assert "лог" in text.lower()


def test_stop_handler_no_runner_returns_stub():
    """Without a runner /stop should return an informative stub, not crash."""
    handler = make_stop_handler()
    text = handler(parse_command("/stop"), None)
    assert isinstance(text, str)
    assert "⏹" in text
    # Must NOT claim "остановлена" or "ничего" — those belong to real handler.
    assert "недоступна" in text.lower() or "не настроен" in text.lower()


def test_stop_handler_rejects_invalid_runner():
    with pytest.raises(ValueError, match="invalid_runner"):
        make_stop_handler(runner="not a runner")  # type: ignore[arg-type]


def test_stop_handler_cancels_active_task():
    """runner.cancel() returns True (active task) → "остановлена" message."""
    from unittest.mock import MagicMock

    from core.background_runner import BackgroundTaskRunner

    mock_runner = MagicMock(spec=BackgroundTaskRunner)
    mock_runner.cancel.return_value = True

    handler = make_stop_handler(runner=mock_runner)
    text = handler(parse_command("/stop"), None)

    mock_runner.cancel.assert_called_once()
    assert "⏹" in text
    assert "остановлена" in text.lower()


def test_stop_handler_nothing_running():
    """runner.cancel() returns False (idle) → "ничего не выполняется" message."""
    from unittest.mock import MagicMock

    from core.background_runner import BackgroundTaskRunner

    mock_runner = MagicMock(spec=BackgroundTaskRunner)
    mock_runner.cancel.return_value = False

    handler = make_stop_handler(runner=mock_runner)
    text = handler(parse_command("/stop"), None)

    mock_runner.cancel.assert_called_once()
    assert "ничего" in text.lower()


def test_stop_handler_with_real_runner_idle():
    """Integration: real BackgroundTaskRunner (idle) → cancel() == False."""
    from core.background_runner import BackgroundTaskRunner

    runner = BackgroundTaskRunner()
    try:
        handler = make_stop_handler(runner=runner)
        text = handler(parse_command("/stop"), None)
        assert "ничего" in text.lower()
    finally:
        runner.shutdown(wait=False)


def test_retry_handler_default():
    handler = make_retry_handler()
    text = handler(parse_command("/retry"), None)
    assert "Повтор" in text


def test_retry_handler_with_different_flag():
    handler = make_retry_handler()
    text = handler(parse_command("/retry --different"), None)
    assert "другой моделью" in text or "стратегией" in text


# ---------------------------------------------------------------------------
# make_tier_handler
# ---------------------------------------------------------------------------


def _msg(chat_id: int = 100) -> IncomingMessage:
    return IncomingMessage(
        chat_id=chat_id,
        user_id=chat_id,
        message_id=1,
        text="/tier",
    )


def test_tier_handler_rejects_non_store():
    with pytest.raises(ValueError, match="invalid_tier_store"):
        make_tier_handler("not a store")  # type: ignore[arg-type]


def test_tier_handler_no_args_shows_summary():
    store = TierSessionStore(default_tier_registry())
    handler = make_tier_handler(store)
    text = handler(parse_command("/tier"), _msg())
    # Default registry has STANDARD active globally; chat hasn't picked yet.
    assert "ECONOMY" in text
    assert "STANDARD" in text
    assert "PREMIUM" in text
    assert "/tier set" in text


def test_tier_handler_set_records_choice():
    store = TierSessionStore(default_tier_registry())
    handler = make_tier_handler(store)
    text = handler(parse_command("/tier set PREMIUM"), _msg(chat_id=42))
    assert "PREMIUM" in text
    assert store.active_tier_name(42) == "PREMIUM"


def test_tier_handler_set_unknown_tier():
    store = TierSessionStore(default_tier_registry())
    handler = make_tier_handler(store)
    text = handler(parse_command("/tier set BOGUS"), _msg(chat_id=42))
    assert "Неизвестный тариф" in text
    assert store.active_tier_name(42) is None  # nothing recorded


def test_tier_handler_set_without_name():
    store = TierSessionStore(default_tier_registry())
    handler = make_tier_handler(store)
    text = handler(parse_command("/tier set"), _msg())
    assert "Использование" in text
    assert "<имя_тарифа>" in text


def test_tier_handler_reset_clears_choice():
    store = TierSessionStore(default_tier_registry())
    store.set_active(42, "PREMIUM")
    handler = make_tier_handler(store)
    text = handler(parse_command("/tier reset"), _msg(chat_id=42))
    assert "сброшен" in text.lower()
    assert store.active_tier_name(42) is None


def test_tier_handler_unknown_subcommand():
    store = TierSessionStore(default_tier_registry())
    handler = make_tier_handler(store)
    text = handler(parse_command("/tier banana"), _msg())
    assert "banana" in text or "подкоманду" in text


def test_tier_handler_invalid_ctx_returns_apology():
    store = TierSessionStore(default_tier_registry())
    handler = make_tier_handler(store)
    text = handler(parse_command("/tier"), None)
    assert "Не удалось определить чат" in text


def test_tier_handler_ctx_without_chat_id():
    store = TierSessionStore(default_tier_registry())
    handler = make_tier_handler(store)

    class FakeCtx:
        pass

    text = handler(parse_command("/tier"), FakeCtx())
    assert "Не удалось определить чат" in text


def test_tier_handler_marks_active_in_summary():
    store = TierSessionStore(default_tier_registry())
    store.set_active(42, "ECONOMY")
    handler = make_tier_handler(store)
    text = handler(parse_command("/tier"), _msg(chat_id=42))
    # Active tier line is prefixed with arrow marker
    lines = text.split("\n")
    economy_line = next(line for line in lines if "ECONOMY" in line and "$" in line)
    assert economy_line.startswith("▸")


# ---------------------------------------------------------------------------
# build_command_registry
# ---------------------------------------------------------------------------


def test_build_command_registry_has_all_ten():
    personas = default_registry()
    reg = build_command_registry(personas)
    assert len(reg) == 10
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


def test_build_command_registry_accepts_explicit_tier_store():
    personas = default_registry()
    store = TierSessionStore(default_tier_registry())
    reg = build_command_registry(personas, tier_store=store)
    # Dispatch /tier with our chat_id; choice should land in the SAME store
    msg = IncomingMessage(chat_id=555, user_id=555, message_id=1, text="/tier set PREMIUM")
    reg.dispatch(parse_command("/tier set PREMIUM"), ctx=msg)
    assert store.active_tier_name(555) == "PREMIUM"


def test_build_command_registry_rejects_invalid_tier_store():
    personas = default_registry()
    with pytest.raises(ValueError, match="invalid_tier_store"):
        build_command_registry(personas, tier_store="not a store")  # type: ignore[arg-type]


def test_build_command_registry_dispatches_each_command():
    personas = default_registry()
    reg = build_command_registry(personas)
    for cmd_name in CommandName:
        result = reg.dispatch(parse_command(f"/{cmd_name.value}"))
        assert isinstance(result, str)
        assert result.strip()


def test_build_command_registry_stop_with_runner_idle():
    """When a real idle runner is wired, /stop says nothing is running."""
    from core.background_runner import BackgroundTaskRunner

    runner = BackgroundTaskRunner()
    try:
        personas = default_registry()
        reg = build_command_registry(personas, runner=runner)
        text = reg.dispatch(parse_command("/stop"))
        assert "ничего" in text.lower()
    finally:
        runner.shutdown(wait=False)


def test_build_command_registry_stop_with_mocked_active_runner():
    """When runner.cancel() returns True, /stop says task was stopped."""
    from unittest.mock import MagicMock

    from core.background_runner import BackgroundTaskRunner

    mock_runner = MagicMock(spec=BackgroundTaskRunner)
    mock_runner.cancel.return_value = True

    personas = default_registry()
    reg = build_command_registry(personas, runner=mock_runner)
    text = reg.dispatch(parse_command("/stop"))
    assert "остановлена" in text.lower()
    mock_runner.cancel.assert_called_once()


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
    assert "Доступ" in captured[0].text
    assert "ограничен" in captured[0].text


def test_build_bridge_from_env_uses_simple_handler_when_no_full_env():
    """Without OPENROUTER_API_KEY + REPO_PATH, falls back to simple handler."""
    env = {"TELEGRAM_OWNER_CHAT_ID": "777"}
    send, captured = _captured_send()
    bridge = build_bridge_from_env(env, send_callable=send)
    msg = IncomingMessage(chat_id=777, user_id=777, message_id=1, text="test task")
    bridge.handle(msg)
    # Simple handler acks with the task text
    assert len(captured) == 1
    assert "test task" in captured[0].text


def test_build_bridge_from_env_uses_simple_handler_no_repo_path():
    """OPENROUTER_API_KEY alone (no REPO_PATH) → still simple handler."""
    env = {
        "TELEGRAM_OWNER_CHAT_ID": "777",
        "OPENROUTER_API_KEY": "sk-or-test",
    }
    send, captured = _captured_send()
    bridge = build_bridge_from_env(env, send_callable=send)
    msg = IncomingMessage(chat_id=777, user_id=777, message_id=1, text="hello task")
    bridge.handle(msg)
    assert len(captured) == 1
    assert "hello task" in captured[0].text


def test_build_bridge_from_env_uses_real_handler_with_full_env(tmp_path):
    """OPENROUTER_API_KEY + valid REPO_PATH → real handler (tier-selection prompt).
    send_progress_callable is required when the real pipeline is active.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    env = {
        "TELEGRAM_OWNER_CHAT_ID": "777",
        "OPENROUTER_API_KEY": "sk-or-test",
        "REPO_PATH": str(repo),
    }
    send, captured = _captured_send()
    bridge = build_bridge_from_env(
        env,
        send_callable=send,
        send_progress_callable=lambda _cid, _txt: None,
    )
    msg = IncomingMessage(chat_id=777, user_id=777, message_id=1, text="build me a CLI")
    bridge.handle(msg)
    # Real handler: no tier set → prompts to pick a tier
    assert len(captured) == 1
    assert "тариф" in captured[0].text.lower() or "/tier" in captured[0].text


def test_build_bridge_from_env_accepts_send_progress_callable(tmp_path):
    """send_progress_callable is accepted without error."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    progress_log: list = []
    env = {
        "TELEGRAM_OWNER_CHAT_ID": "777",
        "OPENROUTER_API_KEY": "sk-or-test",
        "REPO_PATH": str(repo),
    }
    send, _ = _captured_send()
    bridge = build_bridge_from_env(
        env,
        send_callable=send,
        send_progress_callable=lambda cid, txt: progress_log.append((cid, txt)),
    )
    assert isinstance(bridge, TelegramBridge)


def test_build_bridge_from_env_requires_send_progress_when_real_pipeline(tmp_path):
    """Real pipeline active (API key + REPO_PATH set) but send_progress_callable
    omitted → ValueError so caller is not silently losing 30+ seconds of events.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    env = {
        "TELEGRAM_OWNER_CHAT_ID": "777",
        "OPENROUTER_API_KEY": "sk-or-test",
        "REPO_PATH": str(repo),
    }
    send, _ = _captured_send()
    with pytest.raises(ValueError, match="send_progress_required_for_real_pipeline"):
        build_bridge_from_env(env, send_callable=send)  # no send_progress_callable


def test_build_bridge_from_env_no_send_progress_ok_without_real_pipeline():
    """Simple pipeline (no REPO_PATH) → send_progress_callable can be omitted."""
    env = {"TELEGRAM_OWNER_CHAT_ID": "777", "OPENROUTER_API_KEY": "sk-or-test"}
    send, _ = _captured_send()
    # Must not raise — real pipeline won't activate without REPO_PATH
    bridge = build_bridge_from_env(env, send_callable=send)
    assert isinstance(bridge, TelegramBridge)


def test_build_bridge_from_env_no_send_progress_ok_when_repo_path_invalid(tmp_path):
    """REPO_PATH is set but points at a non-git directory → real pipeline won't
    activate → send_progress_callable not required → no ValueError.

    Without the precise eligibility check (only env-var presence), this case
    would falsely raise 'send_progress_required_for_real_pipeline' even though
    the bridge falls back to the simple handler.
    """
    not_a_repo = tmp_path / "not_a_git_repo"
    not_a_repo.mkdir()
    # Note: NO .git subdir → SandboxConfig will reject this path
    env = {
        "TELEGRAM_OWNER_CHAT_ID": "777",
        "OPENROUTER_API_KEY": "sk-or-test",
        "REPO_PATH": str(not_a_repo),
    }
    send, captured = _captured_send()
    # Must not raise — real pipeline cannot activate, so send_progress is optional
    bridge = build_bridge_from_env(env, send_callable=send)
    assert isinstance(bridge, TelegramBridge)
    # Confirm fallback: simple handler is what answers free-text tasks
    msg = IncomingMessage(chat_id=777, user_id=777, message_id=1, text="hi there")
    bridge.handle(msg)
    assert len(captured) == 1
    assert "hi there" in captured[0].text  # simple handler echoes the task


def test_build_bridge_from_env_no_send_progress_ok_when_repo_path_missing(tmp_path):
    """REPO_PATH points at a non-existent directory → falls back to simple handler
    even with API key present. send_progress_callable not required.
    """
    env = {
        "TELEGRAM_OWNER_CHAT_ID": "777",
        "OPENROUTER_API_KEY": "sk-or-test",
        "REPO_PATH": str(tmp_path / "does_not_exist"),
    }
    send, _ = _captured_send()
    bridge = build_bridge_from_env(env, send_callable=send)
    assert isinstance(bridge, TelegramBridge)


# ---------------------------------------------------------------------------
# make_push_handler (Step 14c-1)
# ---------------------------------------------------------------------------


def _make_sandbox_for_push(tmp_path):
    from core.sandbox_workspace import (
        SandboxConfig,
        SandboxWorkspace,
        _RunResult,
        _SubprocessRunner,
    )

    class _OkRunner(_SubprocessRunner):
        def __init__(self):
            self.calls = []

        def run(self, cmd, cwd, env, timeout):
            self.calls.append({"cmd": cmd, "cwd": cwd})
            return _RunResult(returncode=0, stdout="", stderr="")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    cfg = SandboxConfig(
        main_repo_path=repo,
        worktree_root=tmp_path / "worktrees",
    )
    runner = _OkRunner()
    return SandboxWorkspace(cfg, runner=runner), runner


def _make_push_summary(task_id="task-push-001", commit_sha="deadbeef12345678"):
    import time

    from core.task_history import TaskSummary
    return TaskSummary(
        task_id=task_id,
        branch=f"feature/{task_id}",
        commit_sha=commit_sha,
        final_state="SUCCESS",
        failure_reason=None,
        tier_name="ECONOMY",
        finished_at=time.time(),
    )


def test_make_push_handler_returns_callable():
    handler = make_push_handler()
    assert callable(handler)


def test_push_handler_stub_when_no_sandbox():
    handler = make_push_handler(sandbox=None, task_history=None)
    cmd = parse_command("/push task-001")
    result = handler(cmd, None)
    assert "REPO_PATH" in result or "полном режиме" in result


def test_push_handler_rejects_invalid_sandbox():
    with pytest.raises(ValueError, match="invalid_sandbox"):
        make_push_handler(sandbox="not a sandbox")  # type: ignore[arg-type]


def test_push_handler_rejects_invalid_task_history(tmp_path):
    sandbox, _ = _make_sandbox_for_push(tmp_path)
    with pytest.raises(ValueError, match="invalid_task_history"):
        make_push_handler(sandbox=sandbox, task_history="not history")  # type: ignore[arg-type]


def test_push_handler_missing_task_id_arg(tmp_path):
    sandbox, _ = _make_sandbox_for_push(tmp_path)
    history = TaskHistory()
    handler = make_push_handler(sandbox=sandbox, task_history=history)
    cmd = parse_command("/push")
    result = handler(cmd, None)
    assert "task_id" in result.lower() or "укажи" in result.lower()


def test_push_handler_unknown_task_id(tmp_path):
    sandbox, _ = _make_sandbox_for_push(tmp_path)
    history = TaskHistory()
    handler = make_push_handler(sandbox=sandbox, task_history=history)
    cmd = parse_command("/push task-unknown")
    result = handler(cmd, None)
    assert "не найден" in result


def test_push_handler_refuses_failed_task(tmp_path):
    import time

    from core.task_history import TaskSummary
    sandbox, _ = _make_sandbox_for_push(tmp_path)
    history = TaskHistory()
    history.record(TaskSummary(
        task_id="task-fail",
        branch="feature/task-fail",
        commit_sha=None,
        final_state="FAIL",
        failure_reason="ruff_error",
        tier_name="ECONOMY",
        finished_at=time.time(),
    ))
    handler = make_push_handler(sandbox=sandbox, task_history=history)
    cmd = parse_command("/push task-fail")
    result = handler(cmd, None)
    assert "SUCCESS" in result or "нечего пушить" in result


def test_push_handler_success_calls_push_branch_from_main(tmp_path):
    sandbox, runner = _make_sandbox_for_push(tmp_path)
    history = TaskHistory()
    summary = _make_push_summary()
    history.record(summary)
    handler = make_push_handler(sandbox=sandbox, task_history=history)
    cmd = parse_command("/push task-push-001")
    result = handler(cmd, None)
    assert "Запушено" in result
    assert len(runner.calls) == 1
    assert runner.calls[0]["cmd"] == ("git", "push", "origin", "feature/task-push-001")


def test_push_handler_git_failure_returns_error_message(tmp_path):
    from core.sandbox_workspace import (
        SandboxConfig,
        SandboxWorkspace,
        _RunResult,
        _SubprocessRunner,
    )

    class _FailRunner(_SubprocessRunner):
        def run(self, cmd, cwd, env, timeout):
            return _RunResult(returncode=1, stdout="", stderr="fatal: rejected")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    cfg = SandboxConfig(main_repo_path=repo, worktree_root=tmp_path / "wt")
    sandbox = SandboxWorkspace(cfg, runner=_FailRunner())

    history = TaskHistory()
    history.record(_make_push_summary())
    handler = make_push_handler(sandbox=sandbox, task_history=history)
    cmd = parse_command("/push task-push-001")
    result = handler(cmd, None)
    assert "Push не удался" in result or "❌" in result


def test_push_handler_shows_commit_sha_short_on_success(tmp_path):
    sandbox, _ = _make_sandbox_for_push(tmp_path)
    history = TaskHistory()
    history.record(_make_push_summary(commit_sha="cafebabe12345678"))
    handler = make_push_handler(sandbox=sandbox, task_history=history)
    result = handler(parse_command("/push task-push-001"), None)
    assert "cafebabe" in result  # first 8 chars of sha


def test_build_command_registry_with_sandbox_wires_push(tmp_path):
    """When sandbox+task_history are passed, /push performs real push logic."""
    sandbox, runner = _make_sandbox_for_push(tmp_path)
    history = TaskHistory()
    history.record(_make_push_summary())

    personas = default_registry()
    reg = build_command_registry(personas, sandbox=sandbox, task_history=history)
    result = reg.dispatch(parse_command("/push task-push-001"))
    assert "Запушено" in result
    assert len(runner.calls) == 1
