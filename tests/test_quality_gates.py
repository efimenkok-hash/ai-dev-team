import subprocess
from pathlib import Path

import pytest

from core.quality_gates import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_RAW_OUTPUT_CHARS,
    CheckResult,
    QualityGates,
    QualityReport,
    _parse_coverage_percent,
    _truncate,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class CallRecorder:
    """Replaces subprocess.run; returns scripted FakeProcs per command."""

    def __init__(self, responses: list[FakeProc]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(
            {
                "cmd": list(cmd),
                "cwd": kwargs.get("cwd"),
                "stdin": kwargs.get("stdin"),
                "env": dict(kwargs.get("env") or {}),
                "timeout": kwargs.get("timeout"),
                "text": kwargs.get("text"),
            }
        )
        if not self._responses:
            return FakeProc(returncode=0)
        return self._responses.pop(0)


def _patch_run(monkeypatch, recorder: CallRecorder) -> None:
    monkeypatch.setattr("core.quality_gates.subprocess.run", recorder)


def _patch_which(monkeypatch, present=True) -> None:
    def fake_which(name: str):
        return f"/usr/bin/{name}" if present else None

    monkeypatch.setattr("core.quality_gates.shutil.which", fake_which)


@pytest.fixture
def gates(tmp_path: Path) -> QualityGates:
    return QualityGates(repo_path=tmp_path, python_executable="python3", timeout=42)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_truncate_short_passes_through():
    text = "small"
    assert _truncate(text) == text


def test_truncate_long_keeps_prefix_and_marker():
    text = "x" * (MAX_RAW_OUTPUT_CHARS + 100)
    out = _truncate(text)
    assert len(out) <= MAX_RAW_OUTPUT_CHARS + 80
    assert out.endswith("chars]")


def test_parse_coverage_percent_from_report():
    text = "Name      Stmts   Miss  Cover\ncore/foo   10  1  90%\nTOTAL      10  1  90%\n"
    assert _parse_coverage_percent(text) == 90.0


def test_parse_coverage_percent_with_branch_columns():
    text = "TOTAL   120   10   20   4   85.7%\n"
    assert _parse_coverage_percent(text) == 85.7


def test_parse_coverage_percent_returns_none_when_no_total():
    assert _parse_coverage_percent("nothing relevant\n") is None


def test_parse_coverage_percent_returns_none_when_unparseable():
    assert _parse_coverage_percent("TOTAL no percent here\n") is None


# ---------------------------------------------------------------------------
# construction contract
# ---------------------------------------------------------------------------


def test_construction_rejects_missing_path(tmp_path: Path):
    with pytest.raises(ValueError, match="repo_path_missing"):
        QualityGates(repo_path=tmp_path / "nope")


def test_construction_rejects_file_path(tmp_path: Path):
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="repo_path_not_dir"):
        QualityGates(repo_path=f)


def test_construction_rejects_invalid_timeout(tmp_path: Path):
    with pytest.raises(ValueError, match="invalid_timeout"):
        QualityGates(repo_path=tmp_path, timeout=0)
    with pytest.raises(ValueError, match="invalid_timeout"):
        QualityGates(repo_path=tmp_path, timeout=-5)


def test_construction_rejects_empty_python_executable(tmp_path: Path):
    with pytest.raises(ValueError, match="empty_python_executable"):
        QualityGates(repo_path=tmp_path, python_executable="   ")


def test_default_timeout_constant():
    assert DEFAULT_TIMEOUT_SECONDS == 120


# ---------------------------------------------------------------------------
# subprocess invocation invariants (via mocked run)
# ---------------------------------------------------------------------------


def test_run_lint_invokes_ruff_with_correct_arguments(gates: QualityGates, monkeypatch):
    rec = CallRecorder([FakeProc(returncode=0, stdout="All checks passed!\n")])
    _patch_which(monkeypatch, present=True)
    _patch_run(monkeypatch, rec)

    result = gates.run_lint()

    assert result.ok is True
    assert result.name == "lint"
    assert result.summary == "ok:no_lint_violations"
    call = rec.calls[0]
    assert call["cmd"] == ["python3", "-m", "ruff", "check", "core", "tests"]
    assert call["cwd"] == str(gates.repo_path)
    assert call["stdin"] is subprocess.DEVNULL
    assert call["timeout"] == 42
    assert call["env"]["LANG"] == "C"
    assert "GIT_TERMINAL_PROMPT" not in call["env"]  # quality_gates env, not git env


def test_run_lint_failure_propagates_returncode(gates: QualityGates, monkeypatch):
    rec = CallRecorder([FakeProc(returncode=1, stdout="some violations\n")])
    _patch_which(monkeypatch, present=True)
    _patch_run(monkeypatch, rec)
    result = gates.run_lint()
    assert result.ok is False
    assert "returncode=1" in result.summary
    assert "some violations" in result.raw_output


