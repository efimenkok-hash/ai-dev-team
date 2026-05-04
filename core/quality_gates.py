"""
core/quality_gates.py

Programmatic quality gates: lint (ruff), tests (pytest), coverage. Step 10 of
the ULTRA spec. Designed to be called both from CLI scripts and from the
orchestrator after the QA-agent verdict, so that an autonomous run cannot
finish SUCCESS while objective tooling reports issues.

CONTRACTS:
1. Every external tool runs through subprocess.run with cwd=repo_path,
   stdin=DEVNULL, no shell, deterministic env, timeout=DEFAULT_TIMEOUT.
2. Tooling absence (ruff/pytest/coverage missing) -> CheckResult(ok=False)
   with explicit "tool_not_found:<name>" summary; the process never raises.
3. min_coverage must be in [0.0, 100.0]; otherwise ValueError.
4. QualityReport.ok == AND of all included checks. Empty report -> ok=False.
5. duration_ms is monotonic (time.perf_counter), always >= 0.
6. raw_output is truncated to MAX_RAW_OUTPUT_CHARS to avoid memory blowups.
"""

import os
import shutil
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 120
MAX_RAW_OUTPUT_CHARS = 64_000

# Variables required for child Python tooling (find binaries, locate user
# config, write coverage data) but stripped of anything that could leak
# secrets between processes.
_PASSTHROUGH_ENV_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "PYTHONPATH",
    "COVERAGE_FILE",
    "TMPDIR",
)

_BASE_SUBPROCESS_ENV = {
    "LANG": "C",
    "LC_ALL": "C",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
}


def _build_subprocess_env(extra: dict | None = None) -> dict:
    env = dict(_BASE_SUBPROCESS_ENV)
    for key in _PASSTHROUGH_ENV_KEYS:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    if extra:
        env.update(extra)
    return env


# Backwards-compatible alias used by existing callers/tests.
_SUBPROCESS_ENV = _build_subprocess_env()


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    summary: str
    raw_output: str
    duration_ms: int


@dataclass(frozen=True)
class QualityReport:
    ok: bool
    checks: tuple[CheckResult, ...]


def _truncate(text: str) -> str:
    if len(text) <= MAX_RAW_OUTPUT_CHARS:
        return text
    head = text[: MAX_RAW_OUTPUT_CHARS - 64]
    return head + f"\n...[truncated {len(text) - len(head)} chars]"


def _run(
    cmd: list[str],
    cwd: Path,
    timeout: int,
    env: dict | None = None,
) -> tuple[subprocess.CompletedProcess | None, str | None]:
    """Execute a tool. Returns (proc, error). Exactly one of them is non-None."""
    if shutil.which(cmd[0]) is None and not Path(cmd[0]).is_absolute():
        return None, f"tool_not_found:{cmd[0]}"
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=env if env is not None else _build_subprocess_env(),
            timeout=timeout,
            check=False,
        )
        return proc, None
    except subprocess.TimeoutExpired:
        return None, f"timeout:{timeout}s"
    except FileNotFoundError:
        return None, f"tool_not_found:{cmd[0]}"
    except OSError as exc:
        return None, f"os_error:{type(exc).__name__}:{exc}"


def _check(
    name: str,
    cmd: list[str],
    cwd: Path,
    timeout: int,
    success_summary: str,
    env: dict | None = None,
) -> CheckResult:
    started = time.perf_counter()
    proc, err = _run(cmd, cwd, timeout, env=env)
    elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
    if err is not None:
        return CheckResult(
            name=name,
            ok=False,
            summary=err,
            raw_output="",
            duration_ms=elapsed_ms,
        )
    assert proc is not None
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return CheckResult(
            name=name,
            ok=True,
            summary=success_summary,
            raw_output=_truncate(output),
            duration_ms=elapsed_ms,
        )
    return CheckResult(
        name=name,
        ok=False,
        summary=f"failed:returncode={proc.returncode}",
        raw_output=_truncate(output),
        duration_ms=elapsed_ms,
    )


