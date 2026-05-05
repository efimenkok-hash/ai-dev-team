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

from collections.abc import Callable
from pathlib import Path

from core.adapter import ProjectAdapter
from core.runtime_validator import RuntimeValidator, ValidationReport
from core.sandbox_workspace import WorktreeHandle
from core.writer_to_worktree import write_artifact_to_worktree

# Type alias that mirrors orchestrator.RuntimeValidationHook without importing
# the orchestrator (avoids circular dependency).
_Hook = Callable[[str, object], ValidationReport]


def make_sandbox_hook(
    handle: WorktreeHandle,
    adapter_factory: Callable[[Path], ProjectAdapter],
    validator: RuntimeValidator,
) -> _Hook:
    """Build a RuntimeValidationHook for the given worktree.

    Args:
        handle:          WorktreeHandle for the task's worktree (provides .path).
        adapter_factory: Callable[[Path], ProjectAdapter] — builds a ProjectAdapter
                         pointing at the given directory. Called lazily per hook
                         invocation so the adapter always sees the post-write state.
        validator:       Configured RuntimeValidator (INPLACE strategy recommended
                         for worktree use — the worktree IS the sandbox).

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

        # Materialise files inside the worktree.
        write_artifact_to_worktree(writer_artifact, handle.path)

        # Build an adapter targeting the worktree directory.
        adapter = adapter_factory(handle.path)

        # Run the validator and return the report unchanged.
        return validator.validate(adapter)

    return _hook
