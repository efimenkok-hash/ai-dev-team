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
import threading
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
from core.observability import JsonLinesSink, Observability
from core.real_task_handler import RealTaskHandlerConfig, make_real_task_handler
from core.sandbox_workspace import SandboxConfig, SandboxWorkspace
from core.state_db import StateDB
from core.task_history import TaskHistory
from core.telegram_bridge import (
    BridgeReply,
    IncomingMessage,
    TaskHandler,
    TelegramBridge,
)
from core.tier_session import (
    TierSessionStore,
    format_tier_summary,
    migrate_legacy_tier_sessions_json,
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


def build_dispatcher_from_env(
    env: Mapping[str, str],
    *,
    observability: Observability | None = None,
) -> LLMDispatcher | None:
    """Return LLMDispatcher if OPENROUTER_API_KEY is set in env, else None.

    Raises ValueError for non-Mapping env (same contract as get_required_env).
    Returns None when the key is absent or empty — callers decide the fallback.
    """
    if not isinstance(env, Mapping):
        raise ValueError("env_must_be_mapping")
    key = env.get("OPENROUTER_API_KEY")
    if not isinstance(key, str) or not key.strip():
        return None
    return LLMDispatcher(api_key=key.strip(), observability=observability)


def cleanup_orphan_worktrees_from_env(env: Mapping[str, str]) -> int:
    """Sweep stale worktree directories at bot startup. Returns count removed.

    A worktree is "orphan" when its directory exists under WORKTREE_ROOT
    but git has no record of it (e.g. previous bot crashed mid-task and
    left a directory behind, or the user rebooted). cleanup_orphans()
    removes the directory and runs `git worktree prune`.

    Returns:
      - 0 if env doesn't configure a real pipeline (no sandbox to query)
      - 0 if there were no orphans
      - N >= 1 if N orphan directories were removed

    Failures are swallowed so startup never crashes; errors are returned
    as 0. Caller logs the count.
    """
    if not isinstance(env, Mapping):
        raise ValueError("env_must_be_mapping")
    sandbox = _try_build_sandbox(env)
    if sandbox is None:
        return 0
    try:
        return sandbox.cleanup_orphans()
    except Exception:
        return 0


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


def _build_observability(env: Mapping[str, str]) -> Observability | None:
    """Return Observability(JsonLinesSink(path)) if OBS_LOG_PATH is set in env.

    Returns None silently if the var is absent, empty, or the path cannot be
    opened (e.g. directory does not exist and cannot be created).
    """
    path = env.get("OBS_LOG_PATH", "").strip()
    if not path:
        return None
    try:
        return Observability(sink=JsonLinesSink(path))
    except (OSError, ValueError):
        return None


def _resolve_state_db_path(env: Mapping[str, str]) -> Path:
    """Resolve SQLite persistence path with legacy-dir fallback."""
    if not isinstance(env, Mapping):
        raise ValueError("env_must_be_mapping")
    raw = env.get("STATE_DB_PATH", "").strip()
    if raw:
        return Path(raw).expanduser()
    legacy_dir = env.get("BOT_STATE_DIR", "").strip()
    if legacy_dir:
        return (Path(legacy_dir) / "state.db").expanduser()
    return Path("~/.ai-dev-team/state.db").expanduser()


def _resolve_legacy_tier_sessions_path(env: Mapping[str, str]) -> Path | None:
    if not isinstance(env, Mapping):
        raise ValueError("env_must_be_mapping")
    legacy_dir = env.get("BOT_STATE_DIR", "").strip()
    if not legacy_dir:
        return None
    return (Path(legacy_dir) / "tier_sessions.json").expanduser()


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

    obs = _build_observability(env)
    dispatcher = build_dispatcher_from_env(env, observability=obs)
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
            observability=obs,
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
    """Budget store with optional per-chat StateDB persistence.

    Without chat_id context, the store falls back to a process-local default.
    This keeps legacy tests and non-Telegram contexts working while the real
    bridge can persist per-chat overrides via IncomingMessage.chat_id.
    """

    def __init__(
        self,
        initial_usd: float,
        *,
        state_db: StateDB | None = None,
    ) -> None:
        if (
            isinstance(initial_usd, bool)
            or not isinstance(initial_usd, (int, float))
            or initial_usd < 0
        ):
            raise ValueError("invalid_initial_budget")
        if state_db is not None and not isinstance(state_db, StateDB):
            raise ValueError("invalid_state_db")
        self._lock = threading.Lock()
        self._default_budget_usd = float(initial_usd)
        self._state_db = state_db
        self._by_chat: dict[int, float] = {}

    @property
    def budget_usd(self) -> float:
        with self._lock:
            return self._default_budget_usd

    @budget_usd.setter
    def budget_usd(self, value: float) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value < 0
        ):
            raise ValueError("invalid_budget")
        with self._lock:
            self._default_budget_usd = float(value)

    @property
    def state_db(self) -> StateDB | None:
        return self._state_db

    def get_budget(self, chat_id: int | None = None) -> float:
        if chat_id is None:
            return self.budget_usd
        self._validate_chat_id(chat_id)
        with self._lock:
            cached = self._by_chat.get(chat_id)
            if cached is not None:
                return cached
        if self._state_db is not None:
            stored = self._state_db.get_budget(chat_id)
            if stored is not None:
                with self._lock:
                    self._by_chat[chat_id] = float(stored)
                return float(stored)
        return self.budget_usd

    def set_budget(self, usd: float, *, chat_id: int | None = None) -> float:
        if (
            isinstance(usd, bool)
            or not isinstance(usd, (int, float))
            or usd < 0
        ):
            raise ValueError("invalid_budget")
        amount = float(usd)
        if chat_id is None:
            self.budget_usd = amount
            return amount
        self._validate_chat_id(chat_id)
        with self._lock:
            self._by_chat[chat_id] = amount
        if self._state_db is not None:
            self._state_db.set_budget(chat_id, amount)
        return amount

    @staticmethod
    def _validate_chat_id(chat_id: int) -> None:
        if (
            isinstance(chat_id, bool)
            or not isinstance(chat_id, int)
            or chat_id <= 0
        ):
            raise ValueError(f"invalid_chat_id:{chat_id!r}")


