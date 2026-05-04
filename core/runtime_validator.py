"""
core/runtime_validator.py

Step 15 of the ULTRA spec: runtime validation. Static QA-agent verdicts are
necessary but not sufficient — they cannot tell us whether the produced code
actually compiles, lints clean, or passes its own tests when executed. This
module bridges that gap by running real tooling (ruff/pytest/coverage, or any
adapter-defined commands) against either the project tree or a sandbox copy
of it, and packaging the result as a frozen ValidationReport that the
orchestrator consumes inside the QA state to decide SUCCESS vs FIX.

This closes Risk C: "pipeline returns SUCCESS on code that fails real
tooling".

CONTRACTS:
1. RuntimeValidator(...) is constructed once. It does not own a QualityGates
   instance directly — instead it accepts a *factory* so each .validate()
   call may target a different repo path (sandbox vs in-place).
2. validate() never raises on tool absence, timeout, or subprocess error:
   every failure mode is represented inside CheckResult(ok=False, ...).
   It DOES raise ValueError on programmer mistakes (wrong adapter type).
3. Two strategies via ValidationStrategy enum:
   - INPLACE: run gates against adapter.project_path directly.
   - SANDBOX: copy project to a temp dir, run gates there, then unconditionally
     clean up (even on validator exception).
4. Adapter-defined commands take precedence over default gates. If an
   adapter exposes a command named "lint"/"test"/"coverage", the validator
   uses it instead of the built-in QualityGates step.
5. Sandbox copy honors adapter.forbidden_paths and a built-in junk-dir list
   (.git, .venv, node_modules, __pycache__, etc.) so heavy/protected dirs
   never leak into the sandbox.
6. ValidationReport.ok == AND of all included checks. Empty report -> ok=False.
7. Output sizes are bounded by quality_gates.MAX_RAW_OUTPUT_CHARS through
   the delegated CheckResult and a local _truncate fallback for adapter
   commands.
8. ValidationReport.failure_summary() yields a stable, joinable string for
   logging and FAIL reasons.
"""

import contextlib
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from core.adapter import ProjectAdapter, ProjectCommand
from core.quality_gates import (
    MAX_RAW_OUTPUT_CHARS,
    CheckResult,
    QualityGates,
    _build_subprocess_env,
)


class ValidationStrategy(str, Enum):
    INPLACE = "INPLACE"
    SANDBOX = "SANDBOX"


# Directories never copied into the sandbox: heavy, regenerable, or risky.
# Adapter.forbidden_paths is added on top of this.
_SANDBOX_IGNORE = frozenset({
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".coverage",
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "build",
    "dist",
    "htmlcov",
    ".DS_Store",
    "pytest-cache-files",
})


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    strategy: ValidationStrategy
    checks: tuple[CheckResult, ...]
    duration_ms: int

    def failure_summary(self) -> str:
        bad = [c for c in self.checks if not c.ok]
        if not bad:
            return ""
        return ";".join(f"{c.name}:{c.summary}" for c in bad)


# Factory: given a repo path, return a configured QualityGates instance.
GatesFactory = Callable[[Path], QualityGates]


def default_gates_factory(timeout: int = 120) -> GatesFactory:
    """Builds a GatesFactory that constructs QualityGates with the given timeout.

    Use this when you want vanilla QualityGates behavior. For custom configs
    (extra_env, alternate python_executable), supply your own factory.
    """
    if not isinstance(timeout, int) or timeout <= 0:
        raise ValueError(f"invalid_timeout:{timeout}")

    def _factory(repo_path: Path) -> QualityGates:
        return QualityGates(repo_path=repo_path, timeout=timeout)

    return _factory


def _truncate(text: str) -> str:
    if len(text) <= MAX_RAW_OUTPUT_CHARS:
        return text
    head = text[: MAX_RAW_OUTPUT_CHARS - 64]
    return head + f"\n...[truncated {len(text) - len(head)} chars]"


