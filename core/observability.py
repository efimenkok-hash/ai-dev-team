"""
core/observability.py

Step 11 of the ULTRA spec: structured logging, metrics, agent-performance
tracking, and cost tracking. Designed to be plugged into the orchestrator
(optional dependency) so an autonomous run can be observed, replayed and
budget-controlled.

CONTRACTS:
1. All public dataclasses are frozen — observers cannot mutate records after
   the fact.
2. All timestamps are produced via datetime.now(timezone.utc) and formatted
   as ISO 8601 strings; no naive datetimes leak into records.
3. Log levels are restricted to VALID_LEVELS; metric units to VALID_UNITS.
4. Log payloads must be JSON-serializable; non-serializable input is rejected
   with ValueError at the call site (not silently dropped).
5. Numeric fields on AgentCallRecord (duration_ms, tokens, cost_usd) cannot
   be negative.
6. ok=True forbids a non-empty error; ok=False requires a non-empty error.
   This prevents inconsistent records from accumulating.
7. Sinks are pluggable via the LogSink interface; InMemorySink is the
   default and is the only sink that supports query operations
   (cost_snapshot, agent_performance, logs(), metrics()). Other sinks raise
   RuntimeError on query — explicit, no silent fallback.
8. JsonLinesSink uses an in-process lock around append-writes; it is safe
   for multi-threaded sinking inside one process.
"""

import json
import math
import threading
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALID_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")
VALID_UNITS = ("ms", "tokens", "usd", "count", "bytes", "ratio")


@dataclass(frozen=True)
class LogRecord:
    timestamp: str
    level: str
    event: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class MetricSample:
    timestamp: str
    name: str
    value: float
    unit: str
    tags: dict[str, str]


@dataclass(frozen=True)
class AgentCallRecord:
    timestamp: str
    agent_name: str
    task_id: str
    duration_ms: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    ok: bool
    error: str | None


@dataclass(frozen=True)
class AgentPerformance:
    agent_name: str
    total_calls: int
    success_count: int
    error_count: int
    error_rate: float
    avg_duration_ms: float
    p50_duration_ms: float
    p95_duration_ms: float
    p99_duration_ms: float


@dataclass(frozen=True)
class CostSnapshot:
    total_usd: float
    by_agent: dict[str, float]
    by_task: dict[str, float]
    total_input_tokens: int
    total_output_tokens: int
    total_calls: int


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------


class LogSink(ABC):
    @abstractmethod
    def write_log(self, record: LogRecord) -> None: ...

    @abstractmethod
    def write_metric(self, sample: MetricSample) -> None: ...

    @abstractmethod
    def write_agent_call(self, record: AgentCallRecord) -> None: ...


class InMemorySink(LogSink):
    def __init__(self) -> None:
        self.logs: list[LogRecord] = []
        self.metrics: list[MetricSample] = []
        self.calls: list[AgentCallRecord] = []

    def write_log(self, record: LogRecord) -> None:
        self.logs.append(record)

    def write_metric(self, sample: MetricSample) -> None:
        self.metrics.append(sample)

    def write_agent_call(self, record: AgentCallRecord) -> None:
        self.calls.append(record)


class JsonLinesSink(LogSink):
    """Append-only JSONL sink. Each line is a single JSON object with a 'ch'
    field discriminating channels: log/metric/agent_call.
    """

    def __init__(self, path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def _append(self, channel: str, payload: dict) -> None:
        line = json.dumps(
            {"ch": channel, **payload},
            ensure_ascii=False,
            default=str,
        )
        with self._lock, self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def write_log(self, record: LogRecord) -> None:
        self._append("log", asdict(record))

    def write_metric(self, sample: MetricSample) -> None:
        self._append("metric", asdict(sample))

    def write_agent_call(self, record: AgentCallRecord) -> None:
        self._append("agent_call", asdict(record))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"percentile_out_of_range:{q}")
    n = len(sorted_values)
    if n == 1:
        return float(sorted_values[0])
    k = (n - 1) * q
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(sorted_values[int(k)])
    return float(
        sorted_values[int(f)] * (c - k) + sorted_values[int(c)] * (k - f)
    )


def _is_json_serializable(value: Any) -> bool:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


