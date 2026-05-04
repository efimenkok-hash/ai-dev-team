import json
from pathlib import Path

import pytest

from core.observability import (
    VALID_LEVELS,
    VALID_UNITS,
    AgentCallRecord,
    AgentPerformance,
    CostSnapshot,
    InMemorySink,
    JsonLinesSink,
    LogRecord,
    MetricSample,
    Observability,
    _is_json_serializable,
    _percentile,
)

# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_percentile_returns_zero_on_empty():
    assert _percentile([], 0.5) == 0.0


def test_percentile_single_element():
    assert _percentile([42.0], 0.95) == 42.0


def test_percentile_p50_of_sorted_values():
    assert _percentile([10.0, 20.0, 30.0, 40.0, 50.0], 0.50) == 30.0


def test_percentile_p95_interpolates():
    # 10, 20, ..., 100 — p95 between 95 and 100.
    values = [float(v) for v in range(10, 110, 10)]
    p95 = _percentile(values, 0.95)
    assert 95.0 <= p95 <= 100.0


def test_percentile_rejects_q_out_of_range():
    with pytest.raises(ValueError, match="percentile_out_of_range"):
        _percentile([1.0, 2.0], 1.5)


def test_is_json_serializable_for_dict():
    assert _is_json_serializable({"k": 1, "v": "x"}) is True


def test_is_json_serializable_rejects_set():
    assert _is_json_serializable({1, 2, 3}) is False


# ---------------------------------------------------------------------------
# in-memory sink basics
# ---------------------------------------------------------------------------


def test_log_record_is_frozen():
    obs = Observability()
    rec = obs.info("started", task="T1")
    assert isinstance(rec, LogRecord)
    with pytest.raises(Exception):
        rec.event = "tampered"  # type: ignore[misc]


def test_metric_sample_is_frozen():
    obs = Observability()
    s = obs.record_metric("latency", 12.5, "ms", agent="pm")
    assert isinstance(s, MetricSample)
    with pytest.raises(Exception):
        s.value = 0.0  # type: ignore[misc]


def test_agent_call_record_is_frozen():
    obs = Observability()
    rec = obs.record_agent_call("pm_agent", "T1", 10, 100, 50, 0.001, True)
    with pytest.raises(Exception):
        rec.ok = False  # type: ignore[misc]


def test_logs_are_filtered_by_level():
    obs = Observability()
    obs.info("a")
    obs.warn("b")
    obs.error("c")
    assert len(obs.logs()) == 3
    assert len(obs.logs(level="WARN")) == 1
    assert obs.logs(level="ERROR")[0].event == "c"


def test_log_rejects_unknown_level():
    obs = Observability()
    with pytest.raises(ValueError, match="unknown_level"):
        obs.log("CRITICAL", "x")


def test_log_rejects_empty_event():
    obs = Observability()
    with pytest.raises(ValueError, match="empty_event"):
        obs.info("   ")


def test_log_rejects_unserializable_payload():
    obs = Observability()
    with pytest.raises(ValueError, match="unserializable_payload"):
        obs.info("x", bad={1, 2})  # set is not JSON serializable


def test_log_payload_is_decoupled_from_caller():
    obs = Observability()
    payload = {"k": 1}
    rec = obs.info("x", **payload)
    payload["k"] = 999  # should not affect recorded payload
    assert rec.payload["k"] == 1


def test_log_levels_constants():
    assert tuple(VALID_LEVELS) == ("DEBUG", "INFO", "WARN", "ERROR")


# ---------------------------------------------------------------------------
# metrics validation
# ---------------------------------------------------------------------------


def test_record_metric_happy_path():
    obs = Observability()
    s = obs.record_metric("agent.latency", 200.0, "ms", agent="pm")
    assert s.name == "agent.latency"
    assert s.value == 200.0
    assert s.unit == "ms"
    assert s.tags["agent"] == "pm"


def test_record_metric_rejects_unknown_unit():
    obs = Observability()
    with pytest.raises(ValueError, match="unknown_unit"):
        obs.record_metric("x", 1, "furlongs")


def test_record_metric_rejects_empty_name():
    obs = Observability()
    with pytest.raises(ValueError, match="empty_metric_name"):
        obs.record_metric("   ", 1, "count")


def test_record_metric_rejects_non_numeric_value():
    obs = Observability()
    with pytest.raises(ValueError, match="non_numeric_value"):
        obs.record_metric("x", "abc", "count")  # type: ignore[arg-type]


def test_record_metric_rejects_bool_as_value():
    obs = Observability()
    with pytest.raises(ValueError, match="non_numeric_value"):
        obs.record_metric("x", True, "count")  # type: ignore[arg-type]


def test_record_metric_rejects_non_string_tag():
    obs = Observability()
    with pytest.raises(ValueError, match="non_string_tag"):
        obs.record_metric("x", 1, "count", agent=123)  # type: ignore[arg-type]