class RuntimeValidator:
    """Runs real tooling against the project (or a sandbox copy) and produces
    a ValidationReport that the orchestrator consults inside the QA state.

    Typical use:
        adapter = ProjectAdapter(name="hk2", project_path=..., language="python")
        validator = RuntimeValidator(strategy=ValidationStrategy.INPLACE)
        hook = lambda task_id, snap: validator.validate(adapter)
        orch = Orchestrator(memory, agents, runtime_validator=hook)
    """

    def __init__(
        self,
        gates_factory: GatesFactory | None = None,
        strategy: ValidationStrategy = ValidationStrategy.INPLACE,
        min_coverage: float = 0.0,
        run_lint: bool = True,
        run_tests: bool = True,
        run_coverage: bool = False,
        adapter_command_timeout: int = 120,
    ) -> None:
        if not isinstance(strategy, ValidationStrategy):
            raise ValueError(f"invalid_strategy:{strategy}")
        if isinstance(min_coverage, bool) or not isinstance(min_coverage, (int, float)):
            raise ValueError("invalid_min_coverage_type")
        if min_coverage < 0.0 or min_coverage > 100.0:
            raise ValueError(f"invalid_min_coverage:{min_coverage}")
        if (
            not isinstance(adapter_command_timeout, int)
            or isinstance(adapter_command_timeout, bool)
            or adapter_command_timeout <= 0
        ):
            raise ValueError(f"invalid_adapter_command_timeout:{adapter_command_timeout}")
        if not (run_lint or run_tests or run_coverage):
            raise ValueError("no_checks_enabled")

        self._gates_factory: GatesFactory = gates_factory or default_gates_factory()
        self._strategy = strategy
        self._min_coverage = float(min_coverage)
        self._run_lint = bool(run_lint)
        self._run_tests = bool(run_tests)
        self._run_coverage = bool(run_coverage)
        self._adapter_cmd_timeout = adapter_command_timeout

    @property
    def strategy(self) -> ValidationStrategy:
        return self._strategy

    @property
    def min_coverage(self) -> float:
        return self._min_coverage

    def validate(self, adapter: ProjectAdapter) -> ValidationReport:
        if not isinstance(adapter, ProjectAdapter):
            raise ValueError(
                f"invalid_adapter_type:{type(adapter).__name__}"
            )

        started = time.perf_counter()
        if self._strategy is ValidationStrategy.INPLACE:
            checks = self._run_in_directory(adapter, adapter.project_path)
        else:
            with _sandbox(adapter) as sandbox_root:
                checks = self._run_in_directory(adapter, sandbox_root)

        elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
        ok = all(c.ok for c in checks) and bool(checks)
        return ValidationReport(
            ok=ok,
            strategy=self._strategy,
            checks=tuple(checks),
            duration_ms=elapsed_ms,
        )

    def _run_in_directory(
        self,
        adapter: ProjectAdapter,
        cwd: Path,
    ) -> list[CheckResult]:
        results: list[CheckResult] = []
        gates = self._gates_factory(cwd)
        if not isinstance(gates, QualityGates):
            raise ValueError(
                f"gates_factory_returned:{type(gates).__name__}"
            )

        if self._run_lint:
            results.append(self._lint(adapter, gates))
        if self._run_tests:
            results.append(self._tests(adapter, gates))
        if self._run_coverage:
            results.append(self._coverage(adapter, gates))
        return results

    def _lint(
        self,
        adapter: ProjectAdapter,
        gates: QualityGates,
    ) -> CheckResult:
        cmd = adapter.commands.get("lint")
        if cmd is not None:
            return self._run_adapter_command(cmd, gates.repo_path)
        return gates.run_lint()

    def _tests(
        self,
        adapter: ProjectAdapter,
        gates: QualityGates,
    ) -> CheckResult:
        cmd = adapter.commands.get("test")
        if cmd is not None:
            return self._run_adapter_command(cmd, gates.repo_path)
        return gates.run_tests()

    def _coverage(
        self,
        adapter: ProjectAdapter,
        gates: QualityGates,
    ) -> CheckResult:
        cmd = adapter.commands.get("coverage")
        if cmd is not None:
            return self._run_adapter_command(cmd, gates.repo_path)
        return gates.run_coverage(min_coverage=self._min_coverage)

    def _run_adapter_command(
        self,
        command: ProjectCommand,
        cwd: Path,
    ) -> CheckResult:
        env = _build_subprocess_env()
        timeout = command.timeout_seconds
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                list(command.cmd),
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
            return CheckResult(
                name=command.name,
                ok=False,
                summary=f"timeout:{timeout}s",
                raw_output="",
                duration_ms=elapsed_ms,
            )
        except FileNotFoundError:
            elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
            return CheckResult(
                name=command.name,
                ok=False,
                summary=f"tool_not_found:{command.cmd[0]}",
                raw_output="",
                duration_ms=elapsed_ms,
            )
        except OSError as exc:
            elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
            return CheckResult(
                name=command.name,
                ok=False,
                summary=f"os_error:{type(exc).__name__}:{exc}",
                raw_output="",
                duration_ms=elapsed_ms,
            )

        elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
        output = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0:
            return CheckResult(
                name=command.name,
                ok=True,
                summary=f"ok:{command.name}",
                raw_output=_truncate(output),
                duration_ms=elapsed_ms,
            )
        return CheckResult(
            name=command.name,
            ok=False,
            summary=f"failed:returncode={proc.returncode}",
            raw_output=_truncate(output),
            duration_ms=elapsed_ms,
        )


@contextlib.contextmanager
def _sandbox(adapter: ProjectAdapter):
    """Materialize a sandbox copy of the project, honoring forbidden_paths.

    Yields the sandbox path; cleans up unconditionally even on exceptions.
    """
    parent = tempfile.mkdtemp(prefix="aidt_sandbox_")
    target = Path(parent) / adapter.name
    try:
        forbidden = set(adapter.forbidden_paths)

        def _ignore(_root: str, names: list[str]) -> list[str]:
            skipped: list[str] = []
            for name in names:
                if name in _SANDBOX_IGNORE or name in forbidden:
                    skipped.append(name)
            return skipped

        shutil.copytree(adapter.project_path, target, ignore=_ignore)
        yield target
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def make_orchestrator_hook(
    validator: RuntimeValidator,
    adapter: ProjectAdapter,
):
    """Convenience: builds a hook compatible with Orchestrator.runtime_validator.

    The orchestrator passes (task_id, snapshot) to the hook; we ignore both
    here because the validation target is fully captured by the adapter.
    Custom hooks (e.g. picking adapter from a registry by snapshot.task_id)
    can be written by callers directly.
    """
    if not isinstance(validator, RuntimeValidator):
        raise ValueError(
            f"invalid_validator_type:{type(validator).__name__}"
        )
    if not isinstance(adapter, ProjectAdapter):
        raise ValueError(
            f"invalid_adapter_type:{type(adapter).__name__}"
        )

    def _hook(_task_id: str, _snapshot) -> ValidationReport:
        return validator.validate(adapter)

    return _hook
