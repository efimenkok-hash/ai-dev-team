import pytest

from core.observability import JsonLinesSink, Observability
from core.self_improvement import (
    MIN_OCCURRENCES,
    VALID_PRIORITIES,
    VALID_RESOLUTIONS,
    AgentErrorStats,
    ErrorAnalysis,
    ErrorPattern,
    FeedbackEntry,
    PromptSuggestion,
    SelfImprovement,
    normalise_error,
)

# ---------------------------------------------------------------------------
# normalise_error
# ---------------------------------------------------------------------------


def test_normalise_collapses_numbers():
    assert normalise_error("HTTP 500 in 1234ms") == normalise_error("HTTP 999 in 50ms")


def test_normalise_collapses_paths():
    a = normalise_error("file /home/u/a.py not found")
    b = normalise_error("file /tmp/xyz/b.py not found")
    assert a == b


def test_normalise_collapses_double_quoted_strings():
    a = normalise_error('expected "foo" but got "bar"')
    b = normalise_error('expected "x"   but got "y"')
    assert a == b


def test_normalise_collapses_single_quoted_strings():
    a = normalise_error("KeyError: 'missing_field'")
    b = normalise_error("KeyError: 'other_field'")
    assert a == b


def test_normalise_collapses_hex():
    a = normalise_error("at address 0xABCDEF12")
    b = normalise_error("at address 0xdeadbeef")
    assert a == b


def test_normalise_handles_empty():
    assert normalise_error("") == ""
    assert normalise_error("   ") == ""


def test_normalise_truncates_long_messages():
    long_msg = "x" * 5000
    out = normalise_error(long_msg)
    assert len(out) <= 250
    assert out.endswith("...")


def test_normalise_rejects_non_string():
    with pytest.raises(ValueError, match="non_string_message"):
        normalise_error(42)  # type: ignore[arg-type]


def test_normalise_normalises_whitespace():
    assert normalise_error("error\n  with\t\textra   spaces") == \
        "error with extra spaces"


# ---------------------------------------------------------------------------
# Constructor & sink requirements
# ---------------------------------------------------------------------------


def test_constructor_rejects_non_observability():
    with pytest.raises(ValueError, match="invalid_observability_type"):
        SelfImprovement("not an obs")  # type: ignore[arg-type]


def test_analyse_requires_in_memory_sink(tmp_path):
    sink = JsonLinesSink(tmp_path / "log.jsonl")
    obs = Observability(sink=sink)
    si = SelfImprovement(obs)
    with pytest.raises(RuntimeError, match="analyse_errors_requires_in_memory_sink"):
        si.analyse_errors()


# ---------------------------------------------------------------------------
# analyse_errors — empty
# ---------------------------------------------------------------------------


def test_analyse_empty_history():
    obs = Observability()
    si = SelfImprovement(obs)
    a = si.analyse_errors()
    assert isinstance(a, ErrorAnalysis)
    assert a.total_calls == 0
    assert a.total_failures == 0
    assert a.by_agent == {}
    assert a.patterns == ()


def test_analyse_only_successes():
    obs = Observability()
    for _ in range(5):
        obs.record_agent_call("pm_agent", "T1", 10, 1, 1, 0.0, ok=True)
    si = SelfImprovement(obs)
    a = si.analyse_errors()
    assert a.total_calls == 5
    assert a.total_failures == 0
    assert a.by_agent["pm_agent"].failure_rate == 0.0
    assert a.patterns == ()


# ---------------------------------------------------------------------------
# analyse_errors — patterns
# ---------------------------------------------------------------------------


def _seed_failures(obs: Observability, agent: str, error: str, n: int) -> None:
    for _ in range(n):
        obs.record_agent_call(agent, "T1", 10, 1, 1, 0.0, ok=False, error=error)


def test_analyse_groups_similar_errors_into_one_pattern():
    obs = Observability()
    obs.record_agent_call("pm_agent", "T1", 10, 1, 1, 0.0, ok=False, error="JSONDecodeError at line 42")
    obs.record_agent_call("pm_agent", "T1", 10, 1, 1, 0.0, ok=False, error="JSONDecodeError at line 17")
    obs.record_agent_call("pm_agent", "T1", 10, 1, 1, 0.0, ok=False, error="JSONDecodeError at line 1024")
    si = SelfImprovement(obs)
    a = si.analyse_errors()
    assert len(a.patterns) == 1
    assert a.patterns[0].occurrences == 3
    assert "JSONDecodeError" in a.patterns[0].error_signature