def test_run_lint_accepts_custom_targets(gates: QualityGates, monkeypatch):
    rec = CallRecorder([FakeProc(returncode=0)])
    _patch_which(monkeypatch, present=True)
    _patch_run(monkeypatch, rec)
    gates.run_lint(targets=["core/orchestrator.py"])
    assert rec.calls[0]["cmd"] == [
        "python3",
        "-m",
        "ruff",
        "check",
        "core/orchestrator.py",
    ]


def test_run_lint_returns_tool_not_found_when_python_missing(gates: QualityGates, monkeypatch):
    _patch_which(monkeypatch, present=False)

    def never_called(*a, **kw):
        raise AssertionError("subprocess.run must not be called when tool is missing")

    monkeypatch.setattr("core.quality_gates.subprocess.run", never_called)
    result = gates.run_lint()
    assert result.ok is False
    assert result.summary == "tool_not_found:python3"
    assert result.raw_output == ""


def test_run_tests_passes_pytest_invocation(gates: QualityGates, monkeypatch):
    rec = CallRecorder([FakeProc(returncode=0, stdout="5 passed")])
    _patch_which(monkeypatch, present=True)
    _patch_run(monkeypatch, rec)
    result = gates.run_tests()
    assert result.ok is True
    assert result.summary == "ok:all_tests_passed"
    assert rec.calls[0]["cmd"] == [
        "python3",
        "-m",
        "pytest",
        "tests",
        "-q",
        "-p",
        "no:cacheprovider",
    ]


def test_run_tests_rejects_empty_target(gates: QualityGates):
    with pytest.raises(ValueError, match="empty_test_target"):
        gates.run_tests(target="   ")


def test_run_tests_failure_returns_failure_summary(gates: QualityGates, monkeypatch):
    rec = CallRecorder([FakeProc(returncode=2, stdout="3 failed")])
    _patch_which(monkeypatch, present=True)
    _patch_run(monkeypatch, rec)
    result = gates.run_tests()
    assert result.ok is False
    assert "returncode=2" in result.summary


def test_run_tests_handles_timeout(gates: QualityGates, monkeypatch):
    _patch_which(monkeypatch, present=True)

    def raise_timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr("core.quality_gates.subprocess.run", raise_timeout)
    result = gates.run_tests()
    assert result.ok is False
    assert result.summary.startswith("timeout:")


# ---------------------------------------------------------------------------
# coverage flow
# ---------------------------------------------------------------------------


_REPORT_OK = (
    "Name           Stmts   Miss  Cover\n"
    "core/x.py         10      1   90%\n"
    "TOTAL             10      1   90.0%\n"
)


def test_run_coverage_happy_path(gates: QualityGates, monkeypatch):
    rec = CallRecorder(
        [
            FakeProc(returncode=0, stdout="all tests pass"),  # coverage run
            FakeProc(returncode=0, stdout=_REPORT_OK),         # coverage report
        ]
    )
    _patch_which(monkeypatch, present=True)
    _patch_run(monkeypatch, rec)
    result = gates.run_coverage(min_coverage=80.0)
    assert result.ok is True
    assert result.summary == "ok:90.0%>=80.0%"
    assert "coverage" in rec.calls[0]["cmd"]
    assert rec.calls[1]["cmd"] == ["python3", "-m", "coverage", "report", "--precision=1"]


def test_run_coverage_below_threshold_fails(gates: QualityGates, monkeypatch):
    report = "TOTAL    10  5   50%\n"
    rec = CallRecorder(
        [FakeProc(returncode=0), FakeProc(returncode=0, stdout=report)]
    )
    _patch_which(monkeypatch, present=True)
    _patch_run(monkeypatch, rec)
    result = gates.run_coverage(min_coverage=80.0)
    assert result.ok is False
    assert "below_threshold:50.0%<80.0%" in result.summary


def test_run_coverage_tests_failure_short_circuits(gates: QualityGates, monkeypatch):
    rec = CallRecorder([FakeProc(returncode=1, stdout="tests failed")])
    _patch_which(monkeypatch, present=True)
    _patch_run(monkeypatch, rec)
    result = gates.run_coverage(min_coverage=0.0)
    assert result.ok is False
    assert result.summary.startswith("tests_failed:")
    assert len(rec.calls) == 1  # report not called


def test_run_coverage_unparseable_report_fails(gates: QualityGates, monkeypatch):
    rec = CallRecorder(
        [FakeProc(returncode=0), FakeProc(returncode=0, stdout="no total here")]
    )
    _patch_which(monkeypatch, present=True)
    _patch_run(monkeypatch, rec)
    result = gates.run_coverage(min_coverage=0.0)
    assert result.ok is False
    assert result.summary == "coverage_percent_unparseable"


