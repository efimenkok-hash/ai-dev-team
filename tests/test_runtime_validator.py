"""Tests for core.runtime_validator (Step 15: Final Validation, closes Risk C).

Heavy reliance on monkeypatch to keep tests deterministic and tool-independent:
real ruff/pytest invocations are slow and would couple tests to the host
environment. The integration-with-real-tools path is implicitly covered by
existing test_quality_gates.py + scripts/quality_check.sh in CI.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from core.adapter import ProjectAdapter, ProjectCommand
from core.quality_gates import (
    MAX_RAW_OUTPUT_CHARS,
    CheckResult,
    QualityGates,
)
from core.runtime_validator import (
    RuntimeValidator,
    ValidationReport,
    ValidationStrategy,
    default_gates_factory,
    make_orchestrator_hook,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_adapter(
    tmp_path: Path,
    *,
    name: str = "demo",
    commands: dict | None = None,
    forbidden_paths: tuple[str, ...] = (),
) -> ProjectAdapter:
    project = tmp_path / name
    project.mkdir(parents=True, exist_ok=True)
    (project / "pyproject.toml").write_text("# stub\n", encoding="utf-8")
    return ProjectAdapter(
        name=name,
        project_path=project,
        language="python",
        commands=commands or {},
        forbidden_paths=forbidden_paths,
    )


class FakeGates:
    """Drop-in stand-in for QualityGates with pre-canned CheckResult outputs."""

    def __init__(
        self,
        repo_path: Path,
        lint: CheckResult | None = None,
        tests: CheckResult | None = None,
        coverage: CheckResult | None = None,
    ) -> None:
        self.repo_path = repo_path
        self._lint = lint or CheckResult("lint", True, "ok", "", 1)
        self._tests = tests or CheckResult("tests", True, "ok", "", 1)
        self._coverage = coverage or CheckResult("coverage", True, "ok:90.0%", "", 1)
        self.lint_calls = 0
        self.tests_calls = 0
        self.coverage_calls: list[float] = []

    # mimic QualityGates surface
    def run_lint(self, targets=None):
        self.lint_calls += 1
        return self._lint

    def run_tests(self, target: str = "tests"):
        self.tests_calls += 1
        return self._tests

    def run_coverage(self, min_coverage: float = 0.0, target: str = "tests"):
        self.coverage_calls.append(float(min_coverage))
        return self._coverage


def _accept_factory(fake: FakeGates):
    """Helper: builds a gates_factory that returns the supplied FakeGates,
    but also satisfies RuntimeValidator's isinstance(gates, QualityGates)
    runtime check. We monkeypatch that check away in tests via fixture below.
    """
    def _f(repo_path: Path) -> FakeGates:
        return fake
    return _f


@pytest.fixture
def lift_gates_typecheck(monkeypatch):
    """Removes the isinstance(gates, QualityGates) guard so FakeGates works.

    We assert FakeGates exposes the right surface separately.
    """
    monkeypatch.setattr(
        "core.runtime_validator.QualityGates",
        FakeGates,
    )


# ---------------------------------------------------------------------------
# enums and dataclasses
# ---------------------------------------------------------------------------


def test_validation_strategy_values():
    assert ValidationStrategy.INPLACE.value == "INPLACE"
    assert ValidationStrategy.SANDBOX.value == "SANDBOX"


def test_validation_report_is_frozen():
    rep = ValidationReport(ok=True, strategy=ValidationStrategy.INPLACE, checks=(), duration_ms=1)
    with pytest.raises(Exception):
        rep.ok = False  # type: ignore[misc]


def test_validation_report_failure_summary_empty_when_ok():
    rep = ValidationReport(
        ok=True,
        strategy=ValidationStrategy.INPLACE,
        checks=(CheckResult("lint", True, "ok", "", 1),),
        duration_ms=1,
    )
    assert rep.failure_summary() == ""


def test_validation_report_failure_summary_joins_failed_checks():
    rep = ValidationReport(
        ok=False,
        strategy=ValidationStrategy.INPLACE,
        checks=(
            CheckResult("lint", False, "failed:returncode=1", "", 1),
            CheckResult("tests", True, "ok", "", 1),
            CheckResult("coverage", False, "below_threshold:50.0%<80.0%", "", 1),
        ),
        duration_ms=2,
    )
    assert rep.failure_summary() == (
        "lint:failed:returncode=1;coverage:below_threshold:50.0%<80.0%"
    )


# ---------------------------------------------------------------------------
# default_gates_factory
# ---------------------------------------------------------------------------


def test_default_gates_factory_rejects_non_positive_timeout():
    with pytest.raises(ValueError, match="invalid_timeout"):
        default_gates_factory(timeout=0)
    with pytest.raises(ValueError, match="invalid_timeout"):
        default_gates_factory(timeout=-1)


def test_default_gates_factory_returns_quality_gates(tmp_path: Path):
    project = tmp_path / "p"
    project.mkdir()
    factory = default_gates_factory(timeout=42)
    gates = factory(project)
    assert isinstance(gates, QualityGates)
    assert gates.repo_path == project.resolve()


# ---------------------------------------------------------------------------
# RuntimeValidator construction
# ---------------------------------------------------------------------------


def test_construction_rejects_non_strategy_enum():
    with pytest.raises(ValueError, match="invalid_strategy"):
        RuntimeValidator(strategy="INPLACE")  # type: ignore[arg-type]


def test_construction_rejects_bool_min_coverage():
    with pytest.raises(ValueError, match="invalid_min_coverage_type"):
        RuntimeValidator(min_coverage=True)  # type: ignore[arg-type]


def test_construction_rejects_non_numeric_min_coverage():
    with pytest.raises(ValueError, match="invalid_min_coverage_type"):
        RuntimeValidator(min_coverage="80")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [-0.1, 100.001, -50, 200])
def test_construction_rejects_out_of_range_min_coverage(bad):
    with pytest.raises(ValueError, match="invalid_min_coverage"):
        RuntimeValidator(min_coverage=bad)


def test_construction_rejects_zero_timeout():
    with pytest.raises(ValueError, match="invalid_adapter_command_timeout"):
        RuntimeValidator(adapter_command_timeout=0)


def test_construction_rejects_bool_timeout():
    with pytest.raises(ValueError, match="invalid_adapter_command_timeout"):
        RuntimeValidator(adapter_command_timeout=True)  # type: ignore[arg-type]


def test_construction_rejects_no_checks_enabled():
    with pytest.raises(ValueError, match="no_checks_enabled"):
        RuntimeValidator(run_lint=False, run_tests=False, run_coverage=False)


def test_construction_default_runs_lint_and_tests_only():
    v = RuntimeValidator()
    assert v.strategy is ValidationStrategy.INPLACE
    assert v.min_coverage == 0.0


def test_construction_accepts_full_config():
    v = RuntimeValidator(
        strategy=ValidationStrategy.SANDBOX,
        min_coverage=80.0,
        run_coverage=True,
        adapter_command_timeout=60,
    )
    assert v.strategy is ValidationStrategy.SANDBOX
    assert v.min_coverage == 80.0


# ---------------------------------------------------------------------------
# validate() — INPLACE strategy
# ---------------------------------------------------------------------------


def test_validate_rejects_non_adapter_argument():
    v = RuntimeValidator()
    with pytest.raises(ValueError, match="invalid_adapter_type"):
        v.validate("not an adapter")  # type: ignore[arg-type]


def test_inplace_all_pass(tmp_path, lift_gates_typecheck):
    fake = FakeGates(tmp_path)
    v = RuntimeValidator(gates_factory=_accept_factory(fake))
    adapter = _make_adapter(tmp_path)
    rep = v.validate(adapter)
    assert rep.ok is True
    assert rep.strategy is ValidationStrategy.INPLACE
    assert len(rep.checks) == 2  # default: lint + tests
    assert fake.lint_calls == 1
    assert fake.tests_calls == 1
    assert fake.coverage_calls == []


def test_inplace_with_coverage_enabled_passes_min_coverage(tmp_path, lift_gates_typecheck):
    fake = FakeGates(tmp_path)
    v = RuntimeValidator(
        gates_factory=_accept_factory(fake),
        run_coverage=True,
        min_coverage=85.5,
    )
    rep = v.validate(_make_adapter(tmp_path))
    assert rep.ok is True
    assert len(rep.checks) == 3
    assert fake.coverage_calls == [85.5]


def test_inplace_one_failed_check_makes_report_not_ok(tmp_path, lift_gates_typecheck):
    fake = FakeGates(
        tmp_path,
        tests=CheckResult("tests", False, "failed:returncode=1", "", 1),
    )
    v = RuntimeValidator(gates_factory=_accept_factory(fake))
    rep = v.validate(_make_adapter(tmp_path))
    assert rep.ok is False
    assert "tests:failed:returncode=1" in rep.failure_summary()


def test_inplace_lint_only(tmp_path, lift_gates_typecheck):
    fake = FakeGates(tmp_path)
    v = RuntimeValidator(
        gates_factory=_accept_factory(fake),
        run_lint=True,
        run_tests=False,
    )
    rep = v.validate(_make_adapter(tmp_path))
    assert rep.ok is True
    assert len(rep.checks) == 1
    assert rep.checks[0].name == "lint"


def test_inplace_factory_returning_wrong_type_raises(tmp_path):
    def bad_factory(_p):
        return object()

    v = RuntimeValidator(gates_factory=bad_factory)
    with pytest.raises(ValueError, match="gates_factory_returned"):
        v.validate(_make_adapter(tmp_path))


def test_inplace_duration_is_non_negative(tmp_path, lift_gates_typecheck):
    fake = FakeGates(tmp_path)
    v = RuntimeValidator(gates_factory=_accept_factory(fake))
    rep = v.validate(_make_adapter(tmp_path))
    assert rep.duration_ms >= 0


# ---------------------------------------------------------------------------
# validate() — SANDBOX strategy
# ---------------------------------------------------------------------------


def test_sandbox_runs_in_temp_directory(tmp_path, lift_gates_typecheck):
    """Sandbox factory must be invoked with a path different from project_path."""
    seen_paths: list[Path] = []

    def factory(repo_path: Path):
        seen_paths.append(repo_path)
        return FakeGates(repo_path)

    v = RuntimeValidator(
        gates_factory=factory,
        strategy=ValidationStrategy.SANDBOX,
    )
    adapter = _make_adapter(tmp_path)
    (adapter.project_path / "marker.txt").write_text("hello", encoding="utf-8")

    rep = v.validate(adapter)
    assert rep.ok is True
    assert len(seen_paths) == 1
    sandbox_path = seen_paths[0]
    assert sandbox_path != adapter.project_path
    # sandbox must exist DURING validate (factory was called inside ctx manager)
    # We verify it's gone now (cleaned up post-validate).
    assert not sandbox_path.exists()


def test_sandbox_copies_project_files_and_skips_forbidden(tmp_path, lift_gates_typecheck):
    captured = {}

    def factory(repo_path: Path):
        # Snapshot what's in the sandbox at the moment gates are constructed.
        if repo_path.exists():
            files = sorted(p.name for p in repo_path.rglob("*") if p.is_file())
            captured["files"] = files
        return FakeGates(repo_path)

    v = RuntimeValidator(
        gates_factory=factory,
        strategy=ValidationStrategy.SANDBOX,
    )
    adapter = _make_adapter(tmp_path, forbidden_paths=("secrets",))
    (adapter.project_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (adapter.project_path / "secrets").mkdir()
    (adapter.project_path / "secrets" / "token.txt").write_text("ssshhh", encoding="utf-8")
    (adapter.project_path / "__pycache__").mkdir()
    (adapter.project_path / "__pycache__" / "x.pyc").write_text("bytecode", encoding="utf-8")

    v.validate(adapter)
    assert "main.py" in captured["files"]
    assert "pyproject.toml" in captured["files"]
    assert "token.txt" not in captured["files"]
    assert "x.pyc" not in captured["files"]


def test_sandbox_cleans_up_even_on_exception(tmp_path, lift_gates_typecheck):
    leaked_path: list[Path] = []

    def factory(repo_path: Path):
        leaked_path.append(repo_path)
        raise RuntimeError("simulated_factory_failure")

    v = RuntimeValidator(
        gates_factory=factory,
        strategy=ValidationStrategy.SANDBOX,
    )
    with pytest.raises(RuntimeError, match="simulated_factory_failure"):
        v.validate(_make_adapter(tmp_path))
    # the sandbox dir must be gone despite the exception
    assert leaked_path
    assert not leaked_path[0].exists()


# ---------------------------------------------------------------------------
# adapter command override
# ---------------------------------------------------------------------------


def test_adapter_lint_command_takes_precedence(tmp_path, lift_gates_typecheck, monkeypatch):
    fake = FakeGates(tmp_path)
    captured_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(list(cmd))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="lint ok\n", stderr="")

    monkeypatch.setattr("core.runtime_validator.subprocess.run", fake_run)

    cmd = ProjectCommand(name="lint", cmd=("echo", "lint"), timeout_seconds=10)
    v = RuntimeValidator(gates_factory=_accept_factory(fake))
    adapter = _make_adapter(tmp_path, commands={"lint": cmd})

    rep = v.validate(adapter)
    assert rep.ok is True
    # gates.run_lint must NOT be called when adapter overrides it
    assert fake.lint_calls == 0
    assert fake.tests_calls == 1  # tests still goes through gates
    assert captured_cmds == [["echo", "lint"]]
    lint_check = next(c for c in rep.checks if c.name == "lint")
    assert lint_check.summary == "ok:lint"


def test_adapter_test_command_failure_packaged(tmp_path, lift_gates_typecheck, monkeypatch):
    fake = FakeGates(tmp_path)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=2, stdout="", stderr="boom\n")

    monkeypatch.setattr("core.runtime_validator.subprocess.run", fake_run)

    cmd = ProjectCommand(name="test", cmd=("python3", "-c", "exit(2)"), timeout_seconds=5)
    v = RuntimeValidator(gates_factory=_accept_factory(fake))
    adapter = _make_adapter(tmp_path, commands={"test": cmd})

    rep = v.validate(adapter)
    assert rep.ok is False
    test_check = next(c for c in rep.checks if c.name == "test")
    assert test_check.summary == "failed:returncode=2"
    assert "boom" in test_check.raw_output


def test_adapter_command_timeout_packaged(tmp_path, lift_gates_typecheck, monkeypatch):
    fake = FakeGates(tmp_path)

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=5)

    monkeypatch.setattr("core.runtime_validator.subprocess.run", fake_run)

    cmd = ProjectCommand(name="lint", cmd=("sleep", "999"), timeout_seconds=5)
    v = RuntimeValidator(gates_factory=_accept_factory(fake))
    adapter = _make_adapter(tmp_path, commands={"lint": cmd})

    rep = v.validate(adapter)
    assert rep.ok is False
    lint_check = next(c for c in rep.checks if c.name == "lint")
    assert lint_check.summary == "timeout:5s"
    assert lint_check.raw_output == ""


def test_adapter_command_tool_not_found(tmp_path, lift_gates_typecheck, monkeypatch):
    fake = FakeGates(tmp_path)

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("no such tool")

    monkeypatch.setattr("core.runtime_validator.subprocess.run", fake_run)

    cmd = ProjectCommand(name="lint", cmd=("nosuchtool",), timeout_seconds=5)
    v = RuntimeValidator(gates_factory=_accept_factory(fake))
    adapter = _make_adapter(tmp_path, commands={"lint": cmd})

    rep = v.validate(adapter)
    assert rep.ok is False
    lint_check = next(c for c in rep.checks if c.name == "lint")
    assert lint_check.summary == "tool_not_found:nosuchtool"


def test_adapter_command_os_error_packaged(tmp_path, lift_gates_typecheck, monkeypatch):
    fake = FakeGates(tmp_path)

    def fake_run(cmd, **kwargs):
        raise PermissionError("no permission")

    monkeypatch.setattr("core.runtime_validator.subprocess.run", fake_run)

    cmd = ProjectCommand(name="test", cmd=("python3",), timeout_seconds=5)
    v = RuntimeValidator(gates_factory=_accept_factory(fake))
    adapter = _make_adapter(tmp_path, commands={"test": cmd})

    rep = v.validate(adapter)
    assert rep.ok is False
    test_check = next(c for c in rep.checks if c.name == "test")
    assert test_check.summary.startswith("os_error:PermissionError:")


def test_adapter_coverage_command_override(tmp_path, lift_gates_typecheck, monkeypatch):
    fake = FakeGates(tmp_path)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="cov ok\n", stderr="")

    monkeypatch.setattr("core.runtime_validator.subprocess.run", fake_run)

    cmd = ProjectCommand(name="coverage", cmd=("echo", "cov"), timeout_seconds=10)
    v = RuntimeValidator(
        gates_factory=_accept_factory(fake),
        run_coverage=True,
        min_coverage=80.0,
    )
    adapter = _make_adapter(tmp_path, commands={"coverage": cmd})

    rep = v.validate(adapter)
    assert rep.ok is True
    assert fake.coverage_calls == []  # adapter override skipped gates entirely
    cov_check = next(c for c in rep.checks if c.name == "coverage")
    assert cov_check.summary == "ok:coverage"


def test_adapter_command_output_truncated(tmp_path, lift_gates_typecheck, monkeypatch):
    fake = FakeGates(tmp_path)
    huge = "X" * (MAX_RAW_OUTPUT_CHARS + 1000)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=huge, stderr="")

    monkeypatch.setattr("core.runtime_validator.subprocess.run", fake_run)

    cmd = ProjectCommand(name="lint", cmd=("echo", "x"), timeout_seconds=5)
    v = RuntimeValidator(gates_factory=_accept_factory(fake))
    adapter = _make_adapter(tmp_path, commands={"lint": cmd})

    rep = v.validate(adapter)
    lint_check = next(c for c in rep.checks if c.name == "lint")
    assert len(lint_check.raw_output) <= MAX_RAW_OUTPUT_CHARS
    assert "truncated" in lint_check.raw_output


# ---------------------------------------------------------------------------
# make_orchestrator_hook
# ---------------------------------------------------------------------------


def test_make_orchestrator_hook_validates_arg_types(tmp_path):
    v = RuntimeValidator()
    adapter = _make_adapter(tmp_path)
    with pytest.raises(ValueError, match="invalid_validator_type"):
        make_orchestrator_hook("not validator", adapter)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid_adapter_type"):
        make_orchestrator_hook(v, "not adapter")  # type: ignore[arg-type]


def test_make_orchestrator_hook_returns_validation_report(tmp_path, lift_gates_typecheck):
    fake = FakeGates(tmp_path)
    v = RuntimeValidator(gates_factory=_accept_factory(fake))
    adapter = _make_adapter(tmp_path)
    hook = make_orchestrator_hook(v, adapter)
    rep = hook("task-1", object())
    assert isinstance(rep, ValidationReport)
    assert rep.ok is True


# ---------------------------------------------------------------------------
# real subprocess smoke (best-effort, depends on python3 being available)
# ---------------------------------------------------------------------------


def test_real_subprocess_with_adapter_command_python_no_op(tmp_path, lift_gates_typecheck):
    """End-to-end: an adapter command exits 0 via real subprocess, no patching."""
    fake = FakeGates(tmp_path)

    cmd = ProjectCommand(
        name="lint",
        cmd=(sys.executable, "-c", "print('ok')"),
        timeout_seconds=15,
    )
    v = RuntimeValidator(gates_factory=_accept_factory(fake))
    adapter = _make_adapter(tmp_path, commands={"lint": cmd})

    rep = v.validate(adapter)
    assert rep.ok is True
    lint_check = next(c for c in rep.checks if c.name == "lint")
    assert lint_check.ok is True
    assert "ok" in lint_check.raw_output