def test_analyse_one_off_error_excluded_from_patterns_by_default():
    obs = Observability()
    obs.record_agent_call("pm_agent", "T1", 10, 1, 1, 0.0, ok=False, error="weird unique thing")
    si = SelfImprovement(obs)
    a = si.analyse_errors()
    assert a.patterns == ()
    # But still visible in agent stats:
    assert a.by_agent["pm_agent"].failure_count == 1
    assert len(a.by_agent["pm_agent"].top_errors) == 1


def test_analyse_min_occurrences_threshold_is_configurable():
    obs = Observability()
    obs.record_agent_call("pm_agent", "T1", 10, 1, 1, 0.0, ok=False, error="oops once")
    si = SelfImprovement(obs)
    a = si.analyse_errors(min_occurrences=1)
    assert len(a.patterns) == 1


def test_analyse_min_occurrences_validation():
    obs = Observability()
    si = SelfImprovement(obs)
    with pytest.raises(ValueError, match="invalid_min_occurrences"):
        si.analyse_errors(min_occurrences=0)
    with pytest.raises(ValueError, match="invalid_min_occurrences"):
        si.analyse_errors(min_occurrences=-1)


def test_analyse_filter_by_agent():
    obs = Observability()
    _seed_failures(obs, "pm_agent", "boom", 3)
    _seed_failures(obs, "writer_agent", "crash", 3)
    si = SelfImprovement(obs)
    only_pm = si.analyse_errors(agent_name="pm_agent")
    assert "writer_agent" not in only_pm.by_agent
    assert only_pm.by_agent["pm_agent"].failure_count == 3
    assert all(p.agent_name == "pm_agent" for p in only_pm.patterns)


def test_analyse_filter_invalid_agent_name():
    obs = Observability()
    si = SelfImprovement(obs)
    with pytest.raises(ValueError, match="invalid_agent_name"):
        si.analyse_errors(agent_name="   ")


def test_analyse_filter_invalid_since_iso():
    obs = Observability()
    si = SelfImprovement(obs)
    with pytest.raises(ValueError, match="invalid_since_iso"):
        si.analyse_errors(since_iso="   ")


def test_analyse_filter_since_iso_excludes_old():
    obs = Observability()
    obs.record_agent_call("pm_agent", "T1", 10, 1, 1, 0.0, ok=False, error="old err")
    obs.record_agent_call("pm_agent", "T1", 10, 1, 1, 0.0, ok=False, error="old err")
    # Use a future cutoff so all records are excluded.
    si = SelfImprovement(obs)
    a = si.analyse_errors(since_iso="9999-01-01T00:00:00+00:00")
    assert a.total_calls == 0
    assert a.patterns == ()


# ---------------------------------------------------------------------------
# AgentErrorStats fields
# ---------------------------------------------------------------------------


def test_agent_stats_failure_rate_correct():
    obs = Observability()
    for _ in range(7):
        obs.record_agent_call("pm_agent", "T1", 10, 1, 1, 0.0, ok=True)
    for _ in range(3):
        obs.record_agent_call("pm_agent", "T1", 10, 1, 1, 0.0, ok=False, error="x")
    si = SelfImprovement(obs)
    a = si.analyse_errors()
    stats = a.by_agent["pm_agent"]
    assert stats.total_calls == 10
    assert stats.success_count == 7
    assert stats.failure_count == 3
    assert pytest.approx(stats.failure_rate, rel=1e-6) == 0.3


def test_agent_stats_top_errors_sorted_by_count():
    obs = Observability()
    for _ in range(5):
        obs.record_agent_call("pm_agent", "T1", 10, 1, 1, 0.0, ok=False, error="A")
    for _ in range(3):
        obs.record_agent_call("pm_agent", "T1", 10, 1, 1, 0.0, ok=False, error="B")
    for _ in range(2):
        obs.record_agent_call("pm_agent", "T1", 10, 1, 1, 0.0, ok=False, error="C")
    si = SelfImprovement(obs)
    a = si.analyse_errors()
    top = a.by_agent["pm_agent"].top_errors
    assert top[0][1] >= top[1][1] >= top[2][1]


# ---------------------------------------------------------------------------
# ErrorPattern fields
# ---------------------------------------------------------------------------


def test_pattern_collects_sample_errors_capped_at_three():
    obs = Observability()
    for i in range(10):
        obs.record_agent_call(
            "pm_agent", "T1", 10, 1, 1, 0.0, ok=False, error=f"json fail {i}"
        )
    si = SelfImprovement(obs)
    a = si.analyse_errors()
    assert len(a.patterns) == 1
    assert len(a.patterns[0].sample_errors) == 3


