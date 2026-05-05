"""
core/bot_runner.py

Step 14a Module 7: testable builders + default command handlers used by
scripts/run_telegram_bot.py to assemble a working TelegramBridge from
environment configuration.

This module deliberately contains NO python-telegram-bot import — that
dependency lives only in scripts/run_telegram_bot.py. Everything here is
unit-testable without networking.

CONTRACTS:
1. Each builder is a pure function: takes typed inputs, returns a
   constructed object. Validation surfaces as ValueError early.
2. build_*_from_env(env) helpers accept an explicit env mapping (not
   os.environ directly) so tests can pass dicts.
3. Default command handlers do not require Orchestrator integration
   (deferred to Module 7b). They return either real data (e.g. /help,
   /agents) or a clear "будет в 7b" placeholder so users see what's
   wired and what's coming.
4. make_simple_task_handler returns a BridgeReply ack from the Менеджер;
   this is the MVP behaviour when the full pipeline is not configured.
5. parse_owner_chat_ids accepts comma-separated env value, strips spaces,
   rejects non-int/empty/negative, returns frozenset[int].
6. build_dispatcher_from_env returns LLMDispatcher if OPENROUTER_API_KEY
   is set, else None.
7. build_real_task_handler_from_env assembles the full
   BackgroundTaskRunner + SandboxWorkspace + LLMDispatcher pipeline when
   OPENROUTER_API_KEY and REPO_PATH are both present in env.  Returns
   None (silently) if any required env var is missing or any setup error
   occurs (invalid path, OSError, TypeError, ValueError from subsystems)
   — callers fall back to make_simple_task_handler in that case.
8. build_bridge_from_env tries build_real_task_handler_from_env first;
   falls back to make_simple_task_handler when the full stack is absent.
"""

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from core.agent_personas import PersonaRegistry, default_registry
from core.background_runner import BackgroundTaskRunner
from core.bot_commands import (
    BotCommand,
    CommandHandler,
    CommandName,
    CommandRegistry,
    format_help_text,
    parse_budget_amount,
)
from core.confirmation_gate import DEFAULT_COST_THRESHOLD_USD, ConfirmationGate
from core.dispatcher_agents import build_dispatcher_agent_registry_factory
from core.llm_dispatcher import LLMDispatcher
from core.model_tier import default_registry as default_tier_registry
from core.real_task_handler import RealTaskHandlerConfig, make_real_task_handler
from core.sandbox_workspace import SandboxConfig, SandboxWorkspace
from core.task_history import TaskHistory
from core.telegram_bridge import (
    BridgeReply,
    IncomingMessage,
    TaskHandler,
    TelegramBridge,
)
from core.tier_session import TierSessionStore, format_tier_summary
from core.vision_client import VisionClient
from core.whisper_client import WhisperClient

# ---------------------------------------------------------------------------
# env parsing
# ---------------------------------------------------------------------------


def parse_owner_chat_ids(raw: str) -> frozenset[int]:
    """Parse 'TELEGRAM_OWNER_CHAT_ID' env value into a frozenset.

    Accepts a single id ('123') or comma-separated list ('123, 456').
    Rejects empty, non-int, or non-positive ids.
    """
    if not isinstance(raw, str):
        raise ValueError("owner_chat_id_must_be_string")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ValueError("empty_owner_chat_id")
    out: set[int] = set()
    for p in parts:
        try:
            value = int(p)
        except ValueError as exc:
            raise ValueError(f"invalid_owner_chat_id:{p}") from exc
        if value <= 0:
            raise ValueError(f"non_positive_owner_chat_id:{value}")
        out.add(value)
    return frozenset(out)


def get_required_env(env: Mapping[str, str], key: str) -> str:
    """Fetch a non-empty env value or raise."""
    if not isinstance(env, Mapping):
        raise ValueError("env_must_be_mapping")
    if not isinstance(key, str) or not key:
        raise ValueError("empty_env_key")
    value = env.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing_env:{key}")
    return value.strip()


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def build_whisper_client(env: Mapping[str, str]) -> WhisperClient | None:
    """Returns WhisperClient if OPENAI_API_KEY is set, else None.

    None signals to TelegramBridge: voice messages are not supported in
    this run.
    """
    key = env.get("OPENAI_API_KEY")
    if not isinstance(key, str) or not key.strip():
        return None
    return WhisperClient(api_key=key.strip())