def test_metrics_filter_by_name():
    obs = Observability()
    obs.record_metric("a", 1, "count")
    obs.record_metric("b", 2, "count")
    obs.record_metric("a", 3, "count")
    assert len(obs.metrics()) == 3
    assert len(obs.metrics(name="a")) == 2


def test_valid_units_constant():
    assert tuple(VALID_UNITS) == ("ms", "tokens", "usd", "count", "bytes", "ratio")


# ---------------------------------------------------------------------------
# agent call validation
# ---------------------------------------------------------------------------


def test_record_agent_call_happy_path():
    obs = Observability()
    rec = obs.record_agent_call(
        agent_name="pm_agent",
        task_id="T1",
        duration_ms=120,
        input_tokens=80,
        output_tokens=40,
        cost_usd=0.001,
        ok=True,
    )
    assert isinstance(rec, AgentCallRecord)
    assert rec.ok is True
    assert rec.error is None


def test_record_agent_call_failure_requires_error():
    obs = Observability()
    rec = obs.record_agent_call(
        "pm_agent", "T1", 100, 10, 10, 0.0001, ok=False, error="timeout"
    )
    assert rec.ok is False
    assert rec.error == "timeout"


def test_record_agent_call_rejects_negative_duration():
    obs = Observability()
    with pytest.raises(ValueError, match="invalid_duration_ms"):
        obs.record_agent_call("pm", "T", -1, 0, 0, 0.0, True)


@pytest.mark.parametrize("kwarg,bad", [
    ("input_tokens", -1),
    ("output_tokens", -5),
    ("cost_usd", -0.001),
])
def test_record_agent_call_rejects_negative_numeric(kwarg, bad):
    obs = Observability()
    base = dict(
        agent_name="pm",
        task_id="T",
        duration_ms=10,
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
        ok=True,
    )
    base[kwarg] = bad
    with pytest.raises(ValueError, match=r"invalid_(duration_ms|input_tokens|output_tokens|cost_usd)"):
        obs.record_agent_call(**base)


def test_record_agent_call_rejects_ok_with_error():
    obs = Observability()
    with pytest.raises(ValueError, match="ok_with_error"):
        obs.record_agent_call("pm", "T", 1, 0, 0, 0.0, ok=True, error="x")


def test_record_agent_call_rejects_not_ok_without_error():
    obs = Observability()
    with pytest.raises(ValueError, match="not_ok_without_error"):
        obs.record_agent_call("pm", "T", 1, 0, 0, 0.0, ok=False)


def test_record_agent_call_rejects_empty_agent_name():
    obs = Observability()
    with pytest.raises(ValueError, match="empty_agent_name"):
        obs.record_agent_call("  ", "T", 1, 0, 0, 0.0, True)


def test_record_agent_call_rejects_empty_task_id():
    obs = Observability()
    with pytest.raises(ValueError, match="empty_task_id"):
        obs.record_agent_call("pm", "  ", 1, 0, 0, 0.0, True)


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------


def test_agent_calls_filter_by_name_and_task():
    obs = Observability()
    obs.record_agent_call("pm_agent", "T1", 1, 1, 1, 0.0001, True)
    obs.record_agent_call("pm_agent", "T2", 1, 1, 1, 0.0001, True)
    obs.record_agent_call("writer_agent", "T1", 1, 1, 1, 0.0001, True)
    assert len(obs.agent_calls()) == 3
    assert len(obs.agent_calls(agent_name="pm_agent")) == 2
    assert len(obs.agent_calls(task_id="T1")) == 2
    assert len(obs.agent_calls(agent_name="writer_agent", task_id="T1")) == 1


def test_agent_performance_empty_for_unknown_agent():
    obs = Observability()
    perf = obs.agent_performance("never_called")
    assert perf.total_calls == 0
    assert perf.error_rate == 0.0


def test_agent_performance_aggregates_correctly():
    obs = Observability()
    for ms in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
        obs.record_agent_call("pm_agent", "T", ms, 1, 1, 0.0001, True)
    obs.record_agent_call("pm_agent", "T", 5, 1, 1, 0.0001, ok=False, error="boom")
    perf = obs.agent_performance("pm_agent")
    assert perf.total_calls == 11
    assert perf.success_count == 10
    assert perf.error_count == 1
    assert pytest.approx(perf.error_rate, rel=1e-6) == 1 / 11
    assert perf.p50_duration_ms <= perf.p95_duration_ms
    assert perf.p95_duration_ms <= perf.p99_duration_ms