def test_pattern_avg_duration_computed():
    obs = Observability()
    for ms in [10, 20, 30]:
        obs.record_agent_call(
            "pm_agent", "T1", ms, 1, 1, 0.0, ok=False, error="json broken"
        )
    si = SelfImprovement(obs)
    a = si.analyse_errors()
    assert a.patterns[0].avg_duration_ms_at_failure == pytest.approx(20.0)


def test_patterns_sorted_by_occurrences_desc():
    obs = Observability()
    _seed_failures(obs, "agent_a", "rare json", 2)
    _seed_failures(obs, "agent_b", "common timeout", 5)
    si = SelfImprovement(obs)
    a = si.analyse_errors()
    assert a.patterns[0].agent_name == "agent_b"
    assert a.patterns[1].agent_name == "agent_a"


# ---------------------------------------------------------------------------
# suggest_prompt_improvements
# ---------------------------------------------------------------------------


def test_suggest_rejects_non_analysis():
    obs = Observability()
    si = SelfImprovement(obs)
    with pytest.raises(ValueError, match="invalid_analysis_type"):
        si.suggest_prompt_improvements("not analysis")  # type: ignore[arg-type]


def test_suggest_empty_when_no_patterns():
    obs = Observability()
    si = SelfImprovement(obs)
    a = si.analyse_errors()
    assert si.suggest_prompt_improvements(a) == ()


def test_suggest_json_pattern_yields_high_priority():
    obs = Observability()
    _seed_failures(obs, "pm_agent", "JSONDecodeError on line 42", 3)
    si = SelfImprovement(obs)
    a = si.analyse_errors()
    suggestions = si.suggest_prompt_improvements(a)
    assert len(suggestions) >= 1
    json_sug = next(s for s in suggestions if "JSON" in s.issue)
    assert json_sug.priority == "high"
    assert json_sug.agent_name == "pm_agent"


def test_suggest_timeout_pattern_yields_high_priority():
    obs = Observability()
    _seed_failures(obs, "writer_agent", "Request timeout after 30s", 4)
    si = SelfImprovement(obs)
    a = si.analyse_errors()
    suggestions = si.suggest_prompt_improvements(a)
    timeout_sug = next(
        (s for s in suggestions if "timeout" in s.based_on_signature.lower()),
        None,
    )
    assert timeout_sug is not None
    assert timeout_sug.priority == "high"


def test_suggest_unknown_pattern_yields_low_priority_fallback():
    obs = Observability()
    # Lots of successes so failure_rate stays low and the high-priority
    # promotion rule does NOT fire — leaves only the low-priority fallback.
    for _ in range(20):
        obs.record_agent_call("x", "T1", 10, 1, 1, 0.0, ok=True)
    _seed_failures(obs, "x", "completely unrecognised error type xyz", 2)
    si = SelfImprovement(obs)
    a = si.analyse_errors()
    suggestions = si.suggest_prompt_improvements(a)
    assert len(suggestions) == 1
    assert suggestions[0].priority == "low"


def test_suggest_promotes_agent_with_high_failure_rate():
    obs = Observability()
    # 1 success + 5 unique single-shot failures = no patterns, but failure_rate > 0.5
    obs.record_agent_call("flaky_agent", "T1", 10, 1, 1, 0.0, ok=True)
    for i in range(5):
        obs.record_agent_call(
            "flaky_agent", "T1", 10, 1, 1, 0.0, ok=False, error=f"unique error {i}"
        )
    si = SelfImprovement(obs)
    a = si.analyse_errors()
    suggestions = si.suggest_prompt_improvements(a)
    high = [s for s in suggestions if s.priority == "high"]
    assert any("failure rate" in s.issue.lower() for s in high)


def test_suggest_sorted_by_priority_desc():
    obs = Observability()
    _seed_failures(obs, "a1", "JSON broken", 2)              # high
    _seed_failures(obs, "a2", "schema mismatch happened", 2) # medium
    _seed_failures(obs, "a3", "weird unknown thing here", 2) # low
    si = SelfImprovement(obs)
    a = si.analyse_errors()
    suggestions = si.suggest_prompt_improvements(a)
    priorities = [s.priority for s in suggestions]
    # All highs first, then mediums, then lows.
    rank = {"high": 0, "medium": 1, "low": 2}
    assert priorities == sorted(priorities, key=lambda p: rank[p])