def build_vision_client(env: Mapping[str, str]) -> VisionClient | None:
    """Returns VisionClient if OPENROUTER_API_KEY is set, else None."""
    key = env.get("OPENROUTER_API_KEY")
    if not isinstance(key, str) or not key.strip():
        return None
    return VisionClient(api_key=key.strip())


def build_dispatcher_from_env(env: Mapping[str, str]) -> LLMDispatcher | None:
    """Return LLMDispatcher if OPENROUTER_API_KEY is set in env, else None.

    Raises ValueError for non-Mapping env (same contract as get_required_env).
    Returns None when the key is absent or empty — callers decide the fallback.
    """
    if not isinstance(env, Mapping):
        raise ValueError("env_must_be_mapping")
    key = env.get("OPENROUTER_API_KEY")
    if not isinstance(key, str) or not key.strip():
        return None
    return LLMDispatcher(api_key=key.strip())


def _try_build_sandbox(env: Mapping[str, str]) -> SandboxWorkspace | None:
    """Build SandboxWorkspace from env-vars, or return None on any failure.

    Reads REPO_PATH (required) and WORKTREE_ROOT (optional). Returns None
    when REPO_PATH is missing/empty, the path doesn't exist, isn't a git
    repo, or any subsystem raises ValueError/TypeError/OSError. No threads
    or background resources are created — safe to call as a probe.
    """
    repo_path_raw = env.get("REPO_PATH", "").strip()
    if not repo_path_raw:
        return None
    try:
        repo_path = Path(repo_path_raw)
        worktree_raw = env.get("WORKTREE_ROOT", "").strip()
        worktree_root = Path(worktree_raw) if worktree_raw else None

        sandbox_cfg_kwargs: dict = {"main_repo_path": repo_path}
        if worktree_root is not None:
            sandbox_cfg_kwargs["worktree_root"] = worktree_root

        return SandboxWorkspace(SandboxConfig(**sandbox_cfg_kwargs))
    except (ValueError, TypeError, OSError):
        return None


def _real_pipeline_eligible(env: Mapping[str, str]) -> bool:
    """True iff `build_real_task_handler_from_env` would return a real handler.

    Validates everything except runner/factory creation: API key present
    AND sandbox would be successfully built. Used by build_bridge_from_env
    to decide whether to require send_progress_callable.
    """
    if not isinstance(env, Mapping):
        return False
    if not env.get("OPENROUTER_API_KEY", "").strip():
        return False
    return _try_build_sandbox(env) is not None