def test_cost_snapshot_aggregates_total_and_breakdown():
    obs = Observability()
    obs.record_agent_call("pm", "T1", 1, 100, 50, 0.001, True)
    obs.record_agent_call("pm", "T2", 1, 200, 100, 0.002, True)
    obs.record_agent_call("writer", "T1", 1, 50, 20, 0.0005, True)
    snap = obs.cost_snapshot()
    assert snap.total_calls == 3
    assert pytest.approx(snap.total_usd) == 0.0035
    assert pytest.approx(snap.by_agent["pm"]) == 0.003
    assert pytest.approx(snap.by_agent["writer"]) == 0.0005
    assert pytest.approx(snap.by_task["T1"]) == 0.0015
    assert snap.total_input_tokens == 350
    assert snap.total_output_tokens == 170


def test_cost_snapshot_filtered_by_task_id():
    obs = Observability()
    obs.record_agent_call("pm", "T1", 1, 1, 1, 0.001, True)
    obs.record_agent_call("pm", "T2", 1, 1, 1, 0.002, True)
    snap = obs.cost_snapshot(task_id="T1")
    assert snap.total_calls == 1
    assert pytest.approx(snap.total_usd) == 0.001


def test_cost_snapshot_empty_observability():
    obs = Observability()
    snap = obs.cost_snapshot()
    assert snap.total_calls == 0
    assert snap.total_usd == 0.0
    assert snap.by_agent == {}


def test_logs_query_requires_in_memory_sink(tmp_path: Path):
    sink = JsonLinesSink(tmp_path / "log.jsonl")
    obs = Observability(sink=sink)
    obs.info("hello")
    with pytest.raises(RuntimeError, match="logs_requires_in_memory_sink"):
        obs.logs()


def test_metrics_query_requires_in_memory_sink(tmp_path: Path):
    sink = JsonLinesSink(tmp_path / "log.jsonl")
    obs = Observability(sink=sink)
    obs.record_metric("x", 1, "count")
    with pytest.raises(RuntimeError, match="metrics_requires_in_memory_sink"):
        obs.metrics()


def test_cost_snapshot_requires_in_memory_sink(tmp_path: Path):
    sink = JsonLinesSink(tmp_path / "log.jsonl")
    obs = Observability(sink=sink)
    with pytest.raises(RuntimeError, match="cost_snapshot_requires_in_memory_sink"):
        obs.cost_snapshot()


def test_agent_performance_requires_in_memory_sink(tmp_path: Path):
    sink = JsonLinesSink(tmp_path / "log.jsonl")
    obs = Observability(sink=sink)
    with pytest.raises(RuntimeError, match="agent_performance_requires_in_memory_sink"):
        obs.agent_performance("pm")


# ---------------------------------------------------------------------------
# JsonLinesSink
# ---------------------------------------------------------------------------


def test_json_lines_sink_writes_log_record(tmp_path: Path):
    path = tmp_path / "logs/run.jsonl"
    sink = JsonLinesSink(path)
    obs = Observability(sink=sink)
    obs.info("started", task="T1", iter=1)
    contents = path.read_text(encoding="utf-8").strip()
    assert contents.count("\n") == 0
    parsed = json.loads(contents)
    assert parsed["ch"] == "log"
    assert parsed["level"] == "INFO"
    assert parsed["event"] == "started"
    assert parsed["payload"] == {"task": "T1", "iter": 1}


def test_json_lines_sink_writes_three_channels(tmp_path: Path):
    path = tmp_path / "all.jsonl"
    sink = JsonLinesSink(path)
    obs = Observability(sink=sink)
    obs.info("a")
    obs.record_metric("m", 1.0, "count")
    obs.record_agent_call("pm", "T1", 5, 1, 1, 0.0, True)
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").strip().splitlines()]
    channels = [ln["ch"] for ln in lines]
    assert channels == ["log", "metric", "agent_call"]


def test_json_lines_sink_creates_parent_dir(tmp_path: Path):
    path = tmp_path / "deep/nested/dir/log.jsonl"
    JsonLinesSink(path)  # should not raise
    assert path.parent.is_dir()


def test_json_lines_sink_path_property(tmp_path: Path):
    p = tmp_path / "x.jsonl"
    sink = JsonLinesSink(p)
    assert sink.path == p


# ---------------------------------------------------------------------------
# defaults / constants
# ---------------------------------------------------------------------------


def test_default_sink_is_in_memory():
    obs = Observability()
    assert isinstance(obs.sink, InMemorySink)


def test_cost_snapshot_is_frozen():
    obs = Observability()
    snap = obs.cost_snapshot()
    assert isinstance(snap, CostSnapshot)
    with pytest.raises(Exception):
        snap.total_usd = 99.0  # type: ignore[misc]


def test_agent_performance_is_frozen():
    obs = Observability()
    perf = obs.agent_performance("never")
    assert isinstance(perf, AgentPerformance)
    with pytest.raises(Exception):
        perf.total_calls = 99  # type: ignore[misc]


def test_timestamps_are_iso_utc():
    obs = Observability()
    rec = obs.info("e")
    # ISO 8601 with timezone offset (+00:00 or Z)
    assert "T" in rec.timestamp
    assert rec.timestamp.endswith("+00:00") or rec.timestamp.endswith("Z")