class QualityGates:
    def __init__(
        self,
        repo_path,
        python_executable: str = "python3",
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        extra_env: dict | None = None,
    ) -> None:
        path = Path(repo_path).resolve()
        if not path.exists():
            raise ValueError(f"repo_path_missing:{path}")
        if not path.is_dir():
            raise ValueError(f"repo_path_not_dir:{path}")
        if timeout <= 0:
            raise ValueError(f"invalid_timeout:{timeout}")
        if not python_executable or not python_executable.strip():
            raise ValueError("empty_python_executable")
        self._path = path
        self._python = python_executable
        self._timeout = timeout
        self._env = _build_subprocess_env(extra=extra_env)

    @property
    def repo_path(self) -> Path:
        return self._path

    def run_lint(
        self,
        targets: Iterable[str] | None = None,
    ) -> CheckResult:
        target_list = list(targets) if targets is not None else ["core", "tests"]
        cmd = [self._python, "-m", "ruff", "check", *target_list]
        return _check(
            "lint",
            cmd,
            self._path,
            self._timeout,
            success_summary="ok:no_lint_violations",
            env=self._env,
        )

    def run_tests(
        self,
        target: str = "tests",
    ) -> CheckResult:
        if not target or not target.strip():
            raise ValueError("empty_test_target")
        cmd = [self._python, "-m", "pytest", target, "-q", "-p", "no:cacheprovider"]
        return _check(
            "tests",
            cmd,
            self._path,
            self._timeout,
            success_summary="ok:all_tests_passed",
            env=self._env,
        )

    def run_coverage(
        self,
        min_coverage: float = 0.0,
        target: str = "tests",
    ) -> CheckResult:
        if not isinstance(min_coverage, (int, float)):
            raise ValueError("invalid_min_coverage_type")
        if min_coverage < 0.0 or min_coverage > 100.0:
            raise ValueError(f"invalid_min_coverage:{min_coverage}")
        if not target or not target.strip():
            raise ValueError("empty_test_target")
        cmd = [
            self._python,
            "-m",
            "coverage",
            "run",
            "--source=core",
            "-m",
            "pytest",
            target,
            "-q",
            "-p",
            "no:cacheprovider",
        ]
        run_started = time.perf_counter()
        run_proc, run_err = _run(cmd, self._path, self._timeout, env=self._env)
        if run_err is not None:
            elapsed_ms = max(0, int((time.perf_counter() - run_started) * 1000))
            return CheckResult(
                name="coverage",
                ok=False,
                summary=run_err,
                raw_output="",
                duration_ms=elapsed_ms,
            )
        assert run_proc is not None
        run_output = (run_proc.stdout or "") + (run_proc.stderr or "")
        if run_proc.returncode != 0:
            elapsed_ms = max(0, int((time.perf_counter() - run_started) * 1000))
            return CheckResult(
                name="coverage",
                ok=False,
                summary=f"tests_failed:returncode={run_proc.returncode}",
                raw_output=_truncate(run_output),
                duration_ms=elapsed_ms,
            )

        report_cmd = [self._python, "-m", "coverage", "report", "--precision=1"]
        report_proc, report_err = _run(report_cmd, self._path, self._timeout, env=self._env)
        elapsed_ms = max(0, int((time.perf_counter() - run_started) * 1000))
        if report_err is not None:
            return CheckResult(
                name="coverage",
                ok=False,
                summary=report_err,
                raw_output=_truncate(run_output),
                duration_ms=elapsed_ms,
            )
        assert report_proc is not None
        report_output = (report_proc.stdout or "") + (report_proc.stderr or "")
        if report_proc.returncode != 0:
            return CheckResult(
                name="coverage",
                ok=False,
                summary=f"coverage_report_failed:returncode={report_proc.returncode}",
                raw_output=_truncate(run_output + "\n---\n" + report_output),
                duration_ms=elapsed_ms,
            )

        percent = _parse_coverage_percent(report_output)
        if percent is None:
            return CheckResult(
                name="coverage",
                ok=False,
                summary="coverage_percent_unparseable",
                raw_output=_truncate(report_output),
                duration_ms=elapsed_ms,
            )
        if percent + 1e-6 < min_coverage:
            return CheckResult(
                name="coverage",
                ok=False,
                summary=f"below_threshold:{percent:.1f}%<{min_coverage:.1f}%",
                raw_output=_truncate(report_output),
                duration_ms=elapsed_ms,
            )
        return CheckResult(
            name="coverage",
            ok=True,
            summary=f"ok:{percent:.1f}%>={min_coverage:.1f}%",
            raw_output=_truncate(report_output),
            duration_ms=elapsed_ms,
        )

    def run_all(
        self,
        min_coverage: float = 0.0,
        lint_targets: Iterable[str] | None = None,
    ) -> QualityReport:
        results: list[CheckResult] = []
        results.append(self.run_lint(targets=lint_targets))
        results.append(self.run_tests())
        results.append(self.run_coverage(min_coverage=min_coverage))
        ok = all(r.ok for r in results) and bool(results)
        return QualityReport(ok=ok, checks=tuple(results))


def _parse_coverage_percent(report_text: str) -> float | None:
    """Pulls overall coverage % from a `coverage report` text output.

    The TOTAL line looks like: "TOTAL  120  10  92%" (with branch:
    "TOTAL  120  10  20  4  92%"). We pick the last token ending with '%'.
    """
    for line in reversed(report_text.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("TOTAL"):
            continue
        tokens = stripped.split()
        for tok in reversed(tokens):
            if tok.endswith("%"):
                try:
                    return float(tok.rstrip("%"))
                except ValueError:
                    return None
    return None
