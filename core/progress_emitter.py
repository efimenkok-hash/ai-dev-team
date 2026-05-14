"""
core/progress_emitter.py

Step 14b-2 part 1: streaming progress events from a running pipeline.

The orchestrator runs in a background thread (see core.background_runner)
and emits structured ProgressEvent objects as agents come online and finish.
The Telegram bridge consumes these events and turns them into persona-
signed messages so the user sees `Архитектор начал → закончил → Программист
начал → закончил…` instead of a minute of silence.

Design:
- ProgressEvent is a frozen, serialisable record. No formatting decisions
  live here — the bridge does that with PersonaRegistry.
- ProgressEmitter is the *producer* — orchestrator side. It wraps a
  thread-safe callback. Failures in the callback NEVER propagate back
  into orchestrator (UI must not break the pipeline).
- wrap_agent_with_progress / wrap_registry_with_progress are the glue:
  they take an existing AgentRegistry and return a new registry where
  every agent function is wrapped with started/finished/failed events.
  The orchestrator is NOT modified — pure decorator pattern.

CONTRACTS:
1. ProgressEvent is frozen; kind must be one of EVENT_KINDS; agent_role
   optional but if present must be a selectable logical role.
2. ProgressEmitter.emit() is total: never raises, never propagates
   callback errors.
3. wrap_agent_with_progress preserves the agent function's signature
   and return value verbatim — the wrapper is transparent on success.
4. wrap_registry_with_progress returns a NEW dict (does not mutate input).
5. wrap_agent_with_progress emits "started" before calling, "finished" on
   success, "failed" on exception (then re-raises).
"""

import contextlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from core.agent_role_catalog import SELECTABLE_AGENT_ROLES

EVENT_KINDS: frozenset[str] = frozenset({
    "task_started",
    "agent_started",
    "agent_finished",
    "agent_failed",
    "fsm_transition",
    "task_completed",
    "task_failed",
})


@dataclass(frozen=True)
class ProgressEvent:
    kind: str
    timestamp: float
    agent_role: str | None = None
    detail: str = ""
    duration_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in EVENT_KINDS:
            raise ValueError(f"invalid_event_kind:{self.kind!r}")
        if (
            isinstance(self.timestamp, bool)
            or not isinstance(self.timestamp, (int, float))
            or self.timestamp < 0
        ):
            raise ValueError(f"invalid_timestamp:{self.timestamp!r}")
        if self.agent_role is not None:
            if not isinstance(self.agent_role, str) or not self.agent_role.strip():
                raise ValueError("empty_agent_role")
            if self.agent_role not in SELECTABLE_AGENT_ROLES:
                raise ValueError(f"unknown_agent_role:{self.agent_role}")
        if not isinstance(self.detail, str):
            raise ValueError("non_string_detail")
        if self.duration_ms is not None and (
            isinstance(self.duration_ms, bool)
            or not isinstance(self.duration_ms, int)
            or self.duration_ms < 0
        ):
            raise ValueError(f"invalid_duration_ms:{self.duration_ms!r}")


# Type alias for the consumer-supplied callback.
ProgressCallback = Callable[[ProgressEvent], None]


