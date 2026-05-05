"""Tests for core.sandbox_autofix (auto-fix lint issues before validation)."""


import pytest

from core.sandbox_autofix import (
    AutofixCommandResult,
    AutofixResult,
    _AutofixRunner,
    _AutofixRunResult,
    run_ruff_autofix,
)

# ---------------------------------------------------------------------------
# AutofixCommandResult / AutofixResult dataclasses
# ---------------------------------------------------------------------------


def test_command_result_happy_path():
    r = AutofixCommandResult(
        name="format",
        ok=True,
        returncode=0,
        stdout_excerpt="",
        stderr_excerpt="",
    )
    assert r.name == "format"
    assert r.ok is True


def test_command_result_is_frozen():
    r = AutofixCommandResult(
        name="format", ok=True, returncode=0, stdout_excerpt="", stderr_excerpt=""
    )
    with pytest.raises(Exception):
        r.name = "other"  # type: ignore[misc]


@pytest.mark.parametrize("bad", ["", "  "])
def test_command_result_rejects_empty_name(bad):
    with pytest.raises(ValueError, match="empty_name"):
        AutofixCommandResult(name=bad, ok=True, returncode=0, stdout_excerpt="", stderr_excerpt="")


def test_command_result_rejects_non_bool_ok():
    with pytest.raises(ValueError, match="ok_must_be_bool"):
        AutofixCommandResult(name="x", ok=1, returncode=0, stdout_excerpt="", stderr_excerpt="")  # type: ignore[arg-type]


def test_command_result_rejects_non_int_returncode():
    with pytest.raises(ValueError, match="returncode_must_be_int"):
        AutofixCommandResult(
            name="x", ok=True, returncode="0", stdout_excerpt="", stderr_excerpt=""  # type: ignore[arg-type]
        )


def test_aggregate_all_ok_when_all_succeed():
    r1 = AutofixCommandResult(name="format", ok=True, returncode=0, stdout_excerpt="", stderr_excerpt="")
    r2 = AutofixCommandResult(name="check_fix", ok=True, returncode=0, stdout_excerpt="", stderr_excerpt="")
    agg = AutofixResult(results=(r1, r2))
    assert agg.all_ok is True


def test_aggregate_all_ok_false_when_any_fails():
    r1 = AutofixCommandResult(name="format", ok=True, returncode=0, stdout_excerpt="", stderr_excerpt="")
    r2 = AutofixCommandResult(name="check_fix", ok=False, returncode=2, stdout_excerpt="", stderr_excerpt="oops")
    agg = AutofixResult(results=(r1, r2))
    assert agg.all_ok is False


def test_aggregate_summary_format():
    r1 = AutofixCommandResult(name="format", ok=True, returncode=0, stdout_excerpt="", stderr_excerpt="")
    r2 = AutofixCommandResult(name="check_fix", ok=False, returncode=2, stdout_excerpt="", stderr_excerpt="")
    agg = AutofixResult(results=(r1, r2))
    s = agg.summary()
    assert "format=ok" in s
    assert "check_fix=fail(rc=2)" in s


def test_aggregate_rejects_non_tuple():
    with pytest.raises(ValueError, match="results_must_be_tuple"):
        AutofixResult(results=[])  # type: ignore[arg-type]


def test_aggregate_rejects_invalid_result_type():
    with pytest.raises(ValueError, match="invalid_result_type"):
        AutofixResult(results=("not a result",))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# run_ruff_autofix — argument validation
# ---------------------------------------------------------------------------


def test_run_rejects_non_path():
    with pytest.raises(ValueError, match="path_must_be_path"):
        run_ruff_autofix("/tmp/x")  # type: ignore[arg-type]


def test_run_rejects_invalid_timeout(tmp_path):
    with pytest.raises(ValueError, match="invalid_timeout"):
        run_ruff_autofix(tmp_path, timeout=0)
    with pytest.raises(ValueError, match="invalid_timeout"):
        run_ruff_autofix(tmp_path, timeout=-5)
    with pytest.raises(ValueError, match="invalid_timeout"):
        run_ruff_autofix(tmp_path, timeout=True)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["", "  "])