class Observability:
    def __init__(self, sink: LogSink | None = None) -> None:
        self._sink: LogSink = sink if sink is not None else InMemorySink()

    @property
    def sink(self) -> LogSink:
        return self._sink

    # ----- logging ---------------------------------------------------------

    def log(self, level: str, event: str, **payload: Any) -> LogRecord:
        if level not in VALID_LEVELS:
            raise ValueError(f"unknown_level:{level}")
        if not isinstance(event, str) or not event.strip():
            raise ValueError("empty_event")
        if not _is_json_serializable(payload):
            raise ValueError("unserializable_payload")
        record = LogRecord(
            timestamp=_now_utc_iso(),
            level=level,
            event=event.strip(),
            payload=dict(payload),
        )
        self._sink.write_log(record)
        return record

    def debug(self, event: str, **payload: Any) -> LogRecord:
        return self.log("DEBUG", event, **payload)

    def info(self, event: str, **payload: Any) -> LogRecord:
        return self.log("INFO", event, **payload)

    def warn(self, event: str, **payload: Any) -> LogRecord:
        return self.log("WARN", event, **payload)

    def error(self, event: str, **payload: Any) -> LogRecord:
        return self.log("ERROR", event, **payload)

    # ----- metrics ---------------------------------------------------------

    def record_metric(
        self,
        name: str,
        value: float,
        unit: str,
        **tags: str,
    ) -> MetricSample:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("empty_metric_name")
        if unit not in VALID_UNITS:
            raise ValueError(f"unknown_unit:{unit}")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("non_numeric_value")
        for tag_key, tag_val in tags.items():
            if not isinstance(tag_val, str):
                raise ValueError(f"non_string_tag:{tag_key}")
        sample = MetricSample(
            timestamp=_now_utc_iso(),
            name=name.strip(),
            value=float(value),
            unit=unit,
            tags=dict(tags),
        )
        self._sink.write_metric(sample)
        return sample

    # ----- agent calls -----------------------------------------------------

    def record_agent_call(
        self,
        agent_name: str,
        task_id: str,
        duration_ms: int,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        ok: bool,
        error: str | None = None,
    ) -> AgentCallRecord:
        if not isinstance(agent_name, str) or not agent_name.strip():
            raise ValueError("empty_agent_name")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("empty_task_id")
        if not isinstance(duration_ms, int) or duration_ms < 0:
            raise ValueError(f"invalid_duration_ms:{duration_ms}")
        if not isinstance(input_tokens, int) or input_tokens < 0:
            raise ValueError(f"invalid_input_tokens:{input_tokens}")
        if not isinstance(output_tokens, int) or output_tokens < 0:
            raise ValueError(f"invalid_output_tokens:{output_tokens}")
        if not isinstance(cost_usd, (int, float)) or cost_usd < 0:
            raise ValueError(f"invalid_cost_usd:{cost_usd}")
        if ok and error:
            raise ValueError("ok_with_error")
        if not ok and (not isinstance(error, str) or not error.strip()):
            raise ValueError("not_ok_without_error")
        record = AgentCallRecord(
            timestamp=_now_utc_iso(),
            agent_name=agent_name.strip(),
            task_id=task_id.strip(),
            duration_ms=duration_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=float(cost_usd),
            ok=ok,
            error=error,
        )
        self._sink.write_agent_call(record)
        return record

    # ----- queries (require InMemorySink) ----------------------------------

    def _require_memory_sink(self, op: str) -> InMemorySink:
        if not isinstance(self._sink, InMemorySink):
            raise RuntimeError(f"{op}_requires_in_memory_sink")
        return self._sink

    def logs(self, level: str | None = None) -> tuple[LogRecord, ...]:
        sink = self._require_memory_sink("logs")
        records = sink.logs
        if level is not None:
            if level not in VALID_LEVELS:
                raise ValueError(f"unknown_level:{level}")
            records = [r for r in records if r.level == level]
        return tuple(records)

    def metrics(self, name: str | None = None) -> tuple[MetricSample, ...]:
        sink = self._require_memory_sink("metrics")
        records = sink.metrics
        if name is not None:
            records = [r for r in records if r.name == name]
        return tuple(records)

    def agent_calls(
        self,
        agent_name: str | None = None,
        task_id: str | None = None,
    ) -> tuple[AgentCallRecord, ...]:
        sink = self._require_memory_sink("agent_calls")
        records = sink.calls
        if agent_name is not None:
            records = [r for r in records if r.agent_name == agent_name]
        if task_id is not None:
            records = [r for r in records if r.task_id == task_id]
        return tuple(records)

    def agent_performance(self, agent_name: str) -> AgentPerformance:
        if not isinstance(agent_name, str) or not agent_name.strip():
            raise ValueError("empty_agent_name")
        sink = self._require_memory_sink("agent_performance")
        calls = [c for c in sink.calls if c.agent_name == agent_name]
        if not calls:
            return AgentPerformance(
                agent_name=agent_name,
                total_calls=0,
                success_count=0,
                error_count=0,
                error_rate=0.0,
                avg_duration_ms=0.0,
                p50_duration_ms=0.0,
                p95_duration_ms=0.0,
                p99_duration_ms=0.0,
            )
        durations = sorted(float(c.duration_ms) for c in calls)
        success_count = sum(1 for c in calls if c.ok)
        error_count = len(calls) - success_count
        return AgentPerformance(
            agent_name=agent_name,
            total_calls=len(calls),
            success_count=success_count,
            error_count=error_count,
            error_rate=error_count / len(calls),
            avg_duration_ms=sum(durations) / len(durations),
            p50_duration_ms=_percentile(durations, 0.50),
            p95_duration_ms=_percentile(durations, 0.95),
            p99_duration_ms=_percentile(durations, 0.99),
        )

    def cost_snapshot(self, task_id: str | None = None) -> CostSnapshot:
        sink = self._require_memory_sink("cost_snapshot")
        calls = sink.calls
        if task_id is not None:
            calls = [c for c in calls if c.task_id == task_id]
        by_agent: dict[str, float] = {}
        by_task: dict[str, float] = {}
        total_usd = 0.0
        total_in = 0
        total_out = 0
        for c in calls:
            total_usd += c.cost_usd
            total_in += c.input_tokens
            total_out += c.output_tokens
            by_agent[c.agent_name] = by_agent.get(c.agent_name, 0.0) + c.cost_usd
            by_task[c.task_id] = by_task.get(c.task_id, 0.0) + c.cost_usd
        return CostSnapshot(
            total_usd=total_usd,
            by_agent=dict(by_agent),
            by_task=dict(by_task),
            total_input_tokens=total_in,
            total_output_tokens=total_out,
            total_calls=len(calls),
        )
