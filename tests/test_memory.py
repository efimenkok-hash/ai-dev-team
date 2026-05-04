import pytest

from core.fsm import State
from core.memory import (
    VALID_ARTIFACT_KINDS,
    VALID_LOOPS,
    PipelineMemory,
    Snapshot,
    TransitionRecord,
)


def _seeded() -> PipelineMemory:
    m = PipelineMemory()
    m.new_task("T1", "raw task text")
    return m


def test_new_task_creates_state():
    m = PipelineMemory()
    m.new_task("T1", "raw task")
    assert m.list_tasks() == ["T1"]


def test_new_task_rejects_duplicate_task_id():
    m = _seeded()
    with pytest.raises(ValueError, match="task_already_exists:T1"):
        m.new_task("T1", "another raw")


def test_new_task_rejects_empty_task_id():
    m = PipelineMemory()
    with pytest.raises(ValueError, match="empty_task_id"):
        m.new_task("", "raw")


def test_new_task_rejects_whitespace_task_id():
    m = PipelineMemory()
    with pytest.raises(ValueError, match="empty_task_id"):
        m.new_task("   ", "raw")


def test_new_task_rejects_empty_raw_task():
    m = PipelineMemory()
    with pytest.raises(ValueError, match="empty_field:raw_task"):
        m.new_task("T1", "   ")


def test_set_and_get_artifact_happy_path():
    m = _seeded()
    m.set_artifact("T1", "pm", '{"plan_id":"x"}')
    assert m.get_artifact("T1", "pm") == '{"plan_id":"x"}'


def test_all_artifact_kinds_round_trip():
    m = _seeded()
    for kind in VALID_ARTIFACT_KINDS:
        m.set_artifact("T1", kind, f"payload-{kind}")
    for kind in VALID_ARTIFACT_KINDS:
        assert m.get_artifact("T1", kind) == f"payload-{kind}"


def test_set_artifact_rejects_unknown_kind():
    m = _seeded()
    with pytest.raises(ValueError, match="unknown_artifact_kind:hacker"):
        m.set_artifact("T1", "hacker", "x")


def test_set_artifact_rejects_unknown_task():
    m = PipelineMemory()
    with pytest.raises(KeyError, match="unknown_task:T999"):
        m.set_artifact("T999", "pm", "x")


def test_set_artifact_rejects_empty_payload():
    m = _seeded()
    with pytest.raises(ValueError, match="empty_field:payload"):
        m.set_artifact("T1", "pm", "")


def test_set_artifact_rejects_whitespace_payload():
    m = _seeded()
    with pytest.raises(ValueError, match="empty_field:payload"):
        m.set_artifact("T1", "pm", "   \n\t  ")


def test_set_artifact_is_immutable():
    m = _seeded()
    m.set_artifact("T1", "pm", "first")
    with pytest.raises(ValueError, match="artifact_already_set:pm"):
        m.set_artifact("T1", "pm", "second")
    assert m.get_artifact("T1", "pm") == "first"


def test_get_artifact_returns_none_when_missing():
    m = _seeded()
    assert m.get_artifact("T1", "pm") is None


def test_get_artifact_rejects_unknown_kind():
    m = _seeded()
    with pytest.raises(ValueError, match="unknown_artifact_kind:foo"):
        m.get_artifact("T1", "foo")


def test_get_artifact_rejects_unknown_task():
    m = PipelineMemory()
    with pytest.raises(KeyError, match="unknown_task:nope"):
        m.get_artifact("nope", "pm")


def test_record_transition_happy_path():
    m = _seeded()
    m.record_transition("T1", State.IDLE, State.PLANNING)
    snap = m.snapshot("T1")
    assert snap.transitions == (
        TransitionRecord(from_state=State.IDLE, to_state=State.PLANNING),
    )


def test_record_transition_rejects_invalid_jump():
    m = _seeded()
    with pytest.raises(ValueError, match="invalid_transition:IDLE->WRITER"):
        m.record_transition("T1", State.IDLE, State.WRITER)


def test_record_transition_rejects_terminal_origin():
    m = _seeded()
    with pytest.raises(ValueError, match="invalid_transition:SUCCESS->IDLE"):
        m.record_transition("T1", State.SUCCESS, State.IDLE)