def test_run_rejects_empty_python_executable(tmp_path, bad):
    with pytest.raises(ValueError, match="empty_python_executable"):
        run_ruff_autofix(tmp_path, python_executable=bad)


def test_run_rejects_invalid_runner_type(tmp_path):
    with pytest.raises(ValueError, match="invalid_runner_type"):
        run_ruff_autofix(tmp_path, runner="not a runner")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# run_ruff_autofix — execution flow with fake runner
# ---------------------------------------------------------------------------


class _CannedRunner(_AutofixRunner):
    """Records calls; returns canned results in order."""

    def __init__(self, results: list[_AutofixRunResult] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[dict] = []

    def run(self, *, cmd, cwd, timeout):
        self.calls.append({"cmd": cmd, "cwd": cwd, "timeout": timeout})
        if self.results:
            return self.results.pop(0)
        return _AutofixRunResult(returncode=0, stdout="", stderr="")


def test_run_invokes_format_then_check_fix(tmp_path):
    runner = _CannedRunner()
    run_ruff_autofix(tmp_path, runner=runner)

    assert len(runner.calls) == 2
    assert runner.calls[0]["cmd"][:3] == (runner.calls[0]["cmd"][0], "-m", "ruff")
    assert "format" in runner.calls[0]["cmd"]
    assert runner.calls[1]["cmd"][:3] == (runner.calls[1]["cmd"][0], "-m", "ruff")
    assert "check" in runner.calls[1]["cmd"]
    assert "--fix" in runner.calls[1]["cmd"]
    assert "--unsafe-fixes" in runner.calls[1]["cmd"]


def test_run_passes_cwd_correctly(tmp_path):
    runner = _CannedRunner()
    run_ruff_autofix(tmp_path, runner=runner)
    for call in runner.calls:
        assert call["cwd"] == str(tmp_path)


def test_run_passes_timeout(tmp_path):
    runner = _CannedRunner()
    run_ruff_autofix(tmp_path, runner=runner, timeout=15)
    for call in runner.calls:
        assert call["timeout"] == 15


def test_run_default_python_executable(tmp_path):
    """When python_executable is None, sys.executable is used."""
    import sys
    runner = _CannedRunner()
    run_ruff_autofix(tmp_path, runner=runner)
    assert runner.calls[0]["cmd"][0] == sys.executable


def test_run_custom_python_executable(tmp_path):
    runner = _CannedRunner()
    run_ruff_autofix(tmp_path, runner=runner, python_executable="/usr/local/bin/python3.12")
    assert runner.calls[0]["cmd"][0] == "/usr/local/bin/python3.12"


def test_run_returns_aggregate_result(tmp_path):
    runner = _CannedRunner(
        results=[
            _AutofixRunResult(returncode=0, stdout="", stderr=""),
            _AutofixRunResult(returncode=1, stdout="fixed 3 issues", stderr=""),
        ]
    )
    result = run_ruff_autofix(tmp_path, runner=runner)
    assert isinstance(result, AutofixResult)
    assert len(result.results) == 2
    assert result.results[0].name == "format"
    assert result.results[1].name == "check_fix"
    # rc=0 and rc=1 are both ok for ruff
    assert result.all_ok is True


def test_run_rc1_treated_as_ok_for_ruff(tmp_path):
    """ruff check --fix returns 1 when fixes were applied (still ok)."""
    runner = _CannedRunner(
        results=[
            _AutofixRunResult(returncode=0, stdout="", stderr=""),
            _AutofixRunResult(returncode=1, stdout="auto-fixed", stderr=""),
        ]
    )
    result = run_ruff_autofix(tmp_path, runner=runner)
    assert result.results[1].ok is True
    assert result.all_ok is True


def test_run_rc127_marked_not_ok(tmp_path):
    """rc=127 (command not found) → ok=False."""
    runner = _CannedRunner(
        results=[
            _AutofixRunResult(returncode=127, stdout="", stderr="ruff: command not found"),
            _AutofixRunResult(returncode=127, stdout="", stderr="ruff: command not found"),
        ]
    )
    result = run_ruff_autofix(tmp_path, runner=runner)
    assert result.all_ok is False
    assert result.results[0].ok is False


def test_run_rc124_timeout_marked_not_ok(tmp_path):
    """Timeout (rc=124) → not ok."""
    runner = _CannedRunner(
        results=[
            _AutofixRunResult(returncode=124, stdout="", stderr="timeout:30s"),
            _AutofixRunResult(returncode=124, stdout="", stderr="timeout:30s"),
        ]
    )
    result = run_ruff_autofix(tmp_path, runner=runner)
    assert result.all_ok is False


def test_run_truncates_long_stdout(tmp_path):
    """stdout/stderr in results is truncated to keep diagnostics bounded."""
    runner = _CannedRunner(
        results=[
            _AutofixRunResult(returncode=0, stdout="X" * 5000, stderr=""),
            _AutofixRunResult(returncode=0, stdout="", stderr=""),
        ]
    )
    result = run_ruff_autofix(tmp_path, runner=runner)
    assert len(result.results[0].stdout_excerpt) <= 600  # ~500 + "...[truncated]"
    assert "[truncated]" in result.results[0].stdout_excerpt


def test_run_total_never_raises_on_subprocess_errors(tmp_path):
    """Even if subprocess returns weird codes, run_ruff_autofix returns an
    AutofixResult — it never raises (validator runs next and catches issues)."""
    runner = _CannedRunner(
        results=[
            _AutofixRunResult(returncode=-9, stdout="", stderr="killed"),
            _AutofixRunResult(returncode=2, stdout="", stderr="syntax error"),
        ]
    )
    result = run_ruff_autofix(tmp_path, runner=runner)
    # Just verify it returned something; rc=-9 and rc=2 are not in (0, 1)
    assert isinstance(result, AutofixResult)
    assert result.all_ok is False


# ---------------------------------------------------------------------------
# Integration: real subprocess against a tiny Python file (when ruff available)
# ---------------------------------------------------------------------------


def test_real_ruff_format_unindents_messy_code(tmp_path):
    """Sanity: against a real ruff binary, format collapses extra blank lines."""
    import shutil
    if shutil.which("ruff") is None:
        pytest.skip("ruff binary not available")

    bad_file = tmp_path / "bad.py"
    bad_file.write_text(
        "import os\n"
        "import sys\n"
        "\n"
        "\n"
        "\n"
        "\n"
        "def foo():\n"
        "    return 42\n",
        encoding="utf-8",
    )
    result = run_ruff_autofix(tmp_path)
    assert isinstance(result, AutofixResult)
    # `os` and `sys` are unused → should be removed by --fix
    after = bad_file.read_text(encoding="utf-8")
    assert "import os" not in after
    assert "import sys" not in after
    # excessive blank lines should be collapsed by format
    assert "\n\n\n\n" not in after


def test_real_ruff_handles_empty_directory(tmp_path):
    """Empty directory → no files → both commands return 0/1, all_ok=True."""
    import shutil
    if shutil.which("ruff") is None:
        pytest.skip("ruff binary not available")

    result = run_ruff_autofix(tmp_path)
    assert isinstance(result, AutofixResult)
    # No files → no issues to fix → ok
    assert result.all_ok is True


# ---------------------------------------------------------------------------
# Helper: AutofixCommandResult __post_init__ edge cases
# ---------------------------------------------------------------------------


def test_command_result_rejects_non_string_excerpts():
    with pytest.raises(ValueError, match="stdout_excerpt_must_be_str"):
        AutofixCommandResult(
            name="x", ok=True, returncode=0, stdout_excerpt=42, stderr_excerpt=""  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="stderr_excerpt_must_be_str"):
        AutofixCommandResult(
            name="x", ok=True, returncode=0, stdout_excerpt="", stderr_excerpt=42  # type: ignore[arg-type]
        )


def test_run_uses_default_runner_when_none(tmp_path):
    """When runner=None, the function uses _DefaultAutofixRunner. Smoke test:
    invoking with a fake python_executable causes 127 (command not found),
    but still returns AutofixResult — never raises."""
    result = run_ruff_autofix(
        tmp_path,
        python_executable="/nonexistent/python",
    )
    assert isinstance(result, AutofixResult)
    # Both commands fail, all_ok is False
    assert result.all_ok is False
