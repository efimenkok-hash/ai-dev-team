"""Tests for core.bot_runner (Step 14a Module 7 + 14b-6/7: builders + handlers)."""

import time

import pytest

from core.agent_personas import default_registry
from core.bot_commands import (
    BotCommand,
    CommandName,
    parse_command,
)
from core.bot_runner import (
    _BudgetState,
    _build_observability,
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
    make_pr_handler,
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
from core.state_db import StateDB
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
# cleanup_orphan_worktrees_from_env
# ---------------------------------------------------------------------------


def test_cleanup_orphans_returns_zero_without_real_pipeline():
    """No OPENROUTER_API_KEY / REPO_PATH → no sandbox → 0 orphans removed."""
    from core.bot_runner import cleanup_orphan_worktrees_from_env
    assert cleanup_orphan_worktrees_from_env({}) == 0


def test_cleanup_orphans_returns_zero_when_repo_invalid(tmp_path):
    """REPO_PATH points at non-git directory → sandbox build fails → returns 0."""
    from core.bot_runner import cleanup_orphan_worktrees_from_env
    not_repo = tmp_path / "not_repo"
    not_repo.mkdir()
    result = cleanup_orphan_worktrees_from_env({
        "OPENROUTER_API_KEY": "sk-or-test",
        "REPO_PATH": str(not_repo),
    })
    assert result == 0


def test_cleanup_orphans_calls_sandbox_cleanup_when_eligible(tmp_path):
    """With valid REPO_PATH and API key → calls sandbox.cleanup_orphans()."""
    from unittest.mock import patch

    from core.bot_runner import cleanup_orphan_worktrees_from_env

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    # Mock cleanup_orphans on whatever SandboxWorkspace _try_build_sandbox returns
    with patch(
        "core.sandbox_workspace.SandboxWorkspace.cleanup_orphans",
        return_value=3,
    ) as mock_cleanup:
        result = cleanup_orphan_worktrees_from_env({
            "OPENROUTER_API_KEY": "sk-or-test",
            "REPO_PATH": str(repo),
        })

    assert result == 3
    mock_cleanup.assert_called_once()


def test_cleanup_orphans_swallows_exceptions(tmp_path):
    """If cleanup_orphans raises, we MUST return 0 — startup must never crash."""
    from unittest.mock import patch

    from core.bot_runner import cleanup_orphan_worktrees_from_env

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    with patch(
        "core.sandbox_workspace.SandboxWorkspace.cleanup_orphans",
        side_effect=RuntimeError("git exploded"),
    ):
        result = cleanup_orphan_worktrees_from_env({
            "OPENROUTER_API_KEY": "sk-or-test",
            "REPO_PATH": str(repo),
        })

    assert result == 0  # startup must not crash


def test_cleanup_orphans_rejects_non_mapping():
    from core.bot_runner import cleanup_orphan_worktrees_from_env
    with pytest.raises(ValueError, match="env_must_be_mapping"):
        cleanup_orphan_worktrees_from_env("not a mapping")  # type: ignore[arg-type]


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
# _build_observability — OBS_LOG_PATH wiring
# ---------------------------------------------------------------------------


def test_obs_log_path_creates_observability_with_jsonlines_sink(tmp_path):
    """OBS_LOG_PATH in env → make_real_task_handler receives Observability."""
    from core.observability import JsonLinesSink, Observability

    log_file = tmp_path / "obs.jsonl"
    obs = _build_observability({"OBS_LOG_PATH": str(log_file)})

    assert isinstance(obs, Observability)
    assert isinstance(obs.sink, JsonLinesSink)
    assert obs.sink.path == log_file


def test_obs_log_path_absent_returns_none():
    """No OBS_LOG_PATH → _build_observability returns None."""
    assert _build_observability({}) is None
    assert _build_observability({"OBS_LOG_PATH": ""}) is None
    assert _build_observability({"OBS_LOG_PATH": "   "}) is None


def test_obs_log_path_bad_path_returns_none(tmp_path):
    """Unreachable path (parent does not exist and cannot be created)
    must not raise — returns None silently."""
    # /dev/null is a file; writing a directory under it will fail with OSError
    obs = _build_observability({"OBS_LOG_PATH": "/dev/null/impossible/x.jsonl"})
    assert obs is None


def test_obs_wired_into_make_real_task_handler_when_obs_log_path_set(tmp_path):
    """build_real_task_handler_from_env passes observability= to make_real_task_handler
    when OBS_LOG_PATH is set."""
    from unittest.mock import patch

    from core.observability import Observability

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    log_file = tmp_path / "obs.jsonl"
    store = TierSessionStore(default_tier_registry())

    captured: list[Observability | None] = []

    original_make = __import__(
        "core.real_task_handler", fromlist=["make_real_task_handler"]
    ).make_real_task_handler

    def capturing_make(**kwargs):
        captured.append(kwargs.get("observability"))
        return original_make(**kwargs)

    with patch("core.bot_runner.make_real_task_handler", side_effect=capturing_make):
        build_real_task_handler_from_env(
            {
                "OPENROUTER_API_KEY": "sk-or-test",
                "REPO_PATH": str(repo),
                "OBS_LOG_PATH": str(log_file),
            },
            tier_store=store,
            send_progress=_noop_progress,
        )

    assert len(captured) == 1
    assert isinstance(captured[0], Observability)


def test_obs_none_when_obs_log_path_absent_in_full_env(tmp_path):
    """build_real_task_handler_from_env passes observability=None when
    OBS_LOG_PATH is not set."""
    from unittest.mock import patch

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    store = TierSessionStore(default_tier_registry())

    captured: list = []

    original_make = __import__(
        "core.real_task_handler", fromlist=["make_real_task_handler"]
    ).make_real_task_handler

    def capturing_make(**kwargs):
        captured.append(kwargs.get("observability"))
        return original_make(**kwargs)

    with patch("core.bot_runner.make_real_task_handler", side_effect=capturing_make):
        build_real_task_handler_from_env(
            {"OPENROUTER_API_KEY": "sk-or-test", "REPO_PATH": str(repo)},
            tier_store=store,
            send_progress=_noop_progress,
        )

    assert len(captured) == 1
    assert captured[0] is None


# ---------------------------------------------------------------------------
# build_whisper_client / build_vision_client (optional clients)
# ---------------------------------------------------------------------------


def test_build_whisper_client_with_key():
    c = build_whisper_client({"OPENAI_API_KEY": "sk-test"})
    assert isinstance(c, WhisperClient)


def test_build_dispatcher_from_env_wires_observability():
    from core.observability import Observability

    obs = Observability()
    dispatcher = build_dispatcher_from_env(
        {"OPENROUTER_API_KEY": "sk-test"},
        observability=obs,
    )

    assert dispatcher is not None
    assert dispatcher._obs is obs


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


def test_budget_state_rejects_invalid_state_db():
    with pytest.raises(ValueError, match="invalid_state_db"):
        _BudgetState(initial_usd=10.0, state_db="bad")  # type: ignore[arg-type]


def test_budget_handler_scopes_budget_per_chat_in_memory():
    state = _BudgetState(initial_usd=10.0)
    handler = make_budget_handler(state)
    chat_1 = IncomingMessage(chat_id=101, user_id=101, message_id=1, text="/budget")
    chat_2 = IncomingMessage(chat_id=202, user_id=202, message_id=2, text="/budget")

    handler(parse_command("/budget 25.5"), chat_1)

    assert state.get_budget(101) == 25.5
    assert "$25.50" in handler(parse_command("/budget"), chat_1)
    assert "$10.00" in handler(parse_command("/budget"), chat_2)


def test_budget_handler_persists_budget_to_state_db(tmp_path):
    db = StateDB(tmp_path / "state.db")
    state = _BudgetState(initial_usd=10.0, state_db=db)
    handler = make_budget_handler(state)
    msg = IncomingMessage(chat_id=303, user_id=303, message_id=1, text="/budget")

    handler(parse_command("/budget 77"), msg)

    assert db.get_budget(303) == pytest.approx(77.0)
    restarted = _BudgetState(initial_usd=10.0, state_db=db)
    assert restarted.get_budget(303) == pytest.approx(77.0)


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


def test_log_handler_no_history_returns_stub():
    """Without task_history /log must return an informative stub."""
    handler = make_log_handler()
    text = handler(parse_command("/log"), None)
    assert isinstance(text, str)
    assert "📜" in text
    assert "недоступен" in text.lower() or "не настроен" in text.lower()


def test_log_handler_rejects_invalid_task_history():
    with pytest.raises(ValueError, match="invalid_task_history"):
        make_log_handler(task_history="not history")  # type: ignore[arg-type]


def test_log_handler_empty_history():

    h = TaskHistory()
    handler = make_log_handler(task_history=h)
    text = handler(parse_command("/log"), None)
    assert "пуста" in text.lower()


def test_log_handler_recent_list():
    """Without args, /log lists up to 5 most recent tasks."""
    import time

    from core.task_history import TaskSummary

    h = TaskHistory()
    for i in range(3):
        h.record(
            TaskSummary(
                task_id=f"task-{i}",
                branch=f"feature/task-{i}",
                commit_sha="abc1234",
                final_state="SUCCESS",
                failure_reason=None,
                tier_name="ECONOMY",
                finished_at=time.time() + i,
            )
        )
    handler = make_log_handler(task_history=h)
    text = handler(parse_command("/log"), None)
    assert "task-0" in text
    assert "task-2" in text
    assert "✅" in text


def test_log_handler_task_id_lookup_found():
    """'/log task-x' returns details for a known task."""
    import time

    from core.task_history import TaskSummary

    h = TaskHistory()
    h.record(
        TaskSummary(
            task_id="task-abc",
            branch="feature/task-abc",
            commit_sha="deadbeef12345",
            final_state="SUCCESS",
            failure_reason=None,
            tier_name="PREMIUM",
            finished_at=time.time(),
        )
    )
    handler = make_log_handler(task_history=h)
    text = handler(parse_command("/log task-abc"), None)
    assert "task-abc" in text
    assert "deadbee" in text   # first 7 chars of SHA
    assert "PREMIUM" in text
    assert "SUCCESS" in text


def test_log_handler_task_id_lookup_not_found():
    """'/log unknown' returns a not-found message without crashing."""

    h = TaskHistory()
    handler = make_log_handler(task_history=h)
    text = handler(parse_command("/log unknown-task"), None)
    assert "unknown-task" in text
    assert "не найден" in text.lower()


def test_log_handler_failed_task_shows_reason():
    """Failed task with failure_reason shows the reason."""
    import time

    from core.task_history import TaskSummary

    h = TaskHistory()
    h.record(
        TaskSummary(
            task_id="task-fail",
            branch="feature/task-fail",
            commit_sha=None,
            final_state="FAIL",
            failure_reason="ruff_error",
            tier_name="ECONOMY",
            finished_at=time.time(),
        )
    )
    handler = make_log_handler(task_history=h)
    text = handler(parse_command("/log task-fail"), None)
    assert "❌" in text
    assert "ruff_error" in text


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


def test_build_command_registry_has_all_eleven():
    personas = default_registry()
    reg = build_command_registry(personas)
    assert len(reg) == 11
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


def test_build_command_registry_rejects_invalid_state_db():
    personas = default_registry()
    with pytest.raises(ValueError, match="invalid_state_db"):
        build_command_registry(personas, state_db="bad")  # type: ignore[arg-type]


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


def test_build_command_registry_wires_budget_state_db(tmp_path):
    personas = default_registry()
    db = StateDB(tmp_path / "state.db")
    reg = build_command_registry(personas, state_db=db)
    msg = IncomingMessage(chat_id=404, user_id=404, message_id=1, text="/budget 12.5")

    reg.dispatch(parse_command("/budget 12.5"), ctx=msg)

    assert db.get_budget(404) == pytest.approx(12.5)


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


def _bridge_env(tmp_path, **overrides):
    env = {
        "TELEGRAM_OWNER_CHAT_ID": "12345",
        "STATE_DB_PATH": str(tmp_path / "state.db"),
    }
    env.update(overrides)
    return env


def test_build_bridge_from_env_minimal(tmp_path):
    env = _bridge_env(tmp_path)
    send, _ = _captured_send()
    bridge = build_bridge_from_env(env, send_callable=send)
    assert isinstance(bridge, TelegramBridge)


def test_build_bridge_from_env_with_all_keys(tmp_path):
    env = _bridge_env(
        tmp_path,
        OPENAI_API_KEY="sk-test",
        OPENROUTER_API_KEY="sk-or-test",
        BOT_COST_THRESHOLD_USD="2.5",
    )
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


def test_build_bridge_from_env_end_to_end_flow(tmp_path):
    """Smoke: a full message flow through the assembled bridge."""
    env = _bridge_env(tmp_path, TELEGRAM_OWNER_CHAT_ID="777")
    send, captured = _captured_send()
    bridge = build_bridge_from_env(env, send_callable=send)
    msg = IncomingMessage(chat_id=777, user_id=777, message_id=1, text="привет")
    bridge.handle(msg)
    assert len(captured) == 1
    assert captured[0].text.startswith("Менеджер:")
    assert "привет" in captured[0].text


def test_build_bridge_from_env_command_flow(tmp_path):
    env = _bridge_env(tmp_path, TELEGRAM_OWNER_CHAT_ID="777")
    send, captured = _captured_send()
    bridge = build_bridge_from_env(env, send_callable=send)
    msg = IncomingMessage(chat_id=777, user_id=777, message_id=1, text="/help")
    bridge.handle(msg)
    assert len(captured) == 1
    assert "/help" in captured[0].text
    assert captured[0].text.startswith("Менеджер:")


def test_build_bridge_from_env_intruder_denied(tmp_path):
    env = _bridge_env(tmp_path, TELEGRAM_OWNER_CHAT_ID="777")
    send, captured = _captured_send()
    bridge = build_bridge_from_env(env, send_callable=send)
    msg = IncomingMessage(chat_id=999, user_id=999, message_id=1, text="привет")
    bridge.handle(msg)
    assert len(captured) == 1
    assert "Доступ" in captured[0].text
    assert "ограничен" in captured[0].text


def test_build_bridge_from_env_uses_simple_handler_when_no_full_env(tmp_path):
    """Without OPENROUTER_API_KEY + REPO_PATH, falls back to simple handler."""
    env = _bridge_env(tmp_path, TELEGRAM_OWNER_CHAT_ID="777")
    send, captured = _captured_send()
    bridge = build_bridge_from_env(env, send_callable=send)
    msg = IncomingMessage(chat_id=777, user_id=777, message_id=1, text="test task")
    bridge.handle(msg)
    # Simple handler acks with the task text
    assert len(captured) == 1
    assert "test task" in captured[0].text


def test_build_bridge_from_env_uses_simple_handler_no_repo_path(tmp_path):
    """OPENROUTER_API_KEY alone (no REPO_PATH) → still simple handler."""
    env = _bridge_env(
        tmp_path,
        TELEGRAM_OWNER_CHAT_ID="777",
        OPENROUTER_API_KEY="sk-or-test",
    )
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
    env = _bridge_env(
        tmp_path,
        TELEGRAM_OWNER_CHAT_ID="777",
        OPENROUTER_API_KEY="sk-or-test",
        REPO_PATH=str(repo),
    )
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
    env = _bridge_env(
        tmp_path,
        TELEGRAM_OWNER_CHAT_ID="777",
        OPENROUTER_API_KEY="sk-or-test",
        REPO_PATH=str(repo),
    )
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
    env = _bridge_env(
        tmp_path,
        TELEGRAM_OWNER_CHAT_ID="777",
        OPENROUTER_API_KEY="sk-or-test",
        REPO_PATH=str(repo),
    )
    send, _ = _captured_send()
    with pytest.raises(ValueError, match="send_progress_required_for_real_pipeline"):
        build_bridge_from_env(env, send_callable=send)  # no send_progress_callable


def test_build_bridge_from_env_no_send_progress_ok_without_real_pipeline(tmp_path):
    """Simple pipeline (no REPO_PATH) → send_progress_callable can be omitted."""
    env = _bridge_env(
        tmp_path,
        TELEGRAM_OWNER_CHAT_ID="777",
        OPENROUTER_API_KEY="sk-or-test",
    )
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
    env = _bridge_env(
        tmp_path,
        TELEGRAM_OWNER_CHAT_ID="777",
        OPENROUTER_API_KEY="sk-or-test",
        REPO_PATH=str(not_a_repo),
    )
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
    env = _bridge_env(
        tmp_path,
        TELEGRAM_OWNER_CHAT_ID="777",
        OPENROUTER_API_KEY="sk-or-test",
        REPO_PATH=str(tmp_path / "does_not_exist"),
    )
    send, _ = _captured_send()
    bridge = build_bridge_from_env(env, send_callable=send)
    assert isinstance(bridge, TelegramBridge)


def test_build_bridge_stub_mode_does_not_spawn_runner(tmp_path):
    """Minorka #1: In simple-stub mode (no real pipeline) no BackgroundTaskRunner
    thread should be created — it would be an idle resource waste."""
    from unittest.mock import MagicMock, patch

    from core.background_runner import BackgroundTaskRunner

    mock_runner_cls = MagicMock(spec=type(BackgroundTaskRunner))
    env = _bridge_env(tmp_path, TELEGRAM_OWNER_CHAT_ID="777")
    send, _ = _captured_send()

    with patch("core.bot_runner.BackgroundTaskRunner", mock_runner_cls):
        bridge = build_bridge_from_env(env, send_callable=send)

    mock_runner_cls.assert_not_called()
    assert isinstance(bridge, TelegramBridge)


def test_build_bridge_from_env_persists_tier_and_budget_in_state_db(tmp_path):
    env = _bridge_env(tmp_path, TELEGRAM_OWNER_CHAT_ID="777")
    send, captured = _captured_send()
    first = build_bridge_from_env(env, send_callable=send)

    first.handle(IncomingMessage(chat_id=777, user_id=777, message_id=1, text="/tier set PREMIUM"))
    first.handle(IncomingMessage(chat_id=777, user_id=777, message_id=2, text="/budget 33"))
    captured.clear()

    restarted = build_bridge_from_env(env, send_callable=send)
    restarted.handle(IncomingMessage(chat_id=777, user_id=777, message_id=3, text="/tier"))
    restarted.handle(IncomingMessage(chat_id=777, user_id=777, message_id=4, text="/budget"))

    assert any("PREMIUM" in out.text for out in captured)
    assert any("$33.00" in out.text for out in captured)


def test_build_bridge_from_env_migrates_legacy_tier_sessions_json(tmp_path):
    import json

    state_dir = tmp_path / "state-dir"
    state_dir.mkdir()
    legacy = state_dir / "tier_sessions.json"
    legacy.write_text(json.dumps({
        "schema_version": 1,
        "sessions": [
            {"chat_id": 777, "active_tier": "ECONOMY", "last_changed_at": 123.0},
        ],
    }), encoding="utf-8")
    state_db_path = state_dir / "state.db"
    env = {
        "TELEGRAM_OWNER_CHAT_ID": "777",
        "BOT_STATE_DIR": str(state_dir),
        "STATE_DB_PATH": str(state_db_path),
    }
    send, _ = _captured_send()

    build_bridge_from_env(env, send_callable=send)

    db = StateDB(state_db_path)
    assert db.get_tier(777) == "ECONOMY"
    assert not legacy.exists()


# ---------------------------------------------------------------------------
# make_push_handler (Step 16)
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

    from core.task_history import TaskHistory, TaskSummary

    h = TaskHistory()
    h.record(TaskSummary(
        task_id=task_id,
        branch=f"feature/{task_id}",
        commit_sha=commit_sha,
        final_state="SUCCESS",
        failure_reason=None,
        tier_name="ECONOMY",
        finished_at=time.time(),
    ))
    return h


def test_make_push_handler_returns_callable():
    handler = make_push_handler()
    assert callable(handler)


def test_push_handler_stub_when_no_sandbox():
    """Without sandbox /push returns a helpful stub."""
    handler = make_push_handler(sandbox=None, task_history=None)
    result = handler(parse_command("/push task-001"), None)
    assert "REPO_PATH" in result or "недоступен" in result


def test_push_handler_stub_when_no_task_history(tmp_path):
    """Without task_history /push returns a stub (can't guard against failed tasks)."""
    sandbox, _ = _make_sandbox_for_push(tmp_path)
    handler = make_push_handler(sandbox=sandbox, task_history=None)
    result = handler(parse_command("/push task-001"), None)
    assert "недоступен" in result or "REPO_PATH" in result


def test_push_handler_rejects_invalid_sandbox():
    with pytest.raises(ValueError, match="invalid_sandbox"):
        make_push_handler(sandbox="not a sandbox")  # type: ignore[arg-type]


def test_push_handler_rejects_invalid_task_history(tmp_path):
    sandbox, _ = _make_sandbox_for_push(tmp_path)
    with pytest.raises(ValueError, match="invalid_task_history"):
        make_push_handler(sandbox=sandbox, task_history="not history")  # type: ignore[arg-type]


def test_push_handler_missing_task_id_arg(tmp_path):
    """'/push' with no args returns usage hint."""
    sandbox, _ = _make_sandbox_for_push(tmp_path)
    history = TaskHistory()
    handler = make_push_handler(sandbox=sandbox, task_history=history)
    result = handler(parse_command("/push"), None)
    assert "task_id" in result.lower() or "укажи" in result.lower()


def test_push_handler_malicious_task_id_rejected(tmp_path):
    """Shell-meta and path-traversal in task_id must be rejected before TaskHistory lookup."""
    from core.bot_commands import BotCommand, CommandName

    sandbox, runner = _make_sandbox_for_push(tmp_path)
    history = TaskHistory()
    handler = make_push_handler(sandbox=sandbox, task_history=history)

    bad_ids = ["../evil", "UPPER_CASE", "task;rm", "task&ls", "task|cat"]
    for bad in bad_ids:
        cmd = BotCommand(
            name=CommandName.PUSH,
            args=(bad,),
            raw_text=f"/push {bad}",
        )
        result = handler(cmd, None)
        assert "❌" in result or "Некорректный" in result, f"expected rejection for {bad!r}: {result}"
    assert len(runner.calls) == 0, "no git calls on malicious input"


def test_push_handler_refuses_task_not_in_history(tmp_path):
    """task_id valid regex but not in TaskHistory → 'не найден'."""
    sandbox, runner = _make_sandbox_for_push(tmp_path)
    history = TaskHistory()  # empty
    handler = make_push_handler(sandbox=sandbox, task_history=history)
    result = handler(parse_command("/push task-unknown"), None)
    assert "не найден" in result
    assert len(runner.calls) == 0


def test_push_handler_refuses_failed_task(tmp_path):
    """Task in history with commit_sha=None → refused, no git push."""
    import time

    from core.task_history import TaskHistory, TaskSummary

    sandbox, runner = _make_sandbox_for_push(tmp_path)
    history = TaskHistory()
    history.record(TaskSummary(
        task_id="task-fail",
        branch="feature/task-fail",
        commit_sha=None,  # no commit → FAIL or CANCELLED
        final_state="FAIL",
        failure_reason="ruff_error",
        tier_name="ECONOMY",
        finished_at=time.time(),
    ))
    handler = make_push_handler(sandbox=sandbox, task_history=history)
    result = handler(parse_command("/push task-fail"), None)
    assert "нечего пушить" in result or "SUCCESS" in result
    assert len(runner.calls) == 0


def test_push_handler_success_calls_push_named_branch(tmp_path):
    """Happy path: SUCCESS task in history → push_named_branch called."""
    sandbox, runner = _make_sandbox_for_push(tmp_path)
    history = _make_push_summary()
    handler = make_push_handler(sandbox=sandbox, task_history=history)
    result = handler(parse_command("/push task-push-001"), None)
    assert "✅" in result
    assert "feature/task-push-001" in result
    assert len(runner.calls) == 1
    assert runner.calls[0]["cmd"] == ("git", "push", "origin", "feature/task-push-001")


def test_push_handler_shows_commit_sha_short_on_success(tmp_path):
    sandbox, _ = _make_sandbox_for_push(tmp_path)
    history = _make_push_summary(commit_sha="cafebabe12345678")
    handler = make_push_handler(sandbox=sandbox, task_history=history)
    result = handler(parse_command("/push task-push-001"), None)
    assert "cafebabe" in result  # first 8 chars


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
    history = _make_push_summary()
    handler = make_push_handler(sandbox=sandbox, task_history=history)
    result = handler(parse_command("/push task-push-001"), None)
    assert "❌" in result
    assert "Не удалось запушить" in result or "запушить" in result


def test_build_command_registry_with_sandbox_wires_push(tmp_path):
    """When sandbox+task_history are passed, /push performs real push for SUCCESS tasks."""
    sandbox, runner = _make_sandbox_for_push(tmp_path)
    history = _make_push_summary()
    personas = default_registry()
    reg = build_command_registry(personas, sandbox=sandbox, task_history=history)
    result = reg.dispatch(parse_command("/push task-push-001"))
    assert "✅" in result
    assert len(runner.calls) == 1


# ---------------------------------------------------------------------------
# make_pr_handler (Step 17)
# ---------------------------------------------------------------------------


def _make_sandbox_for_pr(tmp_path, *, gh_returns: tuple[int, str, str] = (0, "", "")):
    """Build a SandboxWorkspace whose subprocess runner returns canned results.

    gh_returns: (returncode, stdout, stderr) for the gh subprocess.
    Both `git push` and `gh pr create` go through the same runner — for
    these tests we don't need to distinguish, just feed back success.
    """
    from core.sandbox_workspace import (
        SandboxConfig,
        SandboxWorkspace,
        _RunResult,
        _SubprocessRunner,
    )

    class _CannedRunner(_SubprocessRunner):
        def __init__(self):
            self.calls = []

        def run(self, cmd, cwd, env, timeout):
            self.calls.append({"cmd": cmd, "cwd": cwd})
            # Distinguish: git push (cmd[0]="git") vs gh (cmd[0]="gh")
            if cmd[0] == "gh":
                return _RunResult(*gh_returns)
            return _RunResult(returncode=0, stdout="", stderr="")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    cfg = SandboxConfig(main_repo_path=repo, worktree_root=tmp_path / "wt")
    runner = _CannedRunner()
    return SandboxWorkspace(cfg, runner=runner), runner


def test_pr_handler_stub_when_no_sandbox():
    handler = make_pr_handler(sandbox=None, task_history=None)
    result = handler(parse_command("/pr task-x"), None)
    assert "недоступен" in result.lower() or "REPO_PATH" in result
    assert "/pr" in result


def test_pr_handler_stub_when_no_history(tmp_path):
    sandbox, _ = _make_sandbox_for_pr(tmp_path)
    handler = make_pr_handler(sandbox=sandbox, task_history=None)
    result = handler(parse_command("/pr task-x"), None)
    assert "недоступен" in result.lower() or "REPO_PATH" in result


def test_pr_handler_rejects_invalid_sandbox():
    with pytest.raises(ValueError, match="invalid_sandbox"):
        make_pr_handler(sandbox="not a sandbox")  # type: ignore[arg-type]


def test_pr_handler_rejects_invalid_task_history():
    with pytest.raises(ValueError, match="invalid_task_history"):
        make_pr_handler(task_history="not a history")  # type: ignore[arg-type]


def test_pr_handler_no_args(tmp_path):
    sandbox, _ = _make_sandbox_for_pr(tmp_path)
    history = _make_push_summary()
    handler = make_pr_handler(sandbox=sandbox, task_history=history)
    result = handler(parse_command("/pr"), None)
    assert "укажи task_id" in result.lower() or "task_id" in result.lower()


def test_pr_handler_rejects_invalid_task_id(tmp_path):
    sandbox, _ = _make_sandbox_for_pr(tmp_path)
    history = _make_push_summary()
    handler = make_pr_handler(sandbox=sandbox, task_history=history)
    # Uppercase, semicolons, traversal — must all be rejected
    for bad in ["FOO", "task-id;rm-rf", "../etc"]:
        cmd = BotCommand(name=CommandName.PR, args=(bad,), raw_text=f"/pr {bad}")
        result = handler(cmd, None)
        assert "Некорректный" in result or "❌" in result


def test_pr_handler_task_not_in_history(tmp_path):
    sandbox, _ = _make_sandbox_for_pr(tmp_path)
    from core.task_history import TaskHistory
    empty = TaskHistory()
    handler = make_pr_handler(sandbox=sandbox, task_history=empty)
    result = handler(parse_command("/pr task-missing-001"), None)
    assert "не найден" in result.lower()


def test_pr_handler_refuses_failed_task(tmp_path):
    """Failed task (commit_sha=None) → нечего пушить, no gh call."""
    from core.task_history import TaskHistory, TaskSummary

    sandbox, runner = _make_sandbox_for_pr(tmp_path)
    history = TaskHistory()
    history.record(TaskSummary(
        task_id="task-fail-001",
        branch="feature/task-fail-001",
        commit_sha=None,
        final_state="FAIL",
        failure_reason="agent_exception",
        tier_name="ECONOMY",
        finished_at=time.time(),
    ))
    handler = make_pr_handler(sandbox=sandbox, task_history=history)
    result = handler(parse_command("/pr task-fail-001"), None)
    assert "не достигла SUCCESS" in result or "нечего пушить" in result
    # No git push, no gh — the guard fires before any subprocess is run
    assert all(c["cmd"][0] != "gh" for c in runner.calls)


def test_pr_handler_happy_path(tmp_path):
    """Successful task → push + gh pr create + return PR URL."""
    pr_url = "https://github.com/user/repo/pull/42"
    gh_stdout = f"\nCreating draft pull request for X into Y\n{pr_url}\n"
    sandbox, runner = _make_sandbox_for_pr(
        tmp_path, gh_returns=(0, gh_stdout, ""),
    )
    history = _make_push_summary()
    handler = make_pr_handler(sandbox=sandbox, task_history=history)
    result = handler(parse_command("/pr task-push-001"), None)
    assert "🪄" in result or "Draft PR" in result
    assert pr_url in result
    # Verify both git push (idempotent) and gh pr create were invoked
    cmd_kinds = [c["cmd"][0] for c in runner.calls]
    assert "git" in cmd_kinds  # push
    assert "gh" in cmd_kinds


def test_pr_handler_gh_not_found(tmp_path):
    """When gh CLI returns 127 (not installed), surface a helpful message."""
    sandbox, _ = _make_sandbox_for_pr(
        tmp_path,
        gh_returns=(127, "", "gh: command not found"),
    )
    history = _make_push_summary()
    handler = make_pr_handler(sandbox=sandbox, task_history=history)
    result = handler(parse_command("/pr task-push-001"), None)
    assert "`gh`" in result or "gh CLI" in result.lower()
    assert "не найден" in result.lower() or "auth login" in result


def test_pr_handler_gh_failure(tmp_path):
    """When gh returns non-zero (auth, network, branch missing), report error."""
    sandbox, _ = _make_sandbox_for_pr(
        tmp_path,
        gh_returns=(1, "", "GraphQL: Resource not accessible by integration"),
    )
    history = _make_push_summary()
    handler = make_pr_handler(sandbox=sandbox, task_history=history)
    result = handler(parse_command("/pr task-push-001"), None)
    assert "❌" in result
    assert "gh_pr_create_failed" in result or "PR" in result


def test_pr_handler_push_failure(tmp_path):
    """If the idempotent push fails BEFORE gh, surface that, don't run gh."""
    from core.sandbox_workspace import (
        SandboxConfig,
        SandboxWorkspace,
        _RunResult,
        _SubprocessRunner,
    )

    class _PushFailRunner(_SubprocessRunner):
        def __init__(self):
            self.calls = []

        def run(self, cmd, cwd, env, timeout):
            self.calls.append({"cmd": cmd})
            if cmd[0] == "git":
                return _RunResult(returncode=1, stdout="", stderr="rejected")
            return _RunResult(returncode=0, stdout="", stderr="")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    cfg = SandboxConfig(main_repo_path=repo, worktree_root=tmp_path / "wt")
    runner = _PushFailRunner()
    sandbox = SandboxWorkspace(cfg, runner=runner)
    history = _make_push_summary()
    handler = make_pr_handler(sandbox=sandbox, task_history=history)
    result = handler(parse_command("/pr task-push-001"), None)
    assert "❌" in result
    assert "Push" in result or "push" in result.lower()
    # gh must NOT have been called when push failed
    assert all(c["cmd"][0] != "gh" for c in runner.calls)


def test_build_command_registry_with_sandbox_wires_pr(tmp_path):
    sandbox, _ = _make_sandbox_for_pr(
        tmp_path,
        gh_returns=(0, "https://github.com/x/y/pull/1\n", ""),
    )
    history = _make_push_summary()
    personas = default_registry()
    reg = build_command_registry(personas, sandbox=sandbox, task_history=history)
    result = reg.dispatch(parse_command("/pr task-push-001"))
    assert "🪄" in result or "Draft PR" in result