def build_real_task_handler_from_env(
    env: Mapping[str, str],
    *,
    tier_store: TierSessionStore,
    send_progress: Callable[[int, str], None],
    sandbox: SandboxWorkspace | None = None,
    task_history: TaskHistory | None = None,
    runner: BackgroundTaskRunner | None = None,
) -> TaskHandler | None:
    """Assemble the full pipeline TaskHandler from env when possible.

    Required env vars:
      OPENROUTER_API_KEY — passed to LLMDispatcher
      REPO_PATH          — path to the main git repository (must exist + have .git)

    Optional env vars:
      WORKTREE_ROOT      — where worktrees are placed (default: tmp dir)

    Optional kwargs:
      sandbox       — pre-built SandboxWorkspace (built from env if None).
      task_history  — shared TaskHistory for /push and /log commands; each
                      completed task summary is recorded in on_complete.
      runner        — pre-built BackgroundTaskRunner (created internally if
                      None). Pass the same runner that was given to
                      build_command_registry so /stop can cancel real tasks.
                      When a caller-owned runner is passed, this function
                      will NOT shut it down on error (the caller owns it).

    Returns None (silently) if OPENROUTER_API_KEY or REPO_PATH are absent,
    or if any setup error occurs (invalid path, OSError, TypeError, ValueError
    from subsystems). Callers fall back to make_simple_task_handler in that case.

    Raises ValueError for non-Mapping env or non-TierSessionStore tier_store.
    """
    if not isinstance(env, Mapping):
        raise ValueError("env_must_be_mapping")
    if not isinstance(tier_store, TierSessionStore):
        raise ValueError(f"invalid_tier_store:{type(tier_store).__name__}")
    if not callable(send_progress):
        raise ValueError("send_progress_not_callable")

    dispatcher = build_dispatcher_from_env(env)
    if dispatcher is None:
        return None

    _sandbox = sandbox if sandbox is not None else _try_build_sandbox(env)
    if _sandbox is None:
        return None

    _runner_owned = runner is None
    _runner = runner if runner is not None else BackgroundTaskRunner()
    try:
        factory = build_dispatcher_agent_registry_factory(dispatcher)
        return make_real_task_handler(
            runner=_runner,
            sandbox=_sandbox,
            tier_store=tier_store,
            send_progress=send_progress,
            agent_registry_factory=factory,
            config=RealTaskHandlerConfig(),
            task_history=task_history,
        )
    except (ValueError, TypeError, OSError):
        if _runner_owned:
            _runner.shutdown(wait=False)
        return None


def build_confirmation_gate(env: Mapping[str, str]) -> ConfirmationGate:
    """Optional override of cost threshold via env BOT_COST_THRESHOLD_USD."""
    raw = env.get("BOT_COST_THRESHOLD_USD", "").strip()
    if raw:
        try:
            threshold = float(raw)
        except ValueError as exc:
            raise ValueError(
                f"invalid_BOT_COST_THRESHOLD_USD:{raw}"
            ) from exc
    else:
        threshold = DEFAULT_COST_THRESHOLD_USD
    return ConfirmationGate(cost_threshold_usd=threshold)


# ---------------------------------------------------------------------------
# default command handlers
# ---------------------------------------------------------------------------


def make_help_handler(registered: tuple[CommandName, ...]) -> CommandHandler:
    """Returns a /help handler that lists the given registered commands."""
    if not isinstance(registered, tuple):
        raise ValueError("registered_must_be_tuple")
    text = format_help_text(registered=registered)

    def _handle(_cmd: BotCommand, _ctx: Any) -> str:
        return text

    return _handle


def make_projects_handler(active_project: str = "ai-dev-team") -> CommandHandler:
    if not isinstance(active_project, str) or not active_project.strip():
        raise ValueError("empty_active_project")

    def _handle(_cmd: BotCommand, _ctx: Any) -> str:
        return (
            f"📋 Проекты\n"
            f"\n"
            f"▸ Активный: {active_project}\n"
            f"\n"
            f"🔜 Несколько проектов одновременно — в Модуле 7b."
        )

    return _handle


def make_switch_handler() -> CommandHandler:
    def _handle(cmd: BotCommand, _ctx: Any) -> str:
        positional = cmd.positional_args()
        if not positional:
            return (
                "🔄 Переключение проекта\n"
                "\n"
                "Использование:  /switch <имя_проекта>\n"
                "Пример:         /switch hedgekeeper-v2"
            )
        target = positional[0]
        return (
            f"🔄 Принял запрос на переключение → «{target}»\n"
            f"\n"
            f"🔜 Реальное переключение появится в Модуле 7b "
            f"(подключение AdapterRegistry)."
        )

    return _handle


class _BudgetState:
    """In-memory budget store. Replaced by persistent storage in 7b."""

    def __init__(self, initial_usd: float) -> None:
        self.budget_usd = float(initial_usd)


