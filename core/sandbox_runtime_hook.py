"""
core/sandbox_runtime_hook.py

Step 14b-9: factory that builds an Orchestrator-compatible RuntimeValidationHook
wired to a live worktree.

When the Orchestrator reaches the QA-PASS verdict it calls the hook as:

    report = hook(task_id, snapshot)   # snapshot.artifacts["writer"] is the JSON

The hook:
  1. Pulls the writer artifact from snapshot.artifacts["writer"].
  2. Calls write_artifact_to_worktree() to materialise files on disk.
  3. Builds a ProjectAdapter pointing at handle.path (the worktree).
  4. Calls validator.validate(adapter) and returns the ValidationReport.

The Orchestrator interprets report.ok=False as REJECTED and may route to FIX.

CONTRACTS:
1. make_sandbox_hook validates all three arguments at construction time;
   wrong types → ValueError.
2. The returned hook is a closure; it captures handle, adapter_factory,
   validator by reference (all are immutable / thread-safe after construction).
3. If snapshot has no "writer" artifact → ValueError("missing_writer_artifact").
4. path_escape or JSON errors from write_artifact_to_worktree propagate as
   ValueError (the orchestrator catches them as runtime_validator_exception).
5. ValidationReport is returned unchanged — the orchestrator owns interpretation.
"""

from __future__ import annotations

import ast
import contextlib
import json
from collections.abc import Callable
from pathlib import Path

from core.adapter import ProjectAdapter
from core.quality_gates import CheckResult
from core.runtime_validator import RuntimeValidator, ValidationReport
from core.sandbox_autofix import run_ruff_autofix
from core.sandbox_workspace import WorktreeHandle
from core.writer_to_worktree import write_artifact_to_worktree

# Type alias that mirrors orchestrator.RuntimeValidationHook without importing
# the orchestrator (avoids circular dependency).
_Hook = Callable[[str, object], ValidationReport]


_ADDITIVE_MARKERS = frozenset((
    "add ",
    "add_",
    "append",
    "добавь",
    "добавить",
    "добавляет",
    "добавь функцию",
    "плюс тест",
))


def _flatten_text(value: object) -> str:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value
        return value + "\n" + _flatten_text(decoded)
    if isinstance(value, dict):
        return "\n".join(_flatten_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(_flatten_text(item) for item in value)
    return str(value)


def _snapshot_mentions_additive_task(snapshot: object) -> bool:
    raw_task = getattr(snapshot, "raw_task", "")
    artifacts = getattr(snapshot, "artifacts", None)
    chunks: list[str] = []
    if isinstance(raw_task, str) and raw_task.strip():
        chunks.append(raw_task.lower())
    if hasattr(artifacts, "values"):
        chunks.extend(_flatten_text(value).lower() for value in artifacts.values())
    if not chunks:
        return False
    combined = "\n".join(chunks)
    return any(marker in combined for marker in _ADDITIVE_MARKERS)


def _public_python_defs(root: Path) -> dict[str, frozenset[str]]:
    result: dict[str, frozenset[str]] = {}
    if not root.exists():
        return result

    for file_path in root.rglob("*.py"):
        if any(part.startswith(".") for part in file_path.relative_to(root).parts):
            continue
        try:
            source = file_path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue

        names: set[str] = set()
        for node in tree.body:
            if isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ) and not node.name.startswith("_"):
                names.add(node.name)

        if names:
            rel = file_path.relative_to(root).as_posix()
            result[rel] = frozenset(names)

    return result


def _public_python_def_dumps(root: Path) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    if not root.exists():
        return result

    for file_path in root.rglob("*.py"):
        if any(part.startswith(".") for part in file_path.relative_to(root).parts):
            continue
        try:
            source = file_path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue

        named_dumps: dict[str, str] = {}
        for node in tree.body:
            if isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ) and not node.name.startswith("_"):
                named_dumps[node.name] = ast.dump(node, include_attributes=False)

        if named_dumps:
            rel = file_path.relative_to(root).as_posix()
            result[rel] = named_dumps

    return result