@pytest.mark.parametrize("bad", [-1.0, 100.1, 1000.0])
def test_run_coverage_rejects_out_of_range(gates: QualityGates, bad: float):
    with pytest.raises(ValueError, match="invalid_min_coverage"):
        gates.run_coverage(min_coverage=bad)


def test_run_coverage_rejects_non_numeric(gates: QualityGates):
    with pytest.raises(ValueError, match="invalid_min_coverage_type"):
        gates.run_coverage(min_coverage="80")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# run_all aggregation
# ---------------------------------------------------------------------------


def test_run_all_aggregates_all_three_checks(gates: QualityGates, monkeypatch):
    rec = CallRecorder(
        [
            FakeProc(returncode=0, stdout="ruff ok"),
            FakeProc(returncode=0, stdout="pytest ok"),
            FakeProc(returncode=0, stdout="coverage run ok"),
            FakeProc(returncode=0, stdout=_REPORT_OK),
        ]
    )
    _patch_which(monkeypatch, present=True)
    _patch_run(monkeypatch, rec)
    report = gates.run_all(min_coverage=80.0)
    assert isinstance(report, QualityReport)
    assert report.ok is True
    assert tuple(c.name for c in report.checks) == ("lint", "tests", "coverage")
    assert all(c.ok for c in report.checks)


def test_run_all_marks_overall_failure_if_any_fails(gates: QualityGates, monkeypatch):
    rec = CallRecorder(
        [
            FakeProc(returncode=0, stdout="ruff ok"),
            FakeProc(returncode=1, stdout="pytest failed"),
            FakeProc(returncode=0, stdout="coverage run ok"),
            FakeProc(returncode=0, stdout=_REPORT_OK),
        ]
    )
    _patch_which(monkeypatch, present=True)
    _patch_run(monkeypatch, rec)
    report = gates.run_all(min_coverage=0.0)
    assert report.ok is False
    assert report.checks[1].ok is False
    assert report.checks[1].name == "tests"


def test_quality_report_is_frozen(gates: QualityGates):
    report = QualityReport(ok=True, checks=())
    with pytest.raises(Exception):
        report.ok = False  # type: ignore[misc]


def test_check_result_is_frozen(gates: QualityGates):
    cr = CheckResult(name="x", ok=True, summary="s", raw_output="", duration_ms=1)
    with pytest.raises(Exception):
        cr.ok = False  # type: ignore[misc]


def test_check_result_duration_ms_non_negative(gates: QualityGates, monkeypatch):
    rec = CallRecorder([FakeProc(returncode=0)])
    _patch_which(monkeypatch, present=True)
    _patch_run(monkeypatch, rec)
    result = gates.run_lint()
    assert result.duration_ms >= 0


def test_run_all_with_empty_checks_property():
    # If, hypothetically, no checks ran, ok must be False.
    report = QualityReport(ok=False, checks=())
    assert report.ok is False
    assert report.checks == ()


# ---------------------------------------------------------------------------
# env passthrough invariants
# ---------------------------------------------------------------------------


def test_build_subprocess_env_includes_passthrough_keys(monkeypatch):
    from core.quality_gates import _build_subprocess_env

    monkeypatch.setenv("HOME", "/home/test")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("COVERAGE_FILE", "/tmp/cov")

    env = _build_subprocess_env()
    assert env["LANG"] == "C"
    assert env["HOME"] == "/home/test"
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["COVERAGE_FILE"] == "/tmp/cov"


def test_build_subprocess_env_merges_extra(monkeypatch):
    from core.quality_gates import _build_subprocess_env

    env = _build_subprocess_env(extra={"MY_KEY": "MY_VALUE"})
    assert env["MY_KEY"] == "MY_VALUE"
    # Base keys still present.
    assert env["LANG"] == "C"


def test_build_subprocess_env_does_not_leak_unknown_secrets(monkeypatch):
    from core.quality_gates import _build_subprocess_env

    monkeypatch.setenv("OPENROUTER_API_KEY", "super-secret")
    env = _build_subprocess_env()
    assert "OPENROUTER_API_KEY" not in env


def test_quality_gates_extra_env_propagates_to_run(tmp_path: Path, monkeypatch):
    rec = CallRecorder([FakeProc(returncode=0)])
    _patch_which(monkeypatch, present=True)
    _patch_run(monkeypatch, rec)
    g = QualityGates(repo_path=tmp_path, extra_env={"MY_FLAG": "1"})
    g.run_lint()
    assert rec.calls[0]["env"]["MY_FLAG"] == "1"