def make_budget_handler(state: _BudgetState) -> CommandHandler:
    if not isinstance(state, _BudgetState):
        raise ValueError("invalid_budget_state")

    def _handle(cmd: BotCommand, _ctx: Any) -> str:
        try:
            new_value = parse_budget_amount(cmd.args)
        except ValueError as exc:
            return (
                f"⚠️ Не удалось разобрать сумму: {exc}\n"
                f"\n"
                f"Примеры:  /budget 5    /budget 5.50    /budget 5$"
            )
        if new_value is None:
            return (
                f"💰 Текущий бюджет\n"
                f"\n"
                f"▸ ${state.budget_usd:.2f}\n"
                f"\n"
                f"Чтобы изменить:  /budget <сумма>"
            )
        state.budget_usd = float(new_value)
        return (
            f"✅ Бюджет обновлён\n"
            f"\n"
            f"▸ ${state.budget_usd:.2f}"
        )

    return _handle


def make_agents_handler(personas: PersonaRegistry) -> CommandHandler:
    """Lists all agents with thematic emojis and clean visual hierarchy.

    Performance metrics will be added in 7b once Observability streams here.
    """
    if not isinstance(personas, PersonaRegistry):
        raise ValueError("invalid_personas")

    def _handle(_cmd: BotCommand, _ctx: Any) -> str:
        lines: list[str] = ["👥 Состав команды", ""]
        # Stable order matching the FSM pipeline flow:
        # Planner → PM → Architect → Programmer → Reviewer → Tester → QA → Fixer
        flow_order = (
            "planning_agent",
            "pm_agent",
            "architect_agent",
            "writer_agent",
            "reviewer_agent",
            "tester_agent",
            "qa_agent",
            "fixer_agent",
        )
        for role in flow_order:
            if role not in personas:
                continue
            p = personas.for_role(role)
            icon = p.emoji or "•"
            traits = " · ".join(p.voice_traits[:2])
            lines.append(f"{icon} {p.qualified_name} · {p.seniority}")
            lines.append(f"   {traits}")
            lines.append("")
        # Trim trailing blank line
        while lines and lines[-1] == "":
            lines.pop()
        lines.append("")
        lines.append("📈 Метрики p50/p95/error rate — в Модуле 7b.")
        return "\n".join(lines)

    return _handle


def make_push_handler(
    sandbox: SandboxWorkspace | None = None,
    task_history: TaskHistory | None = None,
) -> CommandHandler:
    """Returns a /push <task_id> handler.

    When sandbox and task_history are provided (real pipeline active):
      - Looks up task_id in history to find branch and commit_sha.
      - Calls sandbox.push_branch_from_main(branch) — works after worktree
        release because it runs `git push` from the main repo.
      - Refuses to push if the task never reached SUCCESS (no commit_sha).

    When either is None (simple-stub pipeline):
      - Returns a helpful message explaining how to enable the feature.
    """
    if sandbox is not None and not isinstance(sandbox, SandboxWorkspace):
        raise ValueError(f"invalid_sandbox:{type(sandbox).__name__}")
    if task_history is not None and not isinstance(task_history, TaskHistory):
        raise ValueError(f"invalid_task_history:{type(task_history).__name__}")

    _sandbox = sandbox
    _history = task_history

    def _handle(cmd: BotCommand, _ctx: Any) -> str:
        if _sandbox is None or _history is None:
            return (
                "🚀 /push <task_id>\n"
                "\n"
                "🔜 Доступно только в полном режиме.\n"
                "Настрой OPENROUTER_API_KEY + REPO_PATH чтобы включить."
            )
        positional = cmd.positional_args()
        if not positional:
            return (
                "⚠️ Укажи task_id:\n"
                "\n"
                "  /push <task_id>\n"
                "\n"
                "Например: /push task-1714829400-7a3b9c"
            )
        task_id = positional[0]
        summary = _history.get(task_id)
        if summary is None:
            return (
                f"⚠️ task_id `{task_id}` не найден в истории.\n"
                "\n"
                "Задача ещё выполняется или id указан неверно."
            )
        if summary.commit_sha is None:
            return (
                f"⚠️ Задача `{task_id}` не достигла SUCCESS — нечего пушить.\n"
                "\n"
                f"  state   `{summary.final_state}`\n"
                f"  reason  `{summary.failure_reason or '?'}`"
            )
        try:
            _sandbox.push_branch_from_main(summary.branch)
        except Exception as exc:
            return (
                f"❌ Push не удался\n"
                "\n"
                f"  branch  `{summary.branch}`\n"
                f"  ошибка  {type(exc).__name__}: {str(exc)[:200]}"
            )
        return (
            f"🚀 Запушено!\n"
            "\n"
            f"  task-id `{task_id}`\n"
            f"  branch  `{summary.branch}`\n"
            f"  commit  `{summary.commit_sha[:8]}`"
        )

    return _handle