def _module_docstrings(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not root.exists():
        return result

    for file_path in root.rglob("*.py"):
        if any(part.startswith(".") for part in file_path.relative_to(root).parts):
            continue
        try:
            source = file_path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue

        docstring = ast.get_docstring(tree, clean=False)
        if docstring is None:
            continue

        rel = file_path.relative_to(root).as_posix()
        result[rel] = docstring

    return result


def _python_file_texts(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not root.exists():
        return result

    for file_path in root.rglob("*.py"):
        if any(part.startswith(".") for part in file_path.relative_to(root).parts):
            continue
        try:
            rel = file_path.relative_to(root).as_posix()
            result[rel] = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

    return result


def _missing_public_defs(
    before: dict[str, frozenset[str]],
    after: dict[str, frozenset[str]],
) -> tuple[str, ...]:
    missing: list[str] = []
    for rel_path, before_names in sorted(before.items()):
        after_names = after.get(rel_path, frozenset())
        for name in sorted(before_names - after_names):
            missing.append(f"{rel_path}:{name}")
    return tuple(missing)


def _modified_public_defs(
    before: dict[str, dict[str, str]],
    after: dict[str, dict[str, str]],
) -> tuple[str, ...]:
    modified: list[str] = []
    for rel_path, before_defs in sorted(before.items()):
        after_defs = after.get(rel_path, {})
        for name, before_dump in sorted(before_defs.items()):
            after_dump = after_defs.get(name)
            if after_dump is not None and after_dump != before_dump:
                modified.append(f"{rel_path}:{name}")
    return tuple(modified)


def _modified_module_docstrings(
    before: dict[str, str],
    after: dict[str, str],
) -> tuple[str, ...]:
    modified: list[str] = []
    for rel_path, before_docstring in sorted(before.items()):
        after_docstring = after.get(rel_path)
        if after_docstring != before_docstring:
            modified.append(f"{rel_path}:module_docstring")
    return tuple(modified)


def _reference_files_for_entries(
    raw_entries: tuple[str, ...],
    file_texts: dict[str, str],
) -> dict[str, str]:
    paths = sorted({
        entry.split(":", 1)[0]
        for entry in raw_entries
        if ":" in entry and entry.split(":", 1)[0] in file_texts
    })
    return {path: file_texts[path] for path in paths}


def _append_preservation_failure(
    report: ValidationReport,
    summary: str,
    raw_entries: tuple[str, ...],
    reference_files: dict[str, str] | None = None,
) -> ValidationReport:
    sections = ["\n".join(raw_entries)]
    if reference_files:
        for rel_path, content in sorted(reference_files.items()):
            sections.append(
                f"REFERENCE_FILE {rel_path}\n---\n{content.rstrip()}\n---"
            )
    raw = "\n\n".join(section for section in sections if section.strip())
    check = CheckResult(
        name="preservation_guard",
        ok=False,
        summary=summary,
        raw_output=raw,
        duration_ms=0,
    )
    return ValidationReport(
        ok=False,
        strategy=report.strategy,
        checks=(*report.checks, check),
        duration_ms=report.duration_ms,
    )


def make_sandbox_hook(
    handle: WorktreeHandle,
    adapter_factory: Callable[[Path], ProjectAdapter],
    validator: RuntimeValidator,
    *,
    autofix: bool = True,
) -> _Hook:
    """Build a RuntimeValidationHook for the given worktree.

    Args:
        handle:          WorktreeHandle for the task's worktree (provides .path).
        adapter_factory: Callable[[Path], ProjectAdapter] — builds a ProjectAdapter
                         pointing at the given directory. Called lazily per hook
                         invocation so the adapter always sees the post-write state.
        validator:       Configured RuntimeValidator (INPLACE strategy recommended
                         for worktree use — the worktree IS the sandbox).
        autofix:         If True (default), run `ruff format` + `ruff check --fix`
                         on the worktree BEFORE the validator's lint check. This
                         eliminates ~80% of trivial lint issues (whitespace, line
                         length, unused imports, import order) without an LLM
                         round-trip — the fixer_agent only sees real semantic
                         issues. Set False to disable for tests or when the
                         project itself has incompatible ruff config.

    Returns:
        A callable compatible with Orchestrator(runtime_validator=...) that:
          (task_id: str, snapshot: Snapshot) -> ValidationReport

    Raises:
        ValueError: if any argument has an unexpected type.
    """
    if not isinstance(handle, WorktreeHandle):
        raise ValueError(f"invalid_handle_type:{type(handle).__name__}")
    if not callable(adapter_factory):
        raise ValueError("adapter_factory_not_callable")
    if not isinstance(validator, RuntimeValidator):
        raise ValueError(f"invalid_validator_type:{type(validator).__name__}")
    if not isinstance(autofix, bool):
        raise ValueError(f"autofix_must_be_bool:{type(autofix).__name__}")

    # Capture the pristine worktree once. Runtime validation may run multiple
    # times inside QA->FIX loops; if we recalculate "before" from the mutated
    # worktree on each invocation, additive-preservation regressions can become
    # invisible after the first failed pass.
    initial_public_defs = _public_python_defs(handle.path)
    initial_public_def_dumps = _public_python_def_dumps(handle.path)
    initial_module_docstrings = _module_docstrings(handle.path)
    initial_python_files = _python_file_texts(handle.path)

    def _hook(task_id: str, snapshot: object) -> ValidationReport:
        # Pull writer artifact from snapshot.
        artifacts = getattr(snapshot, "artifacts", None)
        if artifacts is None:
            raise ValueError("snapshot_missing_artifacts_attribute")

        writer_artifact = artifacts.get("writer") if hasattr(artifacts, "get") else None
        if writer_artifact is None:
            raise ValueError("missing_writer_artifact")
        if not isinstance(writer_artifact, str):
            raise ValueError(
                f"writer_artifact_must_be_str:{type(writer_artifact).__name__}"
            )

        additive_task = _snapshot_mentions_additive_task(snapshot)
        public_defs_before = initial_public_defs if additive_task else {}
        public_def_dumps_before = initial_public_def_dumps if additive_task else {}
        module_docstrings_before = initial_module_docstrings if additive_task else {}

        # Materialise files inside the worktree.
        # Always write the original writer files as a baseline so the worktree
        # contains every file the architect specified.
        write_artifact_to_worktree(writer_artifact, handle.path)

        # If fixer_agent has run at least once, overlay its output on top of
        # the writer baseline.  The fix artifact uses the same {"files":[...]}
        # JSON schema as the writer, so write_artifact_to_worktree handles it
        # directly.  Errors are suppressed — the baseline is already on disk.
        fix_artifact = artifacts.get("fix") if hasattr(artifacts, "get") else None
        if isinstance(fix_artifact, str) and fix_artifact.strip():
            with contextlib.suppress(ValueError, OSError):
                write_artifact_to_worktree(fix_artifact, handle.path)

        # Auto-fix step: deterministic, free, eliminates ~80% of trivial lint
        # issues (whitespace, line length, unused imports). Runs BEFORE the
        # validator's strict lint check so the fixer_agent only ever sees
        # genuinely-broken code, not cosmetic violations.
        if autofix:
            with contextlib.suppress(Exception):
                run_ruff_autofix(handle.path)

        # Build an adapter targeting the worktree directory.
        adapter = adapter_factory(handle.path)

        # Run the validator first, then add deterministic semantic guards that
        # cannot be reliably delegated to LLM self-review.
        report = validator.validate(adapter)

        if additive_task:
            public_defs_after = _public_python_defs(handle.path)
            public_def_dumps_after = _public_python_def_dumps(handle.path)
            module_docstrings_after = _module_docstrings(handle.path)
            missing = _missing_public_defs(public_defs_before, public_defs_after)
            if missing:
                reference_files = _reference_files_for_entries(
                    missing,
                    initial_python_files,
                )
                report = _append_preservation_failure(
                    report,
                    f"deleted_public_defs:{len(missing)}",
                    missing,
                    reference_files,
                )

            modified_defs = _modified_public_defs(
                public_def_dumps_before,
                public_def_dumps_after,
            )
            if modified_defs:
                reference_files = _reference_files_for_entries(
                    modified_defs,
                    initial_python_files,
                )
                report = _append_preservation_failure(
                    report,
                    f"modified_public_defs:{len(modified_defs)}",
                    modified_defs,
                    reference_files,
                )

            modified_docstrings = _modified_module_docstrings(
                module_docstrings_before,
                module_docstrings_after,
            )
            if modified_docstrings:
                reference_files = _reference_files_for_entries(
                    modified_docstrings,
                    initial_python_files,
                )
                report = _append_preservation_failure(
                    report,
                    f"modified_module_docstrings:{len(modified_docstrings)}",
                    modified_docstrings,
                    reference_files,
                )

        return report

    return _hook