def test_record_agent_call_appends_in_order():
    m = _seeded()
    m.record_agent_call("T1", "planning_agent")
    m.record_agent_call("T1", "pm_agent")
    snap = m.snapshot("T1")
    assert snap.agent_calls == ("planning_agent", "pm_agent")
    assert m.agent_calls_count("T1") == 2


def test_record_agent_call_rejects_empty():
    m = _seeded()
    with pytest.raises(ValueError, match="empty_agent_name"):
        m.record_agent_call("T1", "")


def test_record_agent_call_rejects_whitespace():
    m = _seeded()
    with pytest.raises(ValueError, match="empty_agent_name"):
        m.record_agent_call("T1", "   ")


def test_increment_loop_monotonic():
    m = _seeded()
    assert m.increment_loop("T1", "review_fix") == 1
    assert m.increment_loop("T1", "review_fix") == 2
    assert m.increment_loop("T1", "review_fix") == 3
    assert m.get_loop("T1", "review_fix") == 3


def test_increment_loop_isolated_per_loop():
    m = _seeded()
    assert m.increment_loop("T1", "review_fix") == 1
    assert m.increment_loop("T1", "test_fix") == 1
    assert m.increment_loop("T1", "qa_fix") == 1
    assert m.increment_loop("T1", "review_fix") == 2
    assert m.get_loop("T1", "test_fix") == 1


def test_increment_loop_rejects_unknown():
    m = _seeded()
    with pytest.raises(ValueError, match="unknown_loop:zzz"):
        m.increment_loop("T1", "zzz")


def test_get_loop_default_zero():
    m = _seeded()
    for loop in VALID_LOOPS:
        assert m.get_loop("T1", loop) == 0


def test_snapshot_artifacts_is_readonly_mapping():
    m = _seeded()
    m.set_artifact("T1", "pm", "v1")
    snap = m.snapshot("T1")
    with pytest.raises(TypeError):
        snap.artifacts["pm"] = "tampered"


def test_snapshot_loop_counters_readonly():
    m = _seeded()
    m.increment_loop("T1", "review_fix")
    snap = m.snapshot("T1")
    with pytest.raises(TypeError):
        snap.loop_counters["review_fix"] = 99


def test_snapshot_decouples_from_internal_state():
    m = _seeded()
    m.set_artifact("T1", "pm", "v1")
    snap = m.snapshot("T1")
    m.set_artifact("T1", "architect", "v2")
    assert "architect" not in snap.artifacts
    assert snap.artifacts["pm"] == "v1"


def test_snapshot_transitions_decoupled():
    m = _seeded()
    m.record_transition("T1", State.IDLE, State.PLANNING)
    snap = m.snapshot("T1")
    m.record_transition("T1", State.PLANNING, State.PM)
    assert len(snap.transitions) == 1


def test_snapshot_agent_calls_decoupled():
    m = _seeded()
    m.record_agent_call("T1", "planning_agent")
    snap = m.snapshot("T1")
    m.record_agent_call("T1", "pm_agent")
    assert snap.agent_calls == ("planning_agent",)


def test_snapshot_unknown_task_raises():
    m = PipelineMemory()
    with pytest.raises(KeyError, match="unknown_task:nope"):
        m.snapshot("nope")


def test_snapshot_is_frozen_dataclass():
    m = _seeded()
    snap = m.snapshot("T1")
    assert isinstance(snap, Snapshot)
    with pytest.raises(Exception):
        snap.task_id = "tampered"


def test_list_tasks_sorted_alphabetically():
    m = PipelineMemory()
    m.new_task("B", "x")
    m.new_task("A", "y")
    m.new_task("C", "z")
    assert m.list_tasks() == ["A", "B", "C"]


def test_transitions_count_tracks_history():
    m = _seeded()
    assert m.transitions_count("T1") == 0
    m.record_transition("T1", State.IDLE, State.PLANNING)
    m.record_transition("T1", State.PLANNING, State.PM)
    assert m.transitions_count("T1") == 2


