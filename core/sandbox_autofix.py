"""
core/sandbox_autofix.py

Step A2-followup: auto-fix lint issues in the worktree BEFORE validation.

Most cheap/medium LLMs (qwen-coder, gpt-4o-mini, claude-haiku) produce code
that's substantively correct but has trivial lint issues:
  - missing newline at EOF
  - trailing whitespace
  - line length > 100 chars
  - import ordering
  - unused imports

These are 80% of all ruff failures we observe in real LLM runs. Fixing them
via the fixer_agent is wasteful: another LLM call, another pass through
review/test/qa, lots of tokens. Running `ruff format` + `ruff check --fix`
auto-resolves them deterministically and for free.

Pipeline order in `make_sandbox_hook`:
  1. write writer artifact → worktree (baseline)
  2. overlay fixer artifact (if any) → worktree
  3. **run ruff auto-fix here** (this module)
  4. run runtime_validator (lint check) — only sees real, semantic issues now

CONTRACTS:
1. run_ruff_autofix(path) is total — never raises. Subprocess errors are
   logged via the returned summary but don't propagate (validator runs next
   and will catch any remaining issues).
2. ruff binary is invoked via `<python> -m ruff` so it picks up the same
   ruff installed in the running interpreter — no PATH ambiguity.
3. Each command has an enforced timeout (default 30s) to bound runaway calls.
4. AutofixResult is frozen and reports each command's outcome for diagnostics.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_AUTOFIX_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class AutofixCommandResult:
    """Outcome of one ruff invocation. ok=True means subprocess returned 0
    (or 1 for `ruff check --fix` which signals 'fixes applied'). Errors are
    captured for logging but never raised."""

    name: str
    ok: bool
    returncode: int
    stdout_excerpt: str
    stderr_excerpt: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("empty_name")
        if not isinstance(self.ok, bool):
            raise ValueError("ok_must_be_bool")
        if not isinstance(self.returncode, int) or isinstance(self.returncode, bool):
            raise ValueError("returncode_must_be_int")
        if not isinstance(self.stdout_excerpt, str):
            raise ValueError("stdout_excerpt_must_be_str")
        if not isinstance(self.stderr_excerpt, str):
            raise ValueError("stderr_excerpt_must_be_str")


@dataclass(frozen=True)
class AutofixResult:
    """Aggregate outcome of run_ruff_autofix. results is the per-command list
    in execution order; the last fields are convenience accessors."""

    results: tuple[AutofixCommandResult, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.results, tuple):
            raise ValueError("results_must_be_tuple")
        for r in self.results:
            if not isinstance(r, AutofixCommandResult):
                raise ValueError(
                    f"invalid_result_type:{type(r).__name__}"
                )

    @property
    def all_ok(self) -> bool:
        return all(r.ok for r in self.results)

    def summary(self) -> str:
        """Single-line summary suitable for logging."""
        parts = [
            f"{r.name}={'ok' if r.ok else f'fail(rc={r.returncode})'}"
            for r in self.results
        ]
        return ";".join(parts)


def run_ruff_autofix(
    path: Path,
    *,
    timeout: int = DEFAULT_AUTOFIX_TIMEOUT_SECONDS,
    python_executable: str | None = None,
    runner: _AutofixRunner | None = None,
) -> AutofixResult:
    """Run ruff format + ruff check --fix in `path`. Total — never raises.

    Args:
        path: Directory to operate on (typically a worktree). Must exist.
        timeout: Per-command timeout in seconds.
        python_executable: Python to use for `python -m ruff`. Defaults to
            the running interpreter (sys.executable).
        runner: Optional injection point for tests. When None, uses
            `_DefaultAutofixRunner` (real subprocess.run).

    Returns:
        AutofixResult with per-command outcomes. Use `.all_ok` to check
        overall success and `.summary()` for logging.

    Raises:
        ValueError: only on bad arguments (path not a Path, etc.) — never
            from subprocess errors.
    """
    if not isinstance(path, Path):
        raise ValueError(f"path_must_be_path:{type(path).__name__}")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ValueError(f"invalid_timeout:{timeout!r}")
    if python_executable is not None and (
        not isinstance(python_executable, str) or not python_executable.strip()
    ):
        raise ValueError("empty_python_executable")
    if runner is not None and not isinstance(runner, _AutofixRunner):
        raise ValueError(
            f"invalid_runner_type:{type(runner).__name__}"
        )

    py = python_executable or sys.executable
    _runner = runner if runner is not None else _DefaultAutofixRunner()

    # Order matters:
    #   1. ruff format → reformats whitespace, line breaks, quotes (E501 etc).
    #   2. ruff check --fix --unsafe-fixes → applies remaining auto-fixes
    #      (unused imports, sorted imports, simplifications).
    # `--unsafe-fixes` includes fixes that are correct but might change
    # semantics in edge cases; that's acceptable here because the next step
    # is a strict lint check that catches anything broken.
    commands = (
        ("format", (py, "-m", "ruff", "format", str(path))),
        (
            "check_fix",
            (py, "-m", "ruff", "check", "--fix", "--unsafe-fixes", str(path)),
        ),
    )

    results: list[AutofixCommandResult] = []
    for name, cmd in commands:
        result = _runner.run(cmd=cmd, cwd=str(path), timeout=timeout)
        # ruff returns 0 when no fixes needed, 0 after successful fix.
        # `ruff check --fix` returns 1 if it found violations it could NOT
        # auto-fix (but it still applies the fixes it could). We treat that
        # as ok=True — the validator will catch remaining issues.
        ok = result.returncode in (0, 1)
        results.append(
            AutofixCommandResult(
                name=name,
                ok=ok,
                returncode=result.returncode,
                stdout_excerpt=_excerpt(result.stdout),
                stderr_excerpt=_excerpt(result.stderr),
            )
        )

    return AutofixResult(results=tuple(results))


# ---------------------------------------------------------------------------
# Subprocess plumbing — testable via injection.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _AutofixRunResult:
    returncode: int
    stdout: str
    stderr: str


class _AutofixRunner:
    """Protocol-style runner. Tests substitute their own."""

    def run(
        self,
        *,
        cmd: tuple[str, ...],
        cwd: str,
        timeout: int,
    ) -> _AutofixRunResult:  # pragma: no cover — protocol
        raise NotImplementedError


class _DefaultAutofixRunner(_AutofixRunner):
    def run(
        self,
        *,
        cmd: tuple[str, ...],
        cwd: str,
        timeout: int,
    ) -> _AutofixRunResult:
        try:
            proc = subprocess.run(
                list(cmd),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return _AutofixRunResult(
                returncode=124,
                stdout="",
                stderr=f"timeout:{timeout}s",
            )
        except FileNotFoundError as exc:
            return _AutofixRunResult(
                returncode=127,
                stdout="",
                stderr=f"command_not_found:{exc}",
            )
        except OSError as exc:
            return _AutofixRunResult(
                returncode=126,
                stdout="",
                stderr=f"os_error:{exc}",
            )
        return _AutofixRunResult(
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )


def _excerpt(text: str | None, limit: int = 500) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit] + "...[truncated]"
