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
from core.agent_bus import StateBackedAgentBus
from core.agent_bus_projection import (
    AgentBusProjectionService,
    ProjectingAgentBus,
)
from core.agent_bus_projection_throttle import ThrottledProjectingAgentBus
from core.agent_personas import default_registry
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
from core.coordinator_owner_escalation import (
    CoordinatorOwnerEscalationContext,
    CoordinatorOwnerEscalationService,
)
from core.coordinator_role import COORDINATOR_ROLE
from core.coordinator_team_assembly import (
    CoordinatorTeamAssemblyContext,
    CoordinatorTeamAssemblyService,
)
from core.coordinator_team_proposal import (
    CoordinatorTeamProposalService,
)
from core.fsm import State
from core.json_extractor import extract_json_object
from core.logical_hiring import LogicalHiringService
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
from core.owner_dm_routing import OwnerDmRoutingService
from core.progress_emitter import (
    ProgressEmitter,
    ProgressEvent,
    wrap_registry_with_progress,
)
from core.project_chat_posting import (
    ProjectChatPostingContext,
    ProjectChatPostingService,
    format_progress_event,
)
from core.project_runtime_router import (
    ProjectRuntimeRouter,
    ResolvedProjectRuntime,
    describe_project_runtime_error,
)
from core.project_team_state import ProjectSpecialistRoster
from core.runtime_validator import RuntimeValidator, ValidationStrategy
from core.sandbox_runtime_hook import make_sandbox_hook
from core.sandbox_workspace import (
    SandboxError,
    SandboxWorkspace,
    WorktreeHandle,
)
from core.specialization_hints import SpecializationHints
from core.task_history import TaskHistory, TaskSummary
from core.telegram_bridge import (
    BridgeReply,
    IncomingMessage,
    OutgoingEnvelope,
    OutgoingMessage,
    TaskHandler,
)
from core.tier_session import TierSessionStore