def make_log_handler() -> CommandHandler:
    def _handle(_cmd: BotCommand, _ctx: Any) -> str:
        return (
            "📜 Лог последней задачи\n"
            "\n"
            "▸ Файл: .pipeline_log.jsonl (корень проекта)\n"
            "\n"
            "🔜 Стриминг в чат и /log <task_id> — в Модуле 7b."
        )

    return _handle


def make_stop_handler(
    runner: BackgroundTaskRunner | None = None,
) -> CommandHandler:
    """Returns a /stop handler.

    When *runner* is provided (real pipeline is active), calls runner.cancel():
      - cancel() returns True  → active task was signalled → "Задача остановлена"
      - cancel() returns False → nothing running            → "Ничего не выполняется"

    When *runner* is None (pipeline not configured), returns an informative stub.
    """
    if runner is not None and not isinstance(runner, BackgroundTaskRunner):
        raise ValueError(f"invalid_runner:{type(runner).__name__}")

    def _handle(_cmd: BotCommand, _ctx: Any) -> str:
        if runner is None:
            return (
                "⏹ Остановка недоступна\n"
                "\n"
                "Пайплайн не настроен — задачи не выполняются в фоне."
            )
        cancelled = runner.cancel()
        if cancelled:
            return (
                "⏹ Задача остановлена\n"
                "\n"
                "Запрос на отмену отправлен воркеру. "
                "Дожидайся сообщения о завершении."
            )
        return "⏹ Сейчас ничего не выполняется"

    return _handle


def make_retry_handler() -> CommandHandler:
    def _handle(cmd: BotCommand, _ctx: Any) -> str:
        if cmd.has_flag("different"):
            return (
                "🔁 Повтор с другой моделью/стратегией\n"
                "\n"
                "🔜 Появится в Модуле 7b."
            )
        return (
            "🔁 Повтор последней задачи\n"
            "\n"
            "🔜 Появится в Модуле 7b."
        )

    return _handle


def make_tier_handler(store: TierSessionStore) -> CommandHandler:
    """Returns a /tier handler.

    Subcommands:
      - /tier                → show current tier + summary of available tiers
      - /tier set <name>     → make <name> the active tier for this chat
      - /tier reset          → forget chat's choice (next task will ask again)

    `ctx` is expected to be the IncomingMessage (the bridge passes msg as ctx
    on dispatch). We need ctx.chat_id to scope tier state per chat.
    """
    if not isinstance(store, TierSessionStore):
        raise ValueError("invalid_tier_store")

    available = ", ".join(store.registry.list_names())
    no_chat_hint = (
        "⚠️ Не удалось определить чат для /tier.\n"
        "\n"
        "Попробуйте ещё раз — это внутренняя ошибка моста."
    )

    def _handle(cmd: BotCommand, ctx: Any) -> str:
        chat_id = getattr(ctx, "chat_id", None)
        if (
            chat_id is None
            or isinstance(chat_id, bool)
            or not isinstance(chat_id, int)
            or chat_id <= 0
        ):
            return no_chat_hint

        positional = cmd.positional_args()

        # /tier with no args → show summary
        if not positional:
            active = store.active_tier_name(chat_id)
            return format_tier_summary(store.registry, active_name=active)

        action = positional[0].lower()

        if action == "set":
            if len(positional) < 2:
                return (
                    f"💼 Использование:\n"
                    f"\n"
                    f"  /tier set <имя_тарифа>\n"
                    f"\n"
                    f"Доступно: {available}"
                )
            target = positional[1]
            try:
                store.set_active(chat_id, target)
            except KeyError:
                return (
                    f"⚠️ Неизвестный тариф: «{target}»\n"
                    f"\n"
                    f"Доступно: {available}\n"
                    f"\n"
                    f"Подробнее:  /tier"
                )
            return (
                f"✅ Тариф для этого чата: {target}\n"
                f"\n"
                f"Сменить:  /tier set <имя>\n"
                f"Сбросить: /tier reset"
            )

        if action == "reset":
            store.reset(chat_id)
            return (
                "🔁 Тариф сброшен.\n"
                "\n"
                "Перед следующей задачей бот спросит, какой использовать."
            )

        return (
            f"⚠️ Не понял подкоманду: «{action}»\n"
            f"\n"
            f"Использование:\n"
            f"  /tier              — показать текущий тариф\n"
            f"  /tier set <имя>    — выбрать тариф\n"
            f"  /tier reset        — сбросить выбор\n"
            f"\n"
            f"Доступно: {available}"
        )

    return _handle


