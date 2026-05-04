import pytest

from core.contracts import (
    FORBIDDEN_PATH_PARTS,
    FSM_REQUIRED_STATES,
    PROTECTED_FILES,
    ContractResult,
    enforce_code_change,
    validate_code_change,
    validate_file_change,
    validate_fsm_states,
    validate_non_empty_text,
)

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------


def test_fsm_required_states_match_state_enum():
    expected = {
        "IDLE", "PLANNING", "PM", "ARCHITECT", "WRITER",
        "REVIEW", "TEST", "QA", "FIX",
        "SUCCESS", "FAIL", "BLOCKED",
    }
    assert FSM_REQUIRED_STATES == expected


def test_protected_files_includes_core_pillars():
    assert "core/agents.py" in PROTECTED_FILES
    assert "core/fsm.py" in PROTECTED_FILES
    assert "core/contracts.py" in PROTECTED_FILES


def test_forbidden_path_parts_includes_venv_and_git():
    assert ".venv" in FORBIDDEN_PATH_PARTS
    assert "__pycache__" in FORBIDDEN_PATH_PARTS
    assert ".git" in FORBIDDEN_PATH_PARTS


# ---------------------------------------------------------------------------
# validate_fsm_states
# ---------------------------------------------------------------------------


def test_validate_fsm_states_ok_when_full():
    result = validate_fsm_states(sorted(FSM_REQUIRED_STATES))
    assert isinstance(result, ContractResult)
    assert result.ok is True
    assert result.violations == []


def test_validate_fsm_states_reports_missing():
    result = validate_fsm_states(["IDLE", "PLANNING"])
    assert result.ok is False
    assert any(v.startswith("missing_state:") for v in result.violations)
    # Specifically, FAIL must be among missing.
    assert "missing_state:FAIL" in result.violations


def test_validate_fsm_states_extra_states_are_allowed():
    states = list(FSM_REQUIRED_STATES) + ["EXTRA_STATE"]
    result = validate_fsm_states(states)
    assert result.ok is True


# ---------------------------------------------------------------------------
# validate_non_empty_text
# ---------------------------------------------------------------------------


def test_validate_non_empty_text_ok():
    result = validate_non_empty_text("hello", "field")
    assert result.ok is True
    assert result.violations == []


@pytest.mark.parametrize("bad", ["", "   ", "\n\t  "])
def test_validate_non_empty_text_rejects_blank(bad):
    result = validate_non_empty_text(bad, "myfield")
    assert result.ok is False
    assert result.violations == ["empty_field:myfield"]


# ---------------------------------------------------------------------------
# validate_file_change
# ---------------------------------------------------------------------------


def test_validate_file_change_accepts_normal_path():
    result = validate_file_change("core/orchestrator.py")
    assert result.ok is True


@pytest.mark.parametrize("path", [
    ".venv/foo.py",
    "subdir/.venv/x.py",
    "subdir/__pycache__/x.pyc",
    ".git/config",
])
def test_validate_file_change_rejects_forbidden_paths(path):
    result = validate_file_change(path)
    assert result.ok is False
    assert any(v.startswith("forbidden_path:") for v in result.violations)


def test_validate_file_change_rejects_protected_file():
    result = validate_file_change("core/agents.py")
    assert result.ok is False
    assert "protected_file:core/agents.py" in result.violations


def test_validate_file_change_protected_files_all_blocked():
    for protected in PROTECTED_FILES:
        result = validate_file_change(protected)
        assert result.ok is False
        assert any(v.startswith("protected_file:") for v in result.violations)


# ---------------------------------------------------------------------------
# validate_code_change
# ---------------------------------------------------------------------------


def test_validate_code_change_accepts_safe_content():
    result = validate_code_change("core/orchestrator.py", "x = 1\n")
    assert result.ok is True


def test_validate_code_change_rejects_protected_file_first():
    result = validate_code_change("core/contracts.py", "x = 1\n")
    assert result.ok is False
    assert any(v.startswith("protected_file:") for v in result.violations)


def test_validate_code_change_rejects_empty_content():
    result = validate_code_change("core/orchestrator.py", "   ")
    assert result.ok is False
    assert "empty_field:content" in result.violations


@pytest.mark.parametrize("token", ["TODO", "FIXME", "NotImplementedError", "placeholder"])
def test_validate_code_change_rejects_forbidden_tokens(token):
    content = f"def f():\n    raise {token}\n"
    result = validate_code_change("core/orchestrator.py", content)
    assert result.ok is False
    assert f"forbidden_token:{token}" in result.violations


def test_validate_code_change_collects_multiple_token_violations():
    content = "TODO and FIXME and placeholder\n"
    result = validate_code_change("core/orchestrator.py", content)
    assert result.ok is False
    assert {"forbidden_token:TODO", "forbidden_token:FIXME", "forbidden_token:placeholder"} <= set(result.violations)


# ---------------------------------------------------------------------------
# enforce_code_change
# ---------------------------------------------------------------------------


def test_enforce_code_change_passes_for_valid_input():
    enforce_code_change("core/orchestrator.py", "x = 1\n")


def test_enforce_code_change_raises_for_protected_file():
    with pytest.raises(ValueError, match="protected_file:core/agents.py"):
        enforce_code_change("core/agents.py", "x = 1\n")


def test_enforce_code_change_raises_for_forbidden_token():
    with pytest.raises(ValueError, match="forbidden_token:TODO"):
        enforce_code_change("core/orchestrator.py", "TODO: later\n")


def test_enforce_code_change_raises_for_empty_content():
    with pytest.raises(ValueError, match="empty_field:content"):
        enforce_code_change("core/orchestrator.py", "")


def test_contract_result_is_frozen():
    cr = ContractResult(ok=True, violations=[])
    with pytest.raises(Exception):
        cr.ok = False  # type: ignore[misc]
