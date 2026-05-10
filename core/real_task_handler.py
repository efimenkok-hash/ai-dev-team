"""
core/real_task_handler.py

Step 14b-5: integration glue. Wires the Telegram bridge to the full pipeline:

    bridge → make_real_task_handler(...)
              ├── TierSessionStore   (which tier did this chat pick?)
              ├── BackgroundTaskRunner  (one task at a time)
              ├── SandboxWorkspace   (acquire / release worktree per task)
              ├── ProgressEmitter    (stream agent_started/finished into chat)
              └── Orchestrator       (FSM, validators, cost budget)

Behaviour summary
-----------------
1. `_handle(text, msg)` is what the bridge calls. It is non-blocking:
   real work runs in BackgroundTaskRunner's worker thread.

2. Three short-circuit cases return a BridgeReply immediately:
     a. chat hasn't picked a tier  → ask for `/tier set <name>`
     b. saved tier name no longer exists in registry → ask to re-pick
     c. runner is already busy → tell user, do not queue

3. Otherwise we submit a closure to the runner that:
     - acquires a fresh worktree
     - builds the agent registry for the chosen tier (via injected factory)
     - wraps it with ProgressEmitter so each agent_started/finished event
       streams a short Russian-language line back to the chat
     - runs Orchestrator with reject_long_task + reject_injection_markers
       validators and the configured cost budget
     - releases the worktree in a `finally` (always)
   The on_complete callback sends a final success / failure summary back
   to the same chat.

CONTRACTS:
1. RealTaskHandlerConfig is frozen; __post_init__ validates every field.
2. generate_task_id() always returns a string matching SandboxWorkspace's
   _TASK_ID_RE (lowercase ASCII, hyphen-safe, 1-64 chars).
3. make_real_task_handler validates ALL injected dependencies; bad type
   → ValueError immediately.
4. send_progress(chat_id, text) failures are swallowed — streaming must
   never crash the worker.
5. Agent_registry_factory is called ONCE per submitted task with the
   chat's current tier. Default factory is provided for tests; production
   wiring will pass in an LLMDispatcher-backed factory in 14b-5b.
6. The handler closure is thread-safe: it shares no mutable state with
   the runner thread except via the runner's own queue + tier_store
   (which is internally locked).
"""

import contextlib
import re
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core.adapter import ProjectAdapter, ProjectCommand
from core.background_runner import (
    BackgroundTaskRunner,
    CancellationToken,
    RunnerBusyError,
    TaskHandle,
)
from core.coordinator_onboarding import (
    ProjectCaptainOnboardingContext,
    ProjectCaptainOnboardingService,
)
from core.coordinator_role import COORDINATOR_ROLE
from core.fsm import State
from core.memory import PipelineMemory
from core.model_tier import TierConfig
from core.observability import Observability
from core.orchestrator import (
    AgentRegistry,
    Orchestrator,
    RunResult,
    default_agent_registry,
    reject_injection_markers,
    reject_long_task,
)
from core.progress_emitter import (
    ProgressEmitter,
    ProgressEvent,
    wrap_registry_with_progress,
)
from core.project_runtime_router import (
    ProjectRuntimeRouter,
    ResolvedProjectRuntime,
    describe_project_runtime_error,
)
from core.runtime_validator import RuntimeValidator, ValidationStrategy
from core.sandbox_runtime_hook import make_sandbox_hook
from core.sandbox_workspace import (
    SandboxError,
    SandboxWorkspace,
    WorktreeHandle,
)
from core.task_history import TaskHistory, TaskSummary
from core.telegram_bridge import BridgeReply, IncomingMessage, TaskHandler
from core.tier_session import TierSessionStore

# Same shape as sandbox_workspace._TASK_ID_RE — duplicated to avoid a
# private import; we self-validate before handing the id to the sandbox.
_TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Streaming format chosen to be readable in Telegram with fixed-width spans.
SendProgress = Callable[[int, str], None]
AgentRegistryFactory = Callable[[TierConfig], AgentRegistry]