def make_budget_handler(state: _BudgetState) -> CommandHandler:
    if not isinstance(state, _BudgetState):
        raise ValueError("invalid_budget_state")

    def _handle(cmd: BotCommand, _ctx: Any) -> str:
        chat_id = getattr(_ctx, "chat_id", None)
        scoped_chat_id = (
            chat_id
            if isinstance(chat_id, int) and not isinstance(chat_id, bool) and chat_id > 0
            else None
        )
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
                f"▸ ${state.get_budget(scoped_chat_id):.2f}\n"
                f"\n"
                f"Чтобы изменить:  /budget <сумма>"
            )
        updated = state.set_budget(float(new_value), chat_id=scoped_chat_id)
        return (
            f"✅ Бюджет обновлён\n"
            f"\n"
            f"▸ ${updated:.2f}"
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

    When *sandbox* AND *task_history* are provided (real pipeline active):
      - Validates task_id against _TASK_ID_RE (rejects shell-meta, path traversal).
      - Looks up task_id in TaskHistory — refuses if not found or commit_sha is None
        (task never reached SUCCESS, nothing meaningful to push).
      - Calls sandbox.push_named_branch(summary.branch) — works after worktree
        release because it runs `git push` from the main repo.
      - Returns ✅ on success with branch + short SHA, ❌ with reason on failure.

    When either is None (pipeline not configured):
      - Returns an informative stub explaining how to enable the feature.

    The TaskHistory guard prevents pushing branches of failed tasks that exist
    in main_repo but contain no new commits (they were created by acquire() and
    point to the same SHA as main).
    """
    if sandbox is not None and not isinstance(sandbox, SandboxWorkspace):
        raise ValueError(f"invalid_sandbox:{type(sandbox).__name__}")
    if task_history is not None and not isinstance(task_history, TaskHistory):
        raise ValueError(f"invalid_task_history:{type(task_history).__name__}")

    from core.sandbox_workspace import _TASK_ID_RE as _TID_RE

    def _handle(cmd: BotCommand, _ctx: Any) -> str:
        if sandbox is None or task_history is None:
            return (
                "🚀 /push <task_id>\n"
                "\n"
                "Push недоступен: настрой REPO_PATH чтобы включить."
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
        if not isinstance(task_id, str) or not _TID_RE.match(task_id):
            return (
                f"❌ Некорректный task_id: `{task_id[:80]}`\n"
                "\n"
                "task_id должен содержать только строчные буквы, цифры, "
                "дефис и подчёркивание."
            )
        # Guard: only push tasks that actually reached SUCCESS (have a commit).
        # Failed tasks still have a branch in main_repo (created by acquire()),
        # but it points to the same SHA as main — nothing meaningful to push.
        summary = task_history.get(task_id)
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
            sandbox.push_named_branch(summary.branch)
        except Exception as exc:
            return (
                f"❌ Не удалось запушить\n"
                "\n"
                f"  branch  `{summary.branch}`\n"
                f"  ошибка  {type(exc).__name__}: {str(exc)[:200]}"
            )
        return (
            f"✅ Запушено в GitHub\n"
            "\n"
            f"  task-id `{task_id}`\n"
            f"  branch  `{summary.branch}`\n"
            f"  commit  `{summary.commit_sha[:8]}`"
        )

    return _handle


def make_pr_handler(
    sandbox: SandboxWorkspace | None = None,
    task_history: TaskHistory | None = None,
) -> CommandHandler:
    """Returns a /pr <task_id> handler that creates a draft GitHub PR.

    Behaviour:
      - When sandbox + task_history are both wired (real pipeline active):
        * Validate task_id regex (rejects shell-meta, traversal, uppercase).
        * Look up summary in TaskHistory; require commit_sha is not None.
        * Push the branch (idempotent — safe if already pushed).
        * Run `gh pr create --draft` with title/body derived from the task.
        * Return PR URL on success.
      - When either is None: returns "настрой REPO_PATH" stub.

    The PR is created as a DRAFT so user can review before requesting review.
    """
    if sandbox is not None and not isinstance(sandbox, SandboxWorkspace):
        raise ValueError(f"invalid_sandbox:{type(sandbox).__name__}")
    if task_history is not None and not isinstance(task_history, TaskHistory):
        raise ValueError(f"invalid_task_history:{type(task_history).__name__}")

    from core.sandbox_workspace import _TASK_ID_RE as _TID_RE

    def _handle(cmd: BotCommand, _ctx: Any) -> str:
        if sandbox is None or task_history is None:
            return (
                "🪄 /pr <task_id>\n"
                "\n"
                "PR недоступен: настрой REPO_PATH чтобы включить полный pipeline.\n"
                "Также нужен `gh` CLI с авторизацией: `gh auth login`."
            )
        positional = cmd.positional_args()
        if not positional:
            return (
                "⚠️ Укажи task_id:\n"
                "\n"
                "  /pr <task_id>\n"
                "\n"
                "Например: /pr task-1714829400-7a3b9c"
            )
        task_id = positional[0]
        if not isinstance(task_id, str) or not _TID_RE.match(task_id):
            return (
                f"❌ Некорректный task_id: `{task_id[:80]}`\n"
                "\n"
                "task_id должен содержать только строчные буквы, цифры, "
                "дефис и подчёркивание."
            )
        summary = task_history.get(task_id)
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

        # 1. Idempotent push first — `gh pr create` requires the branch on remote.
        try:
            sandbox.push_named_branch(summary.branch)
        except Exception as exc:
            return (
                f"❌ Push перед PR не удался\n"
                "\n"
                f"  branch  `{summary.branch}`\n"
                f"  ошибка  {type(exc).__name__}: {str(exc)[:200]}"
            )

        # 2. Create the draft PR. Title from task_id+branch; body cites task summary.
        pr_title = f"AI Dev Team: {summary.task_id}"
        pr_body = (
            f"Автоматически сгенерированный PR от AI Dev Team.\n\n"
            f"- task-id: `{summary.task_id}`\n"
            f"- branch: `{summary.branch}`\n"
            f"- commit: `{summary.commit_sha}`\n"
            f"- тариф: `{summary.tier_name}`\n\n"
            f"Этот PR создан как **draft** — проверьте код и нажмите "
            f"\"Ready for review\" в GitHub когда будете готовы.\n"
        )
        try:
            url = sandbox.gh_pr_create(
                summary.branch,
                title=pr_title,
                body=pr_body,
            )
        except Exception as exc:
            from core.sandbox_workspace import SandboxError

            if isinstance(exc, SandboxError) and exc.code == "gh_not_found":
                return (
                    "❌ `gh` CLI не найден\n"
                    "\n"
                    "Установи GitHub CLI: https://cli.github.com\n"
                    "Затем авторизуйся:  `gh auth login`"
                )
            return (
                f"❌ Не удалось создать PR\n"
                "\n"
                f"  branch  `{summary.branch}`\n"
                f"  ошибка  {type(exc).__name__}: {str(exc)[:200]}\n"
                f"\n"
                f"Если это первый PR — проверь `gh auth status`."
            )
        return (
            f"🪄 Draft PR создан\n"
            "\n"
            f"  task-id  `{task_id}`\n"
            f"  branch   `{summary.branch}`\n"
            f"  commit   `{summary.commit_sha[:8]}`\n"
            f"\n"
            f"  {url}"
        )

    return _handle


def _format_task_summary(summary: Any) -> str:  # summary: TaskSummary
    """Render one TaskSummary as a human-readable Telegram message."""
    import time as _time

    from core.task_history import TaskSummary

    s: TaskSummary = summary
    state_icon = "✅" if s.final_state == "SUCCESS" else "❌"
    sha_short = s.commit_sha[:7] if s.commit_sha else "—"
    try:
        ts = _time.strftime("%Y-%m-%d %H:%M UTC", _time.gmtime(s.finished_at))
    except Exception:
        ts = "—"
    lines = [
        f"📜 Задача `{s.task_id}`",
        "",
        f"{state_icon} Статус:  {s.final_state}",
        f"🌿 Ветка:   {s.branch}",
        f"🔖 SHA:    {sha_short}",
        f"💼 Тариф:  {s.tier_name}",
        f"🕐 Время:  {ts}",
    ]
    if s.failure_reason:
        lines.append(f"⚠️  Причина: {s.failure_reason}")
    return "\n".join(lines)


def make_log_handler(
    task_history: "TaskHistory | None" = None,
) -> CommandHandler:
    """Returns a /log handler.

    Subcommands:
      /log              → list the 5 most recent completed tasks
      /log <task_id>    → show details for a specific task

    When *task_history* is None, returns an informative stub.
    """
    if task_history is not None:
        from core.task_history import TaskHistory as _TH

        if not isinstance(task_history, _TH):
            raise ValueError(f"invalid_task_history:{type(task_history).__name__}")

    def _handle(cmd: BotCommand, _ctx: Any) -> str:
        if task_history is None:
            return (
                "📜 Лог задач недоступен\n"
                "\n"
                "Пайплайн не настроен — история задач не ведётся."
            )

        positional = cmd.positional_args()

        # /log <task_id>
        if positional:
            task_id = positional[0]
            summary = task_history.get(task_id)
            if summary is None:
                return (
                    f"📜 Задача `{task_id}` не найдена\n"
                    "\n"
                    "Возможно, задача ещё выполняется или история была сброшена."
                )
            return _format_task_summary(summary)

        # /log — show 5 most recent
        recent = task_history.recent(5)
        if not recent:
            return "📜 История задач пуста — ещё ни одна задача не завершена."

        header = f"📜 Последние задачи ({len(recent)}):\n"
        rows = []
        for s in reversed(recent):  # newest first
            icon = "✅" if s.final_state == "SUCCESS" else "❌"
            sha = s.commit_sha[:7] if s.commit_sha else "—"
            rows.append(f"{icon} `{s.task_id}` · {s.branch} · {sha}")
        return header + "\n".join(rows) + "\n\n/log <task_id> — подробности"

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
    state_db: StateDB | None = None,
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

    task_history is also passed to /log: when provided, /log lists recent
    completed tasks and shows per-task details. When None, /log returns a stub.

    runner is optional: when provided, /stop calls runner.cancel() for real
    cooperative cancellation. When None, /stop returns an informative stub.
    """
    if not isinstance(personas, PersonaRegistry):
        raise ValueError("invalid_personas")
    if (
        isinstance(initial_budget_usd, bool)
        or not isinstance(initial_budget_usd, (int, float))
        or initial_budget_usd < 0
    ):
        raise ValueError("invalid_initial_budget")
    if state_db is not None and not isinstance(state_db, StateDB):
        raise ValueError("invalid_state_db")
    if tier_store is None:
        tier_store = TierSessionStore(default_tier_registry())
    elif not isinstance(tier_store, TierSessionStore):
        raise ValueError("invalid_tier_store")

    reg = CommandRegistry()
    budget_state = _BudgetState(
        initial_usd=initial_budget_usd,
        state_db=state_db,
    )

    # Register in enum order so /help output is consistent.
    reg.register(CommandName.PROJECTS, make_projects_handler(active_project))
    reg.register(CommandName.SWITCH, make_switch_handler())
    reg.register(CommandName.BUDGET, make_budget_handler(budget_state))
    reg.register(CommandName.AGENTS, make_agents_handler(personas))
    reg.register(CommandName.TIER, make_tier_handler(tier_store))
    reg.register(CommandName.LOG, make_log_handler(task_history))
    reg.register(CommandName.STOP, make_stop_handler(runner))
    reg.register(CommandName.RETRY, make_retry_handler())
    reg.register(CommandName.PUSH, make_push_handler(sandbox, task_history))
    reg.register(CommandName.PR, make_pr_handler(sandbox, task_history))
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
    """MVP task handler. Acknowledges receipt — used when the real LLM
    pipeline isn't fully configured.

    The bot falls into this mode when either OPENROUTER_API_KEY or
    REPO_PATH is missing from the environment. The reply explicitly tells
    the user what's missing so they don't think the bot is broken.
    """

    def _handle(text: str, _msg: IncomingMessage) -> BridgeReply:
        excerpt = text if len(text) <= 200 else text[:200] + " …[обрезано]"
        return BridgeReply(
            persona_role="pm_agent",
            body=(
                f"⚠️ Реальный pipeline не настроен — задачу не могу выполнить.\n"
                f"\n"
                f"Получил:\n"
                f"«{excerpt}»\n"
                f"\n"
                f"Чтобы агенты заработали, добавь в .env:\n"
                f"  • OPENROUTER_API_KEY (https://openrouter.ai/keys)\n"
                f"  • REPO_PATH = путь к git-репозиторию (с .git/)\n"
                f"\n"
                f"Затем перезапусти бота. Без этих переменных я могу только "
                f"подтверждать приём, а не запускать пайплайн."
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
    tier_registry = default_tier_registry()
    state_db = StateDB(_resolve_state_db_path(env))
    legacy_tier_path = _resolve_legacy_tier_sessions_path(env)
    if legacy_tier_path is not None:
        migrate_legacy_tier_sessions_json(
            tier_registry,
            persistence_path=legacy_tier_path,
            state_db=state_db,
        )
    tier_store = TierSessionStore(
        tier_registry,
        state_db=state_db,
    )

    # Build sandbox once so it can be shared between the task handler
    # (acquire/release worktrees) and the /push command handler (push_branch_from_main).
    sandbox = _try_build_sandbox(env)
    task_history = (
        TaskHistory(state_db=state_db)
        if sandbox is not None
        else None
    )

    # Build a BackgroundTaskRunner ONLY when the real pipeline is eligible.
    # In simple-stub mode there is nothing to cancel, so spawning an idle
    # worker thread is wasteful.  make_stop_handler(None) correctly returns
    # a "pipeline not configured" stub when runner is None.
    runner: BackgroundTaskRunner | None = (
        BackgroundTaskRunner() if _real_pipeline_eligible(env) else None
    )

    commands = build_command_registry(
        personas,
        state_db=state_db,
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