class ProgressEmitter:
    """Thread-safe event sink. Wraps a single callback that delivers events
    to the consumer (typically the Telegram bridge).
    """

    def __init__(self, callback: ProgressCallback) -> None:
        if not callable(callback):
            raise ValueError(f"callback_not_callable:{type(callback).__name__}")
        self._callback = callback

    def emit(self, event: ProgressEvent) -> None:
        """Total — never raises. Callback errors are swallowed silently
        so progress streaming cannot break the pipeline.
        """
        if not isinstance(event, ProgressEvent):
            return
        with contextlib.suppress(Exception):
            self._callback(event)

    # Convenience helpers — same as building ProgressEvent manually but
    # save the caller from importing time.time everywhere.

    def emit_task_started(self, detail: str = "") -> None:
        self.emit(ProgressEvent(
            kind="task_started",
            timestamp=time.time(),
            detail=detail,
        ))

    def emit_agent_started(self, agent_role: str, detail: str = "") -> None:
        self.emit(ProgressEvent(
            kind="agent_started",
            timestamp=time.time(),
            agent_role=agent_role,
            detail=detail,
        ))

    def emit_agent_finished(
        self,
        agent_role: str,
        duration_ms: int,
        detail: str = "",
    ) -> None:
        self.emit(ProgressEvent(
            kind="agent_finished",
            timestamp=time.time(),
            agent_role=agent_role,
            duration_ms=max(0, int(duration_ms)),
            detail=detail,
        ))

    def emit_agent_failed(
        self,
        agent_role: str,
        duration_ms: int,
        error: str,
    ) -> None:
        self.emit(ProgressEvent(
            kind="agent_failed",
            timestamp=time.time(),
            agent_role=agent_role,
            duration_ms=max(0, int(duration_ms)),
            detail=error[:300] if isinstance(error, str) else str(error)[:300],
        ))

    def emit_fsm_transition(self, from_state: str, to_state: str) -> None:
        self.emit(ProgressEvent(
            kind="fsm_transition",
            timestamp=time.time(),
            detail=f"{from_state}->{to_state}",
        ))

    def emit_task_completed(self, summary: str = "") -> None:
        self.emit(ProgressEvent(
            kind="task_completed",
            timestamp=time.time(),
            detail=summary,
        ))

    def emit_task_failed(self, reason: str = "") -> None:
        self.emit(ProgressEvent(
            kind="task_failed",
            timestamp=time.time(),
            detail=reason,
        ))


# Type alias for an agent function in the orchestrator's registry.
AgentFn = Callable[..., str]


def wrap_agent_with_progress(
    agent_role: str,
    fn: AgentFn,
    emitter: ProgressEmitter,
) -> AgentFn:
    """Returns a wrapper that emits started/finished/failed around `fn`.

    The wrapper is transparent: it returns whatever `fn` returns and
    re-raises whatever `fn` raises. Only the progress events are added.
    """
    if not isinstance(agent_role, str) or agent_role not in SELECTABLE_AGENT_ROLES:
        raise ValueError(f"invalid_agent_role:{agent_role!r}")
    if not callable(fn):
        raise ValueError(f"agent_not_callable:{type(fn).__name__}")
    if not isinstance(emitter, ProgressEmitter):
        raise ValueError(f"invalid_emitter:{type(emitter).__name__}")

    def _wrapper(*args, **kwargs):
        emitter.emit_agent_started(agent_role)
        started = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
            emitter.emit_agent_failed(
                agent_role,
                elapsed_ms,
                f"{type(exc).__name__}:{exc}",
            )
            raise
        elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
        emitter.emit_agent_finished(agent_role, elapsed_ms)
        return result

    return _wrapper


def wrap_registry_with_progress(
    registry: Mapping[str, AgentFn],
    emitter: ProgressEmitter,
) -> dict[str, AgentFn]:
    """Returns a new registry where every agent is wrapped with progress
    events. Input registry is NOT mutated.

    Roles outside SELECTABLE_AGENT_ROLES are preserved unwrapped (defensive:
    don't drop unknown roles, just let them through). Roles in
    SELECTABLE_AGENT_ROLES
    that are missing from the input are NOT injected — caller's
    responsibility.
    """
    if not isinstance(registry, Mapping):
        raise ValueError(f"registry_not_mapping:{type(registry).__name__}")
    if not isinstance(emitter, ProgressEmitter):
        raise ValueError(f"invalid_emitter:{type(emitter).__name__}")
    out: dict[str, AgentFn] = {}
    for role, fn in registry.items():
        if role in SELECTABLE_AGENT_ROLES and callable(fn):
            out[role] = wrap_agent_with_progress(role, fn, emitter)
        else:
            out[role] = fn
    return out
