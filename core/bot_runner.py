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
   this is the MVP behaviour until real orchestrator integration lands.
5. parse_owner_chat_ids accepts comma-separated env value, strips spaces,
   rejects non-int/empty/negative, returns frozenset[int].
"""

import os
from collections.abc import Mapping
from typing import Any

from core.agent_personas import PersonaRegistry, default_registry
from core.bot_commands import (
    BotCommand,
    CommandHandler,
    CommandName,
    CommandRegistry,
    format_help_text,
    parse_budget_amount,
)
from core.confirmation_gate import DEFAULT_COST_THRESHOLD_USD, ConfirmationGate
from core.telegram_bridge import (
    BridgeReply,
    IncomingMessage,
    TaskHandler,
    TelegramBridge,
)
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
            f"Активный проект: {active_project}.\n"
            f"Подключение AdapterRegistry с несколькими проектами — "
            f"в Модуле 7b."
        )

    return _handle


def make_switch_handler() -> CommandHandler:
    def _handle(cmd: BotCommand, _ctx: Any) -> str:
        positional = cmd.positional_args()
        if not positional:
            return "Используйте: /switch <имя_проекта>"
        target = positional[0]
        return (
            f"Переключение на проект «{target}» будет реализовано "
            f"в Модуле 7b (нужен AdapterRegistry с зарегистрированными "
            f"проектами)."
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
            return f"Не удалось разобрать сумму: {exc}"
        if new_value is None:
            return f"Текущий бюджет: ${state.budget_usd:.2f}"
        state.budget_usd = float(new_value)
        return f"Бюджет установлен: ${state.budget_usd:.2f}"

    return _handle


def make_agents_handler(personas: PersonaRegistry) -> CommandHandler:
    """Lists all agents in a Russian-language table.

    Performance metrics will be added in 7b once Observability streams here.
    """
    if not isinstance(personas, PersonaRegistry):
        raise ValueError("invalid_personas")

    def _handle(_cmd: BotCommand, _ctx: Any) -> str:
        lines = ["Состав команды:"]
        for p in personas.all():
            lines.append(
                f"  • {p.callsign} ({p.title}) — {p.seniority}, "
                f"{', '.join(p.voice_traits[:2])}"
            )
        lines.append("")
        lines.append(
            "Метрики производительности (p50/p95, error rate) — "
            "в Модуле 7b после интеграции с Observability."
        )
        return "\n".join(lines)

    return _handle


def make_log_handler() -> CommandHandler:
    def _handle(_cmd: BotCommand, _ctx: Any) -> str:
        return (
            "Логи последней задачи: смотрите файл .pipeline_log.jsonl "
            "в корне проекта.\n"
            "Стриминг в чат и /log <task_id> — в Модуле 7b."
        )

    return _handle


def make_stop_handler() -> CommandHandler:
    def _handle(_cmd: BotCommand, _ctx: Any) -> str:
        return (
            "Команда /stop принята. Остановка in-flight задач "
            "будет реализована в Модуле 7b (нужен флаг в orchestrator)."
        )

    return _handle


def make_retry_handler() -> CommandHandler:
    def _handle(cmd: BotCommand, _ctx: Any) -> str:
        if cmd.has_flag("different"):
            return (
                "Повтор последней задачи с другой моделью/стратегией — "
                "в Модуле 7b."
            )
        return "Повтор последней задачи — в Модуле 7b."

    return _handle


def build_command_registry(
    personas: PersonaRegistry,
    *,
    initial_budget_usd: float = 10.0,
    active_project: str = "ai-dev-team",
) -> CommandRegistry:
    """Build a CommandRegistry pre-populated with all 8 default handlers."""
    if not isinstance(personas, PersonaRegistry):
        raise ValueError("invalid_personas")
    if not isinstance(initial_budget_usd, (int, float)) or initial_budget_usd < 0:
        raise ValueError("invalid_initial_budget")

    reg = CommandRegistry()
    budget_state = _BudgetState(initial_usd=initial_budget_usd)

    # Register in enum order so /help output is consistent.
    reg.register(CommandName.PROJECTS, make_projects_handler(active_project))
    reg.register(CommandName.SWITCH, make_switch_handler())
    reg.register(CommandName.BUDGET, make_budget_handler(budget_state))
    reg.register(CommandName.AGENTS, make_agents_handler(personas))
    reg.register(CommandName.LOG, make_log_handler())
    reg.register(CommandName.STOP, make_stop_handler())
    reg.register(CommandName.RETRY, make_retry_handler())
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
        excerpt = text if len(text) <= 200 else text[:200] + "...[обрезано]"
        return BridgeReply(
            persona_role="pm_agent",
            body=(
                f"Получил задачу: «{excerpt}».\n"
                f"Команда зарегистрирована в системе. Реальное выполнение "
                f"через orchestrator подключим в Модуле 7b."
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
) -> TelegramBridge:
    """Top-level builder. Reads env, assembles all components, returns
    a ready TelegramBridge.

    `send_callable` must be supplied at call time (it's transport-specific
    — wraps PTB bot.send_message in scripts/run_telegram_bot.py).

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
    commands = build_command_registry(personas)
    task_handler = make_simple_task_handler(personas)

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
