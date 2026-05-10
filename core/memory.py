"""
core/memory.py

Pipeline memory store. In-memory, deterministic, immutable artifacts.

CONTRACTS:
1. task_id уникален; повторный new_task -> ValueError.
2. set_artifact на несуществующий task_id -> KeyError.
3. Артефакт иммутабелен; повторный set_artifact той же kind -> ValueError.
4. record_transition проверяется через core.fsm.can_transition.
5. snapshot возвращает frozen-копию; внешние мутации не видны.
6. increment_loop возвращает новое значение монотонного счётчика.
7. dump_task / restore_task — JSON-ready сериализация одного task'а; формат
   стабилен и обратим без потерь.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from core.contracts import validate_non_empty_text
from core.fsm import State, can_transition

VALID_ARTIFACT_KINDS: frozenset[str] = frozenset({
    "project_brief",
    "planning",
    "pm",
    "architect",
    "writer",
    "review",
    "test",
    "qa",
    "fix",
})

VALID_LOOPS: frozenset[str] = frozenset({
    "review_fix",
    "test_fix",
    "qa_fix",
})


@dataclass(frozen=True)
class TransitionRecord:
    from_state: State
    to_state: State


@dataclass(frozen=True)
class Snapshot:
    task_id: str
    raw_task: str
    artifacts: Mapping[str, str]
    transitions: tuple[TransitionRecord, ...]
    agent_calls: tuple[str, ...]
    loop_counters: Mapping[str, int]


@dataclass
class _TaskState:
    raw_task: str
    artifacts: dict[str, str] = field(default_factory=dict)
    transitions: list[TransitionRecord] = field(default_factory=list)
    agent_calls: list[str] = field(default_factory=list)
    loop_counters: dict[str, int] = field(default_factory=dict)


def _normalize_artifact_kind(kind: str) -> str:
    if not isinstance(kind, str):
        raise ValueError(f"invalid_artifact_kind_type:{type(kind).__name__}")
    if kind not in VALID_ARTIFACT_KINDS:
        raise ValueError(f"unknown_artifact_kind:{kind}")
    return kind


def _normalize_artifact_payload(payload: str) -> str:
    validation = validate_non_empty_text(payload, "payload")
    if not validation.ok:
        raise ValueError(";".join(validation.violations))
    return payload


def normalize_artifact_seed_mapping(
    artifacts: Mapping[str, str] | None,
) -> dict[str, str]:
    if artifacts is None:
        return {}
    if not isinstance(artifacts, Mapping):
        raise ValueError(
            f"invalid_initial_artifacts_type:{type(artifacts).__name__}"
        )
    normalized: dict[str, str] = {}
    for kind, payload in artifacts.items():
        normalized[_normalize_artifact_kind(kind)] = _normalize_artifact_payload(
            payload
        )
    return normalized


class PipelineMemory:
    def __init__(self) -> None:
        self._tasks: dict[str, _TaskState] = {}

    def new_task(self, task_id: str, raw_task: str) -> None:
        if not task_id or not task_id.strip():
            raise ValueError("empty_task_id")

        validation = validate_non_empty_text(raw_task, "raw_task")
        if not validation.ok:
            raise ValueError(";".join(validation.violations))

        if task_id in self._tasks:
            raise ValueError(f"task_already_exists:{task_id}")

        self._tasks[task_id] = _TaskState(raw_task=raw_task)

    def set_artifact(self, task_id: str, kind: str, payload: str) -> None:
        kind = _normalize_artifact_kind(kind)
        payload = _normalize_artifact_payload(payload)

        task = self._require_task(task_id)

        if kind in task.artifacts:
            raise ValueError(f"artifact_already_set:{kind}")

        task.artifacts[kind] = payload

    def get_artifact(self, task_id: str, kind: str) -> str | None:
        kind = _normalize_artifact_kind(kind)

        task = self._require_task(task_id)
        return task.artifacts.get(kind)

    def record_transition(
        self,
        task_id: str,
        from_state: State,
        to_state: State,
    ) -> None:
        if not can_transition(from_state, to_state):
            raise ValueError(
                f"invalid_transition:{from_state.value}->{to_state.value}"
            )

        task = self._require_task(task_id)
        task.transitions.append(
            TransitionRecord(from_state=from_state, to_state=to_state)
        )

    def record_agent_call(self, task_id: str, agent: str) -> None:
        if not agent or not agent.strip():
            raise ValueError("empty_agent_name")

        task = self._require_task(task_id)
        task.agent_calls.append(agent)

    def increment_loop(self, task_id: str, loop: str) -> int:
        if loop not in VALID_LOOPS:
            raise ValueError(f"unknown_loop:{loop}")

        task = self._require_task(task_id)
        new_value = task.loop_counters.get(loop, 0) + 1
        task.loop_counters[loop] = new_value
        return new_value

    def get_loop(self, task_id: str, loop: str) -> int:
        if loop not in VALID_LOOPS:
            raise ValueError(f"unknown_loop:{loop}")

        task = self._require_task(task_id)
        return task.loop_counters.get(loop, 0)

    def agent_calls_count(self, task_id: str) -> int:
        task = self._require_task(task_id)
        return len(task.agent_calls)

    def transitions_count(self, task_id: str) -> int:
        task = self._require_task(task_id)
        return len(task.transitions)

    def snapshot(self, task_id: str) -> Snapshot:
        task = self._require_task(task_id)

        return Snapshot(
            task_id=task_id,
            raw_task=task.raw_task,
            artifacts=MappingProxyType(dict(task.artifacts)),
            transitions=tuple(task.transitions),
            agent_calls=tuple(task.agent_calls),
            loop_counters=MappingProxyType(dict(task.loop_counters)),
        )

    def list_tasks(self) -> list[str]:
        return sorted(self._tasks.keys())

    def dump_task(self, task_id: str) -> dict[str, Any]:
        snap = self.snapshot(task_id)
        return {
            "schema_version": 1,
            "task_id": snap.task_id,
            "raw_task": snap.raw_task,
            "artifacts": dict(snap.artifacts),
            "transitions": [
                {"from_state": t.from_state.value, "to_state": t.to_state.value}
                for t in snap.transitions
            ],
            "agent_calls": list(snap.agent_calls),
            "loop_counters": dict(snap.loop_counters),
        }

    def restore_task(self, dump: Mapping[str, Any]) -> str:
        if not isinstance(dump, Mapping):
            raise ValueError("invalid_dump_type")
        if dump.get("schema_version") != 1:
            raise ValueError(f"unsupported_schema_version:{dump.get('schema_version')}")
        for required_key in ("task_id", "raw_task", "artifacts", "transitions",
                             "agent_calls", "loop_counters"):
            if required_key not in dump:
                raise ValueError(f"missing_dump_key:{required_key}")

        task_id = dump["task_id"]
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("empty_task_id")
        if task_id in self._tasks:
            raise ValueError(f"task_already_exists:{task_id}")

        raw_task = dump["raw_task"]
        validation = validate_non_empty_text(raw_task, "raw_task")
        if not validation.ok:
            raise ValueError(";".join(validation.violations))

        artifacts = normalize_artifact_seed_mapping(dict(dump["artifacts"]))

        loop_counters: dict[str, int] = {}
        for k, v in dict(dump["loop_counters"]).items():
            if k not in VALID_LOOPS:
                raise ValueError(f"unknown_loop:{k}")
            if not isinstance(v, int) or v < 0:
                raise ValueError(f"invalid_loop_counter:{k}={v}")
            loop_counters[k] = v

        transitions: list[TransitionRecord] = []
        for entry in dump["transitions"]:
            if not isinstance(entry, Mapping):
                raise ValueError("invalid_transition_entry")
            if "from_state" not in entry or "to_state" not in entry:
                raise ValueError("incomplete_transition_entry")
            try:
                from_state = State(entry["from_state"])
                to_state = State(entry["to_state"])
            except ValueError as exc:
                raise ValueError(f"unknown_state:{exc}") from exc
            if not can_transition(from_state, to_state):
                raise ValueError(
                    f"invalid_transition:{from_state.value}->{to_state.value}"
                )
            transitions.append(
                TransitionRecord(from_state=from_state, to_state=to_state)
            )

        agent_calls = [str(a) for a in dump["agent_calls"]]
        for a in agent_calls:
            if not a.strip():
                raise ValueError("empty_agent_name")

        state = _TaskState(raw_task=raw_task)
        state.artifacts = artifacts
        state.transitions = transitions
        state.agent_calls = agent_calls
        state.loop_counters = loop_counters
        self._tasks[task_id] = state
        return task_id

    def _require_task(self, task_id: str) -> _TaskState:
        if task_id not in self._tasks:
            raise KeyError(f"unknown_task:{task_id}")
        return self._tasks[task_id]