def build_command_registry(
    personas: PersonaRegistry,
    *,
    initial_budget_usd: float = 10.0,
    active_project: str = "ai-dev-team",
    tier_store: TierSessionStore | None = None,
    sandbox: SandboxWorkspace | None = None,
    task_history: TaskHistory | None = None,
    runner: BackgroundTaskRunner | None = None,
) -> CommandRegistry:
    """Build a CommandRegistry pre-populated with all 10 default handlers.

    If tier_store is None, a fresh store backed by default_tier_registry()
    is created. Pass an explicit store when the bridge wants to share tier
    state with other components (e.g. real_task_handler).

    sandbox + task_history are optional: when both are provided the /push
    handler is fully wired (real push to GitHub). When either is None,
    /push returns a "настрой REPO_PATH" stub.

    runner is optional: when provided, /stop calls runner.cancel() for real
    cooperative cancellation. When None, /stop returns an informative stub.
    """
    if not isinstance(personas, PersonaRegistry):
        raise ValueError("invalid_personas")
    if not isinstance(initial_budget_usd, (int, float)) or initial_budget_usd < 0:
        raise ValueError("invalid_initial_budget")
    if tier_store is None:
        tier_store = TierSessionStore(default_tier_registry())
    elif not isinstance(tier_store, TierSessionStore):
        raise ValueError("invalid_tier_store")

    reg = CommandRegistry()
    budget_state = _BudgetState(initial_usd=initial_budget_usd)

    # Register in enum order so /help output is consistent.
    reg.register(CommandName.PROJECTS, make_projects_handler(active_project))
    reg.register(CommandName.SWITCH, make_switch_handler())
    reg.register(CommandName.BUDGET, make_budget_handler(budget_state))
    reg.register(CommandName.AGENTS, make_agents_handler(personas))
    reg.register(CommandName.TIER, make_tier_handler(tier_store))
    reg.register(CommandName.LOG, make_log_handler())
    reg.register(CommandName.STOP, make_stop_handler(runner))
    reg.register(CommandName.RETRY, make_retry_handler())
    reg.register(CommandName.PUSH, make_push_handler(sandbox, task_history))
    # /help is registered LAST so it can list everything else.
    reg.register(
        CommandName.HELP,
        make_help_handler((*reg.list_registered(), CommandName.HELP)),
    )

    return reg


# ---------------------------------------------------------------------------
# task handler
# ---------------------------------------------------------------------------


def make_simple_task_handler(_personas: PersonaRegistry) -> TaskHandler:
    """MVP task handler. Acknowledges receipt from the Менеджер persona.

    Real Orchestrator integration (text → run pipeline → stream agents)
    lives in Module 7b.
    """

    def _handle(text: str, _msg: IncomingMessage) -> BridgeReply:
        excerpt = text if len(text) <= 200 else text[:200] + " …[обрезано]"
        return BridgeReply(
            persona_role="pm_agent",
            body=(
                f"👋 Принял задачу\n"
                f"\n"
                f"«{excerpt}»\n"
                f"\n"
                f"🔜 Реальное выполнение через orchestrator — в Модуле 7b. "
                f"Сейчас это MVP-каркас: проверяем транспорт, "
                f"распознавание и подписи персон."
            ),
        )

    return _handle