def test_dump_task_round_trips_through_restore():
    src = PipelineMemory()
    src.new_task("T1", "raw text here")
    src.set_artifact("T1", "pm", '{"plan_id":"x"}')
    src.set_artifact("T1", "architect", '{"arch_id":"a"}')
    src.record_transition("T1", State.IDLE, State.PLANNING)
    src.record_transition("T1", State.PLANNING, State.PM)
    src.record_agent_call("T1", "planning_agent")
    src.record_agent_call("T1", "pm_agent")
    src.increment_loop("T1", "review_fix")
    src.increment_loop("T1", "review_fix")

    dump = src.dump_task("T1")
    # JSON roundtrip-safe (no non-serializable types).
    import json
    encoded = json.dumps(dump)
    decoded = json.loads(encoded)

    dst = PipelineMemory()
    restored_id = dst.restore_task(decoded)
    assert restored_id == "T1"

    s_src = src.snapshot("T1")
    s_dst = dst.snapshot("T1")
    assert s_src.raw_task == s_dst.raw_task
    assert dict(s_src.artifacts) == dict(s_dst.artifacts)
    assert s_src.transitions == s_dst.transitions
    assert s_src.agent_calls == s_dst.agent_calls
    assert dict(s_src.loop_counters) == dict(s_dst.loop_counters)


def test_dump_task_includes_schema_version():
    m = PipelineMemory()
    m.new_task("T1", "x")
    dump = m.dump_task("T1")
    assert dump["schema_version"] == 1


def test_restore_rejects_wrong_schema_version():
    m = PipelineMemory()
    bad = {"schema_version": 99, "task_id": "T1", "raw_task": "x",
           "artifacts": {}, "transitions": [], "agent_calls": [], "loop_counters": {}}
    with pytest.raises(ValueError, match="unsupported_schema_version:99"):
        m.restore_task(bad)


def test_restore_rejects_duplicate_task_id():
    m = PipelineMemory()
    m.new_task("T1", "x")
    src = PipelineMemory()
    src.new_task("T1", "y")
    with pytest.raises(ValueError, match="task_already_exists:T1"):
        m.restore_task(src.dump_task("T1"))


def test_restore_rejects_missing_keys():
    m = PipelineMemory()
    incomplete = {"schema_version": 1, "task_id": "T1"}
    with pytest.raises(ValueError, match="missing_dump_key"):
        m.restore_task(incomplete)


def test_restore_rejects_unknown_artifact_kind():
    m = PipelineMemory()
    bad = {"schema_version": 1, "task_id": "T1", "raw_task": "x",
           "artifacts": {"hacker": "y"}, "transitions": [],
           "agent_calls": [], "loop_counters": {}}
    with pytest.raises(ValueError, match="unknown_artifact_kind:hacker"):
        m.restore_task(bad)


def test_restore_rejects_invalid_transition():
    m = PipelineMemory()
    bad = {"schema_version": 1, "task_id": "T1", "raw_task": "x",
           "artifacts": {}, "transitions": [
               {"from_state": "IDLE", "to_state": "WRITER"}
           ], "agent_calls": [], "loop_counters": {}}
    with pytest.raises(ValueError, match="invalid_transition:IDLE->WRITER"):
        m.restore_task(bad)


def test_restore_rejects_unknown_state_value():
    m = PipelineMemory()
    bad = {"schema_version": 1, "task_id": "T1", "raw_task": "x",
           "artifacts": {}, "transitions": [
               {"from_state": "IDLE", "to_state": "PARTY"}
           ], "agent_calls": [], "loop_counters": {}}
    with pytest.raises(ValueError, match="unknown_state"):
        m.restore_task(bad)


def test_restore_rejects_negative_loop_counter():
    m = PipelineMemory()
    bad = {"schema_version": 1, "task_id": "T1", "raw_task": "x",
           "artifacts": {}, "transitions": [],
           "agent_calls": [], "loop_counters": {"review_fix": -1}}
    with pytest.raises(ValueError, match="invalid_loop_counter"):
        m.restore_task(bad)


def test_restore_rejects_non_mapping_dump():
    m = PipelineMemory()
    with pytest.raises(ValueError, match="invalid_dump_type"):
        m.restore_task("not a dict")  # type: ignore[arg-type]


def test_two_tasks_isolated():
    m = PipelineMemory()
    m.new_task("A", "raw a")
    m.new_task("B", "raw b")
    m.set_artifact("A", "pm", "pm-A")
    m.set_artifact("B", "pm", "pm-B")
    assert m.get_artifact("A", "pm") == "pm-A"
    assert m.get_artifact("B", "pm") == "pm-B"
    m.increment_loop("A", "review_fix")
    assert m.get_loop("B", "review_fix") == 0