@dataclass(frozen=True)
class RealTaskHandlerConfig:
    cost_budget_usd: float = 5.0
    max_task_chars: int = 10_000
    author_name: str = "AI Dev Team Bot"
    author_email: str = "bot@ai-dev-team.local"

    def __post_init__(self) -> None:
        if (
            isinstance(self.cost_budget_usd, bool)
            or not isinstance(self.cost_budget_usd, (int, float))
            or self.cost_budget_usd <= 0
        ):
            raise ValueError(f"invalid_cost_budget:{self.cost_budget_usd!r}")
        if (
            isinstance(self.max_task_chars, bool)
            or not isinstance(self.max_task_chars, int)
            or self.max_task_chars <= 0
        ):
            raise ValueError(f"invalid_max_task_chars:{self.max_task_chars!r}")
        if not isinstance(self.author_name, str) or not self.author_name.strip():
            raise ValueError("empty_author_name")
        if (
            not isinstance(self.author_email, str)
            or "@" not in self.author_email
            or not self.author_email.strip()
        ):
            raise ValueError("invalid_author_email")


def generate_task_id(
    *,
    prefix: str = "task",
    clock: Callable[[], float] | None = None,
) -> str:
    """Returns a unique task_id like 'task-1714829400-7a3b9c'.

    Always matches the SandboxWorkspace task_id regex.
    """
    if not isinstance(prefix, str) or not prefix.strip():
        raise ValueError("empty_prefix")
    if not _TASK_ID_RE.match(prefix):
        raise ValueError(f"invalid_prefix:{prefix!r}")
    now = clock() if clock is not None else time.time()
    if isinstance(now, bool) or not isinstance(now, (int, float)) or now <= 0:
        raise ValueError(f"invalid_clock_value:{now!r}")
    suffix = uuid.uuid4().hex[:6]
    candidate = f"{prefix}-{int(now)}-{suffix}"
    if not _TASK_ID_RE.match(candidate):  # paranoia
        raise RuntimeError(f"generated_invalid_id:{candidate}")
    return candidate


def _format_event(event: ProgressEvent) -> str:
    """Translate a ProgressEvent into a single short Russian-language line."""
    kind = event.kind
    agent = event.agent_role or "—"
    detail = (event.detail or "").strip()
    if kind == "task_started":
        return f"🚀 Старт{(' · ' + detail) if detail else ''}"
    if kind == "agent_started":
        return f"▶︎ {agent} начал"
    if kind == "agent_finished":
        ms = event.duration_ms or 0
        return f"✓ {agent} закончил ({ms} мс)"
    if kind == "agent_failed":
        return f"⚠️ {agent} упал — {detail[:160]}"
    if kind == "fsm_transition":
        return f"⤳ {detail}" if detail else "⤳ переход"
    if kind == "task_completed":
        return f"🏁 Готово{(' · ' + detail) if detail else ''}"
    if kind == "task_failed":
        return f"💥 Провалена: {detail or 'причина не указана'}"
    return f"[{kind}] {detail}"


def _default_agent_registry_factory(_tier: TierConfig) -> AgentRegistry:
    """Fallback factory for tests and local runs without a configured LLMDispatcher.

    Returns the legacy single-model `core.agents.*` registry (ask_openrouter
    hard-coded to qwen/qwen3-coder). Production wiring passes a dispatcher-aware
    factory built by build_dispatcher_agent_registry_factory() from
    core.dispatcher_agents instead.
    """
    return default_agent_registry()