# ---------------------------------------------------------------------------
# top-level builder
# ---------------------------------------------------------------------------


def build_bridge_from_env(
    env: Mapping[str, str] | None = None,
    *,
    send_callable=None,
    send_progress_callable: Callable[[int, str], None] | None = None,
) -> TelegramBridge:
    """Top-level builder. Reads env, assembles all components, returns
    a ready TelegramBridge.

    `send_callable` must be supplied at call time (it's transport-specific
    — wraps PTB bot.send_message in scripts/run_telegram_bot.py).

    `send_progress_callable` is optional: a (chat_id: int, text: str) -> None
    callback used to stream pipeline progress back to Telegram. When absent,
    progress events are silently swallowed. In production, pass something like
    `lambda cid, txt: bot.send_message(chat_id=cid, text=txt)`.

    Pipeline selection:
      - If OPENROUTER_API_KEY + REPO_PATH are set → full LLMDispatcher pipeline
        via make_real_task_handler (dispatcher_agents + BackgroundTaskRunner +
        SandboxWorkspace). Progress routed through send_progress_callable.
      - Otherwise → make_simple_task_handler (MVP stub, always available).

    Raises ValueError if required env vars are missing.
    """
    if env is None:
        env = dict(os.environ)
    if send_callable is None or not callable(send_callable):
        raise ValueError("send_callable_required")

    owner_raw = get_required_env(env, "TELEGRAM_OWNER_CHAT_ID")
    owner_chat_ids = parse_owner_chat_ids(owner_raw)

    personas = default_registry()
    gate = build_confirmation_gate(env)
    whisper = build_whisper_client(env)
    vision = build_vision_client(env)
    tier_store = TierSessionStore(default_tier_registry())

    # Build sandbox once so it can be shared between the task handler
    # (acquire/release worktrees) and the /push command handler (push_branch_from_main).
    sandbox = _try_build_sandbox(env)
    task_history = TaskHistory() if sandbox is not None else None

    # Build a single BackgroundTaskRunner so /stop can cancel the active task.
    # The runner is passed to both build_command_registry (for /stop) and
    # build_real_task_handler_from_env (for task execution).  When the real
    # pipeline is not configured the runner is still created but remains idle;
    # it's lightweight and runner.cancel() safely returns False when idle.
    runner = BackgroundTaskRunner()

    commands = build_command_registry(
        personas,
        tier_store=tier_store,
        sandbox=sandbox,
        task_history=task_history,
        runner=runner,
    )

    # Attempt to build the full dispatcher-backed pipeline; fall back to stub.
    # Guard: if the real pipeline would actually activate (API key present AND
    # repo path is a valid git repo) but no progress callback was provided, we
    # must raise rather than silently swallow 30+ seconds of events. We use a
    # full eligibility check (not just env-var presence) so misconfigured paths
    # don't trigger this guard — they'll fall back to the simple handler.
    if _real_pipeline_eligible(env) and send_progress_callable is None:
        raise ValueError("send_progress_required_for_real_pipeline")

    _send_progress: Callable[[int, str], None] = (
        send_progress_callable if send_progress_callable is not None
        else lambda _cid, _txt: None
    )
    task_handler: TaskHandler = build_real_task_handler_from_env(
        env,
        tier_store=tier_store,
        send_progress=_send_progress,
        sandbox=sandbox,
        task_history=task_history,
        runner=runner,
    ) or make_simple_task_handler(personas)

    return TelegramBridge(
        owner_chat_ids=owner_chat_ids,
        send=send_callable,
        whisper=whisper,
        vision=vision,
        personas=personas,
        gate=gate,
        commands=commands,
        task_handler=task_handler,
    )