# Same shape as sandbox_workspace._TASK_ID_RE — duplicated to avoid a
# private import; we self-validate before handing the id to the sandbox.
_TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Streaming format chosen to be readable in Telegram with fixed-width spans.
SendProgress = Callable[[int, str], None]
SendProgressEnvelope = Callable[[OutgoingEnvelope], None]
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
    return format_progress_event(event)


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
    send_progress: SendProgress | None = None,
    send_progress_envelope: SendProgressEnvelope | None = None,
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
    if send_progress is not None and not callable(send_progress):
        raise ValueError("send_progress_not_callable")
    if send_progress_envelope is not None and not callable(send_progress_envelope):
        raise ValueError("send_progress_envelope_not_callable")
    if send_progress is None and send_progress_envelope is None:
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
    personas = default_registry()
    team_assembly_service = CoordinatorTeamAssemblyService()
    owner_escalation_service = CoordinatorOwnerEscalationService()
    team_proposal_service = CoordinatorTeamProposalService()
    posting_service = ProjectChatPostingService()
    owner_dm_routing_service = OwnerDmRoutingService()

    available_tiers = ", ".join(tier_store.registry.list_names())

    def _adapt_legacy_send_progress(
        legacy_send_progress: SendProgress | None,
    ) -> SendProgressEnvelope:
        if legacy_send_progress is None or not callable(legacy_send_progress):
            raise ValueError("send_progress_not_callable")

        def _send(envelope: OutgoingEnvelope) -> None:
            legacy_send_progress(
                envelope.message.chat_id,
                envelope.message.text,
            )

        return _send

    _send_progress_envelope: SendProgressEnvelope = (
        send_progress_envelope
        if send_progress_envelope is not None
        else _adapt_legacy_send_progress(send_progress)
    )
    collaboration_bus: ThrottledProjectingAgentBus | None = None
    logical_hiring_service: LogicalHiringService | None = None
    if runtime_router is not None and runtime_router.registry is not None:
        collaboration_bus = ThrottledProjectingAgentBus(
            ProjectingAgentBus(
                StateBackedAgentBus(runtime_router.registry.state_db),
                AgentBusProjectionService(
                    runtime_router.registry,
                    _send_progress_envelope,
                ),
            )
        )
        logical_hiring_service = LogicalHiringService(runtime_router.registry)

    def _safe_send_envelope(envelope: OutgoingEnvelope) -> None:
        with contextlib.suppress(Exception):
            _send_progress_envelope(envelope)

    def _safe_send(chat_id: int, text: str) -> None:
        with contextlib.suppress(Exception):
            _send_progress_envelope(
                OutgoingEnvelope(
                    message=OutgoingMessage(chat_id=chat_id, text=text),
                    sender_role=COORDINATOR_ROLE,
                )
            )

    def _build_run_fn(
        chat_id: int,
        task_id: str,
        project_id: str | None,
        owner_task_text: str,
        onboarding_context: ProjectCaptainOnboardingContext | None,
        posting_context: ProjectChatPostingContext | None,
        owner_dm_delivery_role: str | None,
        pipeline_task_prompt: str,
        initial_artifacts: dict[str, str] | None,
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
                _safe_send_envelope(
                    _build_event_envelope(
                        chat_id=chat_id,
                        event=evt,
                        posting_context=posting_context,
                        owner_dm_delivery_role=owner_dm_delivery_role,
                    )
                )

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
                _safe_send_envelope(
                    _build_system_envelope(
                        chat_id=chat_id,
                        posting_context=posting_context,
                        owner_dm_delivery_role=owner_dm_delivery_role,
                        text=(
                        f"🌳 worktree готов\n"
                        f"  branch  `{handle.branch}`\n"
                        f"  path    `{handle.path.name}`"
                        ),
                    )
                )

                memory = memory_factory()

                def _resolve_runtime_specialization_hints() -> SpecializationHints:
                    pm_artifact = memory.get_artifact(task_id, "pm")
                    if pm_artifact is None:
                        return SpecializationHints.empty()
                    pm_payload = extract_json_object(pm_artifact)
                    if pm_payload is None or not isinstance(pm_payload, dict):
                        raise ValueError(
                            "invalid_pm_specialization_hints_payload"
                        )
                    return SpecializationHints.from_pm_payload(pm_payload)

                collaboration_registry_builder = getattr(
                    agent_registry_factory,
                    "build_collaboration_registry",
                    None,
                )
                if (
                    project_id is not None
                    and collaboration_bus is not None
                    and callable(collaboration_registry_builder)
                ):
                    collaboration_thread = (
                        collaboration_bus.projecting_bus.get_or_open_task_thread(
                            project_id,
                            task_id,
                            opened_by_role=COORDINATOR_ROLE,
                            created_at=time.time(),
                        )
                    )
                    base_registry = collaboration_registry_builder(
                        tier,
                        project_id=project_id,
                        task_id=task_id,
                        thread=collaboration_thread,
                        owner_task_text=owner_task_text,
                        bus=collaboration_bus,
                        specialization_hints_provider=(
                            _resolve_runtime_specialization_hints
                        ),
                    )
                else:
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

                result: RunResult = orch.run(
                    task_id,
                    pipeline_task_prompt,
                    initial_artifacts=initial_artifacts,
                )

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

                if (
                    not cancelled
                    and logical_hiring_service is not None
                    and onboarding_context is not None
                ):
                    try:
                        logical_hiring_result = logical_hiring_service.run_from_hints(
                            onboarding_context.snapshot,
                            _resolve_runtime_specialization_hints(),
                        )
                    except Exception as logical_hiring_exc:
                        summary["logical_hiring_status"] = "system_error"
                        summary["logical_hiring_reply"] = (
                            "🧩 Логический hire не удалось обработать; "
                            "persisted project roster не менялся.\n"
                            "\n"
                            "Техническая причина: "
                            f"`{type(logical_hiring_exc).__name__}: "
                            f"{str(logical_hiring_exc)[:160]}`"
                        )
                    else:
                        summary["logical_hiring_status"] = logical_hiring_result.status
                        if logical_hiring_result.status != "no_candidates":
                            summary["logical_hiring_reply"] = (
                                logical_hiring_result.message_text
                            )

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
                _attach_owner_escalation(
                    summary=summary,
                    onboarding_context=onboarding_context,
                    memory=memory,
                    task_id=task_id,
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

    def _build_project_onboarding_context(
        *,
        owner_task_text: str,
        msg: IncomingMessage,
        resolved_runtime: ResolvedProjectRuntime | None,
    ) -> ProjectCaptainOnboardingContext | None:
        if resolved_runtime is None:
            return None
        if msg.project_context_source not in {
            "bound_chat",
            "owner_dm_single_project",
        }:
            return None
        return ProjectCaptainOnboardingContext(
            snapshot=resolved_runtime.snapshot,
            chat_provider="telegram",
            chat_id=msg.chat_id,
            user_id=msg.user_id,
            context_source=msg.project_context_source,
            owner_task_text=owner_task_text,
        )

    def _build_pipeline_task_prompt(
        onboarding_context: ProjectCaptainOnboardingContext | None,
        *,
        owner_task_text: str,
    ) -> str:
        if onboarding_context is None:
            return owner_task_text
        return onboarding_service.build_pipeline_task_prompt(onboarding_context)

    def _build_initial_artifacts(
        onboarding_context: ProjectCaptainOnboardingContext | None,
        project_specialist_roster: ProjectSpecialistRoster | None = None,
    ) -> dict[str, str] | None:
        if onboarding_context is None:
            return None
        if project_specialist_roster is None:
            project_specialist_roster = ProjectSpecialistRoster(
                project_id=onboarding_context.snapshot.project.project_id,
                specialist_roles=(),
            )
        team_assembly_context = CoordinatorTeamAssemblyContext(
            snapshot=onboarding_context.snapshot,
            owner_task_text=onboarding_context.owner_task_text,
            context_source=onboarding_context.context_source,
            personas=personas,
            project_specialist_roster=project_specialist_roster,
            specialization_hints=SpecializationHints.empty(),
        )
        team_assembly = team_assembly_service.assemble_team(
            team_assembly_context
        )
        return {
            "project_brief": onboarding_service.build_project_brief_artifact(
                onboarding_context
            ),
            "team_proposal": team_proposal_service.build_team_proposal_artifact(
                team_assembly
            ),
        }

    def _build_project_chat_posting_context(
        onboarding_context: ProjectCaptainOnboardingContext | None,
    ) -> ProjectChatPostingContext | None:
        if onboarding_context is None:
            return None
        return ProjectChatPostingContext(
            snapshot=onboarding_context.snapshot,
            chat_id=onboarding_context.chat_id,
            context_source=onboarding_context.context_source,
        )

    def _fallback_progress_event_envelope(
        chat_id: int,
        event: ProgressEvent,
        owner_dm_delivery_role: str | None,
    ) -> OutgoingEnvelope:
        return OutgoingEnvelope(
            message=OutgoingMessage(
                chat_id=chat_id,
                text=_format_event(event),
            ),
            sender_role=COORDINATOR_ROLE,
            delivery_role=owner_dm_delivery_role,
        )

    def _resolve_owner_dm_delivery_role(
        msg: IncomingMessage,
    ) -> str | None:
        if msg.project_context_source != "owner_dm_single_project":
            return None
        if msg.incoming_bot_role is None:
            return None
        if not owner_dm_routing_service.is_owner_dm_message(msg):
            return None
        try:
            context = owner_dm_routing_service.build_context(msg)
            return owner_dm_routing_service.resolve_delivery_role(
                context,
                COORDINATOR_ROLE,
            )
        except ValueError:
            return None

    def _apply_owner_dm_delivery_role(
        envelope: OutgoingEnvelope,
        owner_dm_delivery_role: str | None,
    ) -> OutgoingEnvelope:
        if owner_dm_delivery_role is None:
            return envelope
        return OutgoingEnvelope(
            message=envelope.message,
            sender_role=envelope.sender_role,
            delivery_role=owner_dm_delivery_role,
        )

    def _build_event_envelope(
        *,
        chat_id: int,
        event: ProgressEvent,
        posting_context: ProjectChatPostingContext | None,
        owner_dm_delivery_role: str | None,
    ) -> OutgoingEnvelope:
        if (
            posting_context is not None
            and posting_context.context_source == "owner_dm_single_project"
            and owner_dm_delivery_role is not None
            and event.kind in {"agent_started", "agent_finished", "agent_failed"}
        ):
            if not isinstance(event.agent_role, str) or not event.agent_role.strip():
                return _fallback_progress_event_envelope(
                    chat_id,
                    event,
                    owner_dm_delivery_role,
                )
            return OutgoingEnvelope(
                message=OutgoingMessage(
                    chat_id=chat_id,
                    text=_format_event(event),
                ),
                sender_role=event.agent_role,
                delivery_role=owner_dm_delivery_role,
            )
        if posting_context is None:
            return _fallback_progress_event_envelope(
                chat_id,
                event,
                owner_dm_delivery_role,
            )
        try:
            envelope = posting_service.build_event_envelope(posting_context, event)
        except ValueError:
            return _fallback_progress_event_envelope(
                chat_id,
                event,
                owner_dm_delivery_role,
            )
        return _apply_owner_dm_delivery_role(envelope, owner_dm_delivery_role)

    def _build_system_envelope(
        *,
        chat_id: int,
        text: str,
        posting_context: ProjectChatPostingContext | None,
        owner_dm_delivery_role: str | None,
    ) -> OutgoingEnvelope:
        if posting_context is None:
            return _apply_owner_dm_delivery_role(
                OutgoingEnvelope(
                message=OutgoingMessage(chat_id=chat_id, text=text),
                sender_role=COORDINATOR_ROLE,
                ),
                owner_dm_delivery_role,
            )
        try:
            envelope = posting_service.build_system_envelope(posting_context, text)
        except ValueError:
            return _apply_owner_dm_delivery_role(
                OutgoingEnvelope(
                message=OutgoingMessage(chat_id=chat_id, text=text),
                sender_role=COORDINATOR_ROLE,
                ),
                owner_dm_delivery_role,
            )
        return _apply_owner_dm_delivery_role(envelope, owner_dm_delivery_role)

    def _build_terminal_envelope(
        *,
        chat_id: int,
        text: str,
        posting_context: ProjectChatPostingContext | None,
        owner_dm_delivery_role: str | None,
    ) -> OutgoingEnvelope:
        if posting_context is None:
            return _apply_owner_dm_delivery_role(
                OutgoingEnvelope(
                    message=OutgoingMessage(chat_id=chat_id, text=text),
                    sender_role=COORDINATOR_ROLE,
                ),
                owner_dm_delivery_role,
            )
        try:
            envelope = posting_service.build_terminal_envelope(posting_context, text)
        except ValueError:
            return _apply_owner_dm_delivery_role(
                OutgoingEnvelope(
                    message=OutgoingMessage(chat_id=chat_id, text=text),
                    sender_role=COORDINATOR_ROLE,
                ),
                owner_dm_delivery_role,
            )
        return _apply_owner_dm_delivery_role(envelope, owner_dm_delivery_role)

    def _fallback_owner_escalation_reply(
        *,
        onboarding_context: ProjectCaptainOnboardingContext,
        final_state: str,
    ) -> str:
        return (
            "Координатор: задача по проекту "
            f"`{onboarding_context.snapshot.project.slug}` завершилась "
            f"состоянием `{final_state}` и требует owner review причины "
            "перед следующим запуском."
        )

    def _attach_owner_escalation(
        *,
        summary: dict,
        onboarding_context: ProjectCaptainOnboardingContext | None,
        memory: PipelineMemory,
        task_id: str,
    ) -> None:
        if onboarding_context is None:
            return
        final_state = summary.get("final_state")
        failure_reason = summary.get("failure_reason")
        if final_state not in {"FAIL", "BLOCKED"}:
            return
        if not isinstance(failure_reason, str) or not failure_reason.strip():
            return
        try:
            escalation_context = CoordinatorOwnerEscalationContext(
                snapshot=onboarding_context.snapshot,
                owner_task_text=onboarding_context.owner_task_text,
                context_source=onboarding_context.context_source,
                final_state=final_state,
                failure_reason=failure_reason,
            )
            summary["owner_escalation_type"] = (
                owner_escalation_service.classify_owner_escalation_type(
                    escalation_context
                )
            )
            summary["owner_escalation_reply"] = (
                owner_escalation_service.build_owner_escalation_reply(
                    escalation_context
                )
            )
            artifact = owner_escalation_service.build_owner_escalation_artifact(
                escalation_context
            )
        except Exception:
            summary["owner_escalation_type"] = "system_failure"
            summary["owner_escalation_reply"] = _fallback_owner_escalation_reply(
                onboarding_context=onboarding_context,
                final_state=final_state,
            )
            return
        with contextlib.suppress(Exception):
            memory.set_artifact(task_id, "owner_escalation", artifact)

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
        posting_context: ProjectChatPostingContext | None,
        owner_dm_delivery_role: str | None,
    ) -> Callable[..., None]:
        def _on_complete(handle: TaskHandle, result, error) -> None:
            if error is not None:
                _safe_send_envelope(
                    _build_terminal_envelope(
                        chat_id=chat_id,
                        posting_context=posting_context,
                        owner_dm_delivery_role=owner_dm_delivery_role,
                        text=(
                        f"❌ Воркер упал\n"
                        f"  task-id `{handle.task_id}`\n"
                        f"  {type(error).__name__}: {str(error)[:200]}"
                        ),
                    )
                )
                return
            if not isinstance(result, dict):
                _safe_send_envelope(
                    _build_terminal_envelope(
                        chat_id=chat_id,
                        posting_context=posting_context,
                        owner_dm_delivery_role=owner_dm_delivery_role,
                        text=(
                        f"❌ Воркер вернул неожиданный результат\n"
                        f"  task-id `{handle.task_id}`\n"
                        f"  type={type(result).__name__}"
                        ),
                    )
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

            logical_hiring_reply = result.get("logical_hiring_reply")
            if isinstance(logical_hiring_reply, str) and logical_hiring_reply.strip():
                _safe_send_envelope(
                    _build_system_envelope(
                        chat_id=chat_id,
                        posting_context=posting_context,
                        owner_dm_delivery_role=owner_dm_delivery_role,
                        text=logical_hiring_reply,
                    )
                )

            if final_state == "CANCELLED":
                _safe_send_envelope(
                    _build_terminal_envelope(
                        chat_id=chat_id,
                        posting_context=posting_context,
                        owner_dm_delivery_role=owner_dm_delivery_role,
                        text=(
                        f"⏹ Отменено пользователем\n"
                        f"\n"
                        f"  task-id `{handle.task_id}`\n"
                        f"  тариф   `{tier_name}`\n"
                        f"  Коммит не сделан."
                        ),
                    )
                )
            elif final_state == State.SUCCESS.value:
                _safe_send_envelope(
                    _build_terminal_envelope(
                        chat_id=chat_id,
                        posting_context=posting_context,
                        owner_dm_delivery_role=owner_dm_delivery_role,
                        text=(
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
                )
            else:
                owner_escalation_reply = result.get("owner_escalation_reply")
                _safe_send_envelope(
                    _build_terminal_envelope(
                        chat_id=chat_id,
                        posting_context=posting_context,
                        owner_dm_delivery_role=owner_dm_delivery_role,
                        text=(
                        f"❌ Не получилось\n"
                        f"\n"
                        f"  task-id `{handle.task_id}`\n"
                        f"  тариф   `{tier_name}`\n"
                        f"  state   `{final_state}`\n"
                        + (
                            f"\n{owner_escalation_reply}\n\n"
                            if owner_escalation_reply
                            else ""
                        )
                        + f"  reason  `{result.get('failure_reason', '?')}`"
                        ),
                    )
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
            onboarding_context = _build_project_onboarding_context(
                owner_task_text=text,
                msg=msg,
                resolved_runtime=resolved_runtime,
            )
            posting_context = _build_project_chat_posting_context(
                onboarding_context
            )
            owner_dm_delivery_role = _resolve_owner_dm_delivery_role(msg)
            pipeline_task_prompt = _build_pipeline_task_prompt(
                onboarding_context,
                owner_task_text=text,
            )
            project_specialist_roster = (
                None
                if resolved_runtime is None
                else runtime_router.registry.get_project_specialist_roster(
                    resolved_runtime.snapshot.project.project_id
                )
            )
            initial_artifacts = _build_initial_artifacts(
                onboarding_context,
                project_specialist_roster,
            )
        except ValueError as exc:
            return BridgeReply(
                persona_role=COORDINATOR_ROLE,
                body=(
                    "⚠️ Не удалось подготовить Coordinator artifacts "
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
            project_id,
            text,
            onboarding_context,
            posting_context,
            owner_dm_delivery_role,
            pipeline_task_prompt,
            initial_artifacts,
            tier_name,
            sandbox_workspace,
        )
        on_complete = _build_on_complete(
            chat_id,
            project_id=project_id,
            posting_context=posting_context,
            owner_dm_delivery_role=owner_dm_delivery_role,
        )
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