def make_real_task_handler(
    *,
    runner: BackgroundTaskRunner,
    sandbox: SandboxWorkspace | None = None,
    runtime_router: ProjectRuntimeRouter | None = None,
    tier_store: TierSessionStore,
    send_progress: SendProgress,
    agent_registry_factory: AgentRegistryFactory = _default_agent_registry_factory,
    config: RealTaskHandlerConfig | None = None,
    observability: Observability | None = None,
    memory_factory: Callable[[], PipelineMemory] = PipelineMemory,
    task_id_factory: Callable[[], str] = generate_task_id,
    task_history: TaskHistory | None = None,
    onboarding_service: ProjectCaptainOnboardingService | None = None,
) -> TaskHandler:
    """Build a TaskHandler that runs the full agent pipeline asynchronously.

    Returns a callable matching `core.telegram_bridge.TaskHandler`:
      (text: str, msg: IncomingMessage) -> BridgeReply | None

    Production wiring (in build_bridge_from_env-style assembly):
      handler = make_real_task_handler(
          runner=BackgroundTaskRunner(),
          sandbox=SandboxWorkspace(SandboxConfig(main_repo_path=...)),
          tier_store=TierSessionStore(default_tier_registry()),
          send_progress=lambda chat_id, text: bot.send_message(chat_id, text),
          agent_registry_factory=build_dispatcher_agent_registry_factory(dispatcher),
      )
    """
    if not isinstance(runner, BackgroundTaskRunner):
        raise ValueError(f"invalid_runner:{type(runner).__name__}")
    if sandbox is not None and not isinstance(sandbox, SandboxWorkspace):
        raise ValueError(f"invalid_sandbox:{type(sandbox).__name__}")
    if runtime_router is not None and not isinstance(
        runtime_router,
        ProjectRuntimeRouter,
    ):
        raise ValueError(
            f"invalid_runtime_router:{type(runtime_router).__name__}"
        )
    if sandbox is None and runtime_router is None:
        raise ValueError("sandbox_or_runtime_router_required")
    if not isinstance(tier_store, TierSessionStore):
        raise ValueError(f"invalid_tier_store:{type(tier_store).__name__}")
    if not callable(send_progress):
        raise ValueError("send_progress_not_callable")
    if not callable(agent_registry_factory):
        raise ValueError("agent_registry_factory_not_callable")
    if config is None:
        config = RealTaskHandlerConfig()
    elif not isinstance(config, RealTaskHandlerConfig):
        raise ValueError(f"invalid_config:{type(config).__name__}")
    if observability is not None and not isinstance(observability, Observability):
        raise ValueError(
            f"invalid_observability:{type(observability).__name__}"
        )
    if not callable(memory_factory):
        raise ValueError("memory_factory_not_callable")
    if not callable(task_id_factory):
        raise ValueError("task_id_factory_not_callable")
    if task_history is not None and not isinstance(task_history, TaskHistory):
        raise ValueError(f"invalid_task_history:{type(task_history).__name__}")
    if onboarding_service is None:
        onboarding_service = ProjectCaptainOnboardingService()
    elif not isinstance(
        onboarding_service,
        ProjectCaptainOnboardingService,
    ):
        raise ValueError(
            "invalid_onboarding_service:"
            f"{type(onboarding_service).__name__}"
        )

    available_tiers = ", ".join(tier_store.registry.list_names())

    def _safe_send(chat_id: int, text: str) -> None:
        with contextlib.suppress(Exception):
            send_progress(chat_id, text)

    def _build_run_fn(
        chat_id: int,
        task_id: str,
        owner_task_text: str,
        pipeline_task_prompt: str,
        tier_name: str,
        sandbox_workspace: SandboxWorkspace,
    ) -> Callable[[CancellationToken], dict]:
        """Builds the closure that the runner will execute in a worker thread.

        Captures (chat_id, task_id, text, tier_name) by value. The closure
        is the only place that touches the worktree; everything inside is
        wrapped so that the worktree is always released.
        """

        # Terminal events must reach the user even after /stop — they carry
        # the final verdict (task_completed / task_failed).  Only intermediate
        # progress events are silenced so the chat isn't flooded after cancel.
        _TERMINAL_EVENTS: frozenset[str] = frozenset(
            {"task_completed", "task_failed"}
        )

        def _run(token: CancellationToken) -> dict:
            handle: WorktreeHandle | None = None

            def _on_event(evt: ProgressEvent) -> None:
                if token.is_set() and evt.kind not in _TERMINAL_EVENTS:
                    return
                _safe_send(chat_id, _format_event(evt))

            emitter = ProgressEmitter(_on_event)
            emitter.emit_task_started(
                detail=f"task_id={task_id} · тариф {tier_name}"
            )

            tier = tier_store.registry.get(tier_name)

            try:
                handle = sandbox_workspace.acquire(task_id)
            except (SandboxError, ValueError) as exc:
                emitter.emit_task_failed(
                    reason=(
                        f"sandbox_error:{type(exc).__name__}:"
                        f"{str(exc)[:160]}"
                    ),
                )
                raise

            try:
                _safe_send(
                    chat_id,
                    (
                        f"🌳 worktree готов\n"
                        f"  branch  `{handle.branch}`\n"
                        f"  path    `{handle.path.name}`"
                    ),
                )

                memory = memory_factory()
                base_registry = agent_registry_factory(tier)
                cost_estimator = getattr(base_registry, "cost_estimator", None)
                if cost_estimator is not None and not callable(cost_estimator):
                    raise ValueError("registry_cost_estimator_not_callable")
                wrapped = wrap_registry_with_progress(base_registry, emitter)

                # Build the runtime-validation hook: writes writer artifact into
                # the worktree then runs ruff against it.
                # Custom lint command: `ruff check .` targets the whole worktree
                # rather than the default ["core", "tests"] which don't exist in
                # generated projects.
                # run_tests=False: tester output is not written to the worktree
                # (only writer output is), so pytest would find no tests.
                _lint_cmd = ProjectCommand(
                    name="lint",
                    cmd=(sys.executable, "-m", "ruff", "check", "."),
                    timeout_seconds=60,
                )

                def _adapter_factory(p: Path) -> ProjectAdapter:
                    return ProjectAdapter(
                        name="sandbox",
                        project_path=p,
                        language="python",
                        commands={"lint": _lint_cmd},
                    )

                runtime_validator = RuntimeValidator(
                    strategy=ValidationStrategy.INPLACE,
                    run_lint=True,
                    run_tests=False,
                )
                runtime_hook = make_sandbox_hook(
                    handle=handle,
                    adapter_factory=_adapter_factory,
                    validator=runtime_validator,
                )

                prompt_overhead = max(
                    0,
                    len(pipeline_task_prompt) - len(owner_task_text),
                )

                orch = Orchestrator(
                    memory=memory,
                    agents=wrapped,
                    observability=observability,
                    task_validators=(
                        reject_long_task(
                            config.max_task_chars + prompt_overhead
                        ),
                        reject_injection_markers(),
                    ),
                    cost_estimator=cost_estimator,
                    cost_budget_usd=config.cost_budget_usd,
                    runtime_validator=runtime_hook,
                )

                result: RunResult = orch.run(task_id, pipeline_task_prompt)

                # /stop pressed while pipeline was running?  Orchestrator
                # cannot be interrupted mid-flight (it runs all agents to
                # completion), but we must NOT lie to the user: override
                # final_state to CANCELLED, skip commit, and let
                # _build_on_complete send the ⏹ message.
                cancelled = token.is_set()
                final_state = result.final_state

                summary: dict = {
                    "task_id": task_id,
                    "final_state": "CANCELLED" if cancelled else final_state.value,
                    "branch": handle.branch,
                    "worktree": str(handle.path),
                    "failure_reason": (
                        "cancelled_by_user" if cancelled else result.failure_reason
                    ),
                    "tier_name": tier_name,
                    "commit_sha": None,
                }

                if cancelled:
                    # Do NOT commit — user explicitly stopped the task.
                    # _build_on_complete will send the ⏹ Отменено message.
                    pass
                elif final_state == State.SUCCESS:
                    try:
                        summary["commit_sha"] = sandbox_workspace.commit_in_worktree(
                            handle,
                            message=f"AI Dev Team: {owner_task_text[:60]}",
                            author_name=config.author_name,
                            author_email=config.author_email,
                        )
                    except Exception as commit_exc:
                        # Pipeline produced SUCCESS but commit failed → честно
                        # репортим. Чаще всего: nothing_to_commit (файлы не
                        # изменились относительно base) или git config issue.
                        summary["final_state"] = "FAIL"
                        summary["failure_reason"] = (
                            f"commit_failed:{type(commit_exc).__name__}:"
                            f"{str(commit_exc)[:160]}"
                        )
                        emitter.emit_task_failed(
                            reason=(
                                f"commit_failed · {type(commit_exc).__name__}: "
                                f"{str(commit_exc)[:120]}"
                            ),
                        )
                    else:
                        emitter.emit_task_completed(
                            summary=(
                                f"branch={handle.branch}"
                                f" · commit={summary['commit_sha'][:8]}"
                            )
                        )
                else:
                    emitter.emit_task_failed(
                        reason=(
                            f"final={final_state.value} · "
                            f"{(result.failure_reason or '')[:160]}"
                        ),
                    )
                return summary
            finally:
                if handle is not None:
                    with contextlib.suppress(Exception):
                        sandbox_workspace.release(handle, delete_branch=False)

        return _run

    def _resolve_runtime_for_message(
        msg: IncomingMessage,
    ) -> tuple[
        SandboxWorkspace | None,
        str | None,
        str | None,
        ResolvedProjectRuntime | None,
    ]:
        if runtime_router is None:
            return (sandbox, None, None, None)
        try:
            resolved_runtime = runtime_router.resolve_message_runtime(msg)
        except ValueError as exc:
            return (None, str(exc), None, None)
        return (
            resolved_runtime.sandbox,
            None,
            resolved_runtime.snapshot.project.project_id,
            resolved_runtime,
        )

    def _build_pipeline_task_prompt(
        *,
        owner_task_text: str,
        msg: IncomingMessage,
        resolved_runtime: ResolvedProjectRuntime | None,
    ) -> str:
        if resolved_runtime is None:
            return owner_task_text
        if msg.project_context_source not in {
            "bound_chat",
            "owner_dm_single_project",
        }:
            return owner_task_text
        context = ProjectCaptainOnboardingContext(
            snapshot=resolved_runtime.snapshot,
            chat_provider="telegram",
            chat_id=msg.chat_id,
            user_id=msg.user_id,
            context_source=msg.project_context_source,
            owner_task_text=owner_task_text,
        )
        return onboarding_service.build_pipeline_task_prompt(context)

    def _runtime_error_reply(reason_code: str) -> BridgeReply:
        return BridgeReply(
            persona_role=COORDINATOR_ROLE,
            body=(
                "⚠️ Не удалось определить runtime проекта для этой задачи.\n"
                "\n"
                f"{describe_project_runtime_error(reason_code)}"
            ),
        )

    def _build_on_complete(
        chat_id: int,
        *,
        project_id: str | None,
    ) -> Callable[..., None]:
        def _on_complete(handle: TaskHandle, result, error) -> None:
            if error is not None:
                _safe_send(
                    chat_id,
                    (
                        f"❌ Воркер упал\n"
                        f"  task-id `{handle.task_id}`\n"
                        f"  {type(error).__name__}: {str(error)[:200]}"
                    ),
                )
                return
            if not isinstance(result, dict):
                _safe_send(
                    chat_id,
                    (
                        f"❌ Воркер вернул неожиданный результат\n"
                        f"  task-id `{handle.task_id}`\n"
                        f"  type={type(result).__name__}"
                    ),
                )
                return
            final_state = result.get("final_state", "?")
            branch = result.get("branch", "?")
            tier_name = result.get("tier_name", "?")
            commit_sha = result.get("commit_sha") or ""
            sha_short = commit_sha[:8] if commit_sha else ""

            # Record to shared TaskHistory so /push and /log can access it.
            if task_history is not None:
                with contextlib.suppress(Exception):
                    task_history.record(
                        TaskSummary(
                            task_id=handle.task_id,
                            branch=branch,
                            commit_sha=commit_sha or None,
                            final_state=final_state,
                            failure_reason=result.get("failure_reason"),
                            tier_name=tier_name,
                            finished_at=time.time(),
                            project_id=project_id,
                        )
                    )

            if final_state == "CANCELLED":
                _safe_send(
                    chat_id,
                    (
                        f"⏹ Отменено пользователем\n"
                        f"\n"
                        f"  task-id `{handle.task_id}`\n"
                        f"  тариф   `{tier_name}`\n"
                        f"  Коммит не сделан."
                    ),
                )
            elif final_state == State.SUCCESS.value:
                _safe_send(
                    chat_id,
                    (
                        f"✅ Готово\n"
                        f"\n"
                        f"  task-id `{handle.task_id}`\n"
                        f"  тариф   `{tier_name}`\n"
                        f"  branch  `{branch}`\n"
                        + (f"  commit  `{sha_short}`\n" if sha_short else "")
                        + f"\n"
                        f"Запуш в GitHub:  /push {handle.task_id}"
                    ),
                )
            else:
                _safe_send(
                    chat_id,
                    (
                        f"❌ Не получилось\n"
                        f"\n"
                        f"  task-id `{handle.task_id}`\n"
                        f"  тариф   `{tier_name}`\n"
                        f"  state   `{final_state}`\n"
                        f"  reason  `{result.get('failure_reason', '?')}`"
                    ),
                )

        return _on_complete

    def _handle(text: str, msg: IncomingMessage) -> BridgeReply | None:
        if not isinstance(msg, IncomingMessage):
            return BridgeReply(
                persona_role=COORDINATOR_ROLE,
                body="Внутренняя ошибка моста: неизвестный формат сообщения.",
            )
        chat_id = msg.chat_id

        # 1. Has the chat picked a tier?
        tier_name = tier_store.active_tier_name(chat_id)
        if tier_name is None:
            return BridgeReply(
                persona_role=COORDINATOR_ROLE,
                body=(
                    f"💼 Сначала выбери тариф моделей.\n"
                    f"\n"
                    f"  /tier set <имя>\n"
                    f"\n"
                    f"Доступно: {available_tiers}\n"
                    f"Подробнее:  /tier"
                ),
            )

        # 2. Validate the saved tier still exists in registry.
        try:
            tier_store.registry.get(tier_name)
        except KeyError:
            return BridgeReply(
                persona_role=COORDINATOR_ROLE,
                body=(
                    f"⚠️ Сохранённый тариф «{tier_name}» больше не доступен.\n"
                    f"\n"
                    f"Выбери заново:  /tier set <имя>\n"
                    f"Доступно: {available_tiers}"
                ),
            )

        (
            sandbox_workspace,
            runtime_error,
            project_id,
            resolved_runtime,
        ) = _resolve_runtime_for_message(msg)
        if runtime_error is not None or sandbox_workspace is None:
            return _runtime_error_reply(
                runtime_error or "bootstrap_active_project_runtime_invalid"
            )
        try:
            pipeline_task_prompt = _build_pipeline_task_prompt(
                owner_task_text=text,
                msg=msg,
                resolved_runtime=resolved_runtime,
            )
        except ValueError as exc:
            return BridgeReply(
                persona_role=COORDINATOR_ROLE,
                body=(
                    "⚠️ Не удалось подготовить Coordinator onboarding "
                    f"для pipeline: {str(exc)[:200]}"
                ),
            )

        # 3. Generate task_id BEFORE submission so we can show it in the ack.
        try:
            task_id = task_id_factory()
        except Exception as exc:
            return BridgeReply(
                persona_role=COORDINATOR_ROLE,
                body=(
                    f"⚠️ Не удалось сгенерировать task_id: "
                    f"{type(exc).__name__}: {str(exc)[:120]}"
                ),
            )
        if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id):
            return BridgeReply(
                persona_role=COORDINATOR_ROLE,
                body=f"⚠️ Невалидный task_id: `{task_id!r}`",
            )

        # 4. Submit. RunnerBusyError is a USER-FACING outcome, not a bug.
        run_fn = _build_run_fn(
            chat_id,
            task_id,
            text,
            pipeline_task_prompt,
            tier_name,
            sandbox_workspace,
        )
        on_complete = _build_on_complete(chat_id, project_id=project_id)
        try:
            runner.submit(
                task_id=task_id,
                raw_task=text,
                run_fn=run_fn,
                on_complete=on_complete,
            )
        except RunnerBusyError as exc:
            current = exc.current_handle
            current_excerpt = current.raw_task
            if len(current_excerpt) > 120:
                current_excerpt = current_excerpt[:120] + " …"
            return BridgeReply(
                persona_role=COORDINATOR_ROLE,
                body=(
                    f"⏳ Сейчас уже работаю.\n"
                    f"\n"
                    f"  «{current_excerpt}»\n"
                    f"  task-id `{current.task_id}`\n"
                    f"\n"
                    f"Возьму следующую как только освобожусь."
                ),
            )
        except Exception as exc:
            return BridgeReply(
                persona_role=COORDINATOR_ROLE,
                body=(
                    f"⚠️ Не удалось запустить задачу: "
                    f"{type(exc).__name__}: {str(exc)[:200]}"
                ),
            )

        # 5. Immediate ack.
        excerpt = text if len(text) <= 120 else text[:120] + " …"
        return BridgeReply(
            persona_role=COORDINATOR_ROLE,
            body=(
                f"🚀 Принял в работу\n"
                f"\n"
                f"  «{excerpt}»\n"
                f"\n"
                f"  task-id `{task_id}`\n"
                f"  тариф   `{tier_name}`\n"
                f"\n"
                f"Прогресс пришлю отдельными сообщениями. Стоп: /stop."
            ),
        )

    return _handle