def test_suggestion_priority_in_valid_set():
    obs = Observability()
    _seed_failures(obs, "agent", "json bad", 2)
    si = SelfImprovement(obs)
    suggestions = si.suggest_prompt_improvements(si.analyse_errors())
    assert all(s.priority in VALID_PRIORITIES for s in suggestions)


# ---------------------------------------------------------------------------
# Feedback log
# ---------------------------------------------------------------------------


def test_feedback_round_trip():
    obs = Observability()
    si = SelfImprovement(obs)
    e = si.record_feedback(
        task_id="T1",
        agent_name="pm_agent",
        error_signature="json_decode",
        resolution="fixed",
        notes="updated prompt to include schema",
    )
    assert isinstance(e, FeedbackEntry)
    history = si.feedback_history()
    assert history == (e,)


def test_feedback_filter_by_agent():
    obs = Observability()
    si = SelfImprovement(obs)
    si.record_feedback("T1", "pm_agent", "x", "fixed")
    si.record_feedback("T1", "writer_agent", "y", "fixed")
    pm_only = si.feedback_history(agent_name="pm_agent")
    assert len(pm_only) == 1
    assert pm_only[0].agent_name == "pm_agent"


def test_feedback_filter_by_resolution():
    obs = Observability()
    si = SelfImprovement(obs)
    si.record_feedback("T1", "pm", "x", "fixed")
    si.record_feedback("T1", "pm", "y", "ignore")
    fixed = si.feedback_history(resolution="fixed")
    assert len(fixed) == 1


def test_feedback_rejects_unknown_resolution():
    obs = Observability()
    si = SelfImprovement(obs)
    with pytest.raises(ValueError, match="unknown_resolution"):
        si.record_feedback("T1", "pm", "x", "approved")


def test_feedback_rejects_empty_task_id():
    obs = Observability()
    si = SelfImprovement(obs)
    with pytest.raises(ValueError, match="empty_task_id"):
        si.record_feedback("  ", "pm", "x", "fixed")


def test_feedback_rejects_empty_agent_name():
    obs = Observability()
    si = SelfImprovement(obs)
    with pytest.raises(ValueError, match="empty_agent_name"):
        si.record_feedback("T1", "  ", "x", "fixed")


def test_feedback_rejects_empty_signature():
    obs = Observability()
    si = SelfImprovement(obs)
    with pytest.raises(ValueError, match="empty_error_signature"):
        si.record_feedback("T1", "pm", "  ", "fixed")


def test_feedback_history_decoupled_from_internal_state():
    obs = Observability()
    si = SelfImprovement(obs)
    si.record_feedback("T1", "pm", "x", "fixed")
    snap = si.feedback_history()
    si.record_feedback("T1", "pm", "y", "fixed")
    assert len(snap) == 1   # snapshot is decoupled


def test_valid_resolutions_constant():
    assert "fixed" in VALID_RESOLUTIONS
    assert "wont_fix" in VALID_RESOLUTIONS


# ---------------------------------------------------------------------------
# Frozen invariants
# ---------------------------------------------------------------------------


def test_error_pattern_is_frozen():
    p = ErrorPattern(
        agent_name="x",
        error_signature="sig",
        occurrences=2,
        sample_errors=("a", "b"),
        avg_duration_ms_at_failure=10.0,
    )
    with pytest.raises(Exception):
        p.occurrences = 99  # type: ignore[misc]


def test_agent_error_stats_is_frozen():
    s = AgentErrorStats(
        agent_name="x",
        total_calls=1,
        success_count=0,
        failure_count=1,
        failure_rate=1.0,
        top_errors=(),
    )
    with pytest.raises(Exception):
        s.failure_rate = 0.0  # type: ignore[misc]


def test_error_analysis_is_frozen():
    obs = Observability()
    si = SelfImprovement(obs)
    a = si.analyse_errors()
    with pytest.raises(Exception):
        a.total_calls = 999  # type: ignore[misc]


def test_prompt_suggestion_is_frozen():
    s = PromptSuggestion(
        agent_name="x",
        issue="i",
        suggestion="s",
        priority="low",
        based_on_signature="sig",
    )
    with pytest.raises(Exception):
        s.priority = "high"  # type: ignore[misc]


def test_feedback_entry_is_frozen():
    obs = Observability()
    si = SelfImprovement(obs)
    e = si.record_feedback("T1", "pm", "x", "fixed")
    with pytest.raises(Exception):
        e.notes = "tampered"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_min_occurrences_constant():
    assert MIN_OCCURRENCES == 2


def test_priority_values():
    assert tuple(VALID_PRIORITIES) == ("high", "medium", "low")
