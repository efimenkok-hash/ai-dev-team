"""Tests for core.sandbox_runtime_hook (Step 14b-9)."""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.adapter import ProjectAdapter
from core.runtime_validator import (
    RuntimeValidator,
    ValidationReport,
    ValidationStrategy,
)
from core.sandbox_runtime_hook import make_sandbox_hook
from core.sandbox_workspace import WorktreeHandle

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_handle(path: Path) -> WorktreeHandle:
    return WorktreeHandle(
        task_id="test-task-1",
        branch="feature/test-task-1",
        path=path,
        created_at=time.time(),
    )


def _make_snapshot(writer_artifact: str | None = None) -> object:
    """Create a minimal duck-typed Snapshot substitute."""
    artifacts: dict = {}
    if writer_artifact is not None:
        artifacts["writer"] = writer_artifact

    class _Snap:
        pass

    snap = _Snap()
    snap.artifacts = artifacts  # type: ignore[attr-defined]
    return snap


def _simple_artifact(path: str = "hello.py", content: str = "x = 1\n") -> str:
    return json.dumps({"files": [{"path": path, "content": content}]})


def _adapter_factory(p: Path) -> ProjectAdapter:
    return ProjectAdapter(name="sandbox", project_path=p, language="python")


def _ok_report() -> ValidationReport:
    from core.quality_gates import CheckResult

    return ValidationReport(
        ok=True,
        strategy=ValidationStrategy.INPLACE,
        checks=(
            CheckResult(
                name="lint", ok=True, summary="ok", raw_output="", duration_ms=0
            ),
        ),
        duration_ms=1,
    )


def _fail_report() -> ValidationReport:
    from core.quality_gates import CheckResult

    return ValidationReport(
        ok=False,
        strategy=ValidationStrategy.INPLACE,
        checks=(
            CheckResult(
                name="lint",
                ok=False,
                summary="E501 line too long",
                raw_output="",
                duration_ms=0,
            ),
        ),
        duration_ms=1,
    )


# ---------------------------------------------------------------------------
# construction validation
# ---------------------------------------------------------------------------


def test_make_sandbox_hook_returns_callable(tmp_path):
    handle = _make_handle(tmp_path)
    validator = MagicMock(spec=RuntimeValidator)
    hook = make_sandbox_hook(handle, _adapter_factory, validator)
    assert callable(hook)


def test_make_sandbox_hook_rejects_bad_handle(tmp_path):
    validator = MagicMock(spec=RuntimeValidator)
    with pytest.raises(ValueError, match="invalid_handle_type"):
        make_sandbox_hook("not a handle", _adapter_factory, validator)  # type: ignore[arg-type]


def test_make_sandbox_hook_rejects_non_callable_factory(tmp_path):
    handle = _make_handle(tmp_path)
    validator = MagicMock(spec=RuntimeValidator)
    with pytest.raises(ValueError, match="adapter_factory_not_callable"):
        make_sandbox_hook(handle, "not callable", validator)  # type: ignore[arg-type]


def test_make_sandbox_hook_rejects_bad_validator(tmp_path):
    handle = _make_handle(tmp_path)
    with pytest.raises(ValueError, match="invalid_validator_type"):
        make_sandbox_hook(handle, _adapter_factory, "not a validator")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# hook invocation — happy path
# ---------------------------------------------------------------------------


def test_hook_writes_files_to_worktree(tmp_path):
    handle = _make_handle(tmp_path)
    mock_validator = MagicMock(spec=RuntimeValidator)
    mock_validator.validate.return_value = _ok_report()

    hook = make_sandbox_hook(handle, _adapter_factory, mock_validator)
    snapshot = _make_snapshot(_simple_artifact("out.py", "result = 42\n"))

    hook("task-1", snapshot)

    assert (tmp_path / "out.py").exists()
    assert (tmp_path / "out.py").read_text() == "result = 42\n"


def test_hook_calls_validator_with_adapter_for_worktree(tmp_path):
    handle = _make_handle(tmp_path)
    mock_validator = MagicMock(spec=RuntimeValidator)
    mock_validator.validate.return_value = _ok_report()

    captured_adapters: list[ProjectAdapter] = []

    def _capturing_factory(p: Path) -> ProjectAdapter:
        adapter = _adapter_factory(p)
        captured_adapters.append(adapter)
        return adapter

    hook = make_sandbox_hook(handle, _capturing_factory, mock_validator)
    snapshot = _make_snapshot(_simple_artifact())
    hook("task-1", snapshot)

    assert len(captured_adapters) == 1
    assert captured_adapters[0].project_path == tmp_path.resolve()


def test_hook_returns_report_unchanged(tmp_path):
    handle = _make_handle(tmp_path)
    expected = _ok_report()
    mock_validator = MagicMock(spec=RuntimeValidator)
    mock_validator.validate.return_value = expected

    hook = make_sandbox_hook(handle, _adapter_factory, mock_validator)
    snapshot = _make_snapshot(_simple_artifact())
    result = hook("task-1", snapshot)

    assert result is expected


def test_hook_returns_fail_report_unchanged(tmp_path):
    handle = _make_handle(tmp_path)
    expected = _fail_report()
    mock_validator = MagicMock(spec=RuntimeValidator)
    mock_validator.validate.return_value = expected

    hook = make_sandbox_hook(handle, _adapter_factory, mock_validator)
    snapshot = _make_snapshot(_simple_artifact())
    result = hook("task-1", snapshot)

    assert result is expected
    assert result.ok is False


def test_hook_validator_called_exactly_once(tmp_path):
    handle = _make_handle(tmp_path)
    mock_validator = MagicMock(spec=RuntimeValidator)
    mock_validator.validate.return_value = _ok_report()

    hook = make_sandbox_hook(handle, _adapter_factory, mock_validator)
    snapshot = _make_snapshot(_simple_artifact())
    hook("task-1", snapshot)

    mock_validator.validate.assert_called_once()


# ---------------------------------------------------------------------------
# hook invocation — error cases
# ---------------------------------------------------------------------------


def test_hook_raises_on_missing_writer_artifact(tmp_path):
    handle = _make_handle(tmp_path)
    mock_validator = MagicMock(spec=RuntimeValidator)
    hook = make_sandbox_hook(handle, _adapter_factory, mock_validator)
    snapshot = _make_snapshot(None)  # no writer key

    with pytest.raises(ValueError, match="missing_writer_artifact"):
        hook("task-1", snapshot)


def test_hook_raises_on_snapshot_without_artifacts(tmp_path):
    handle = _make_handle(tmp_path)
    mock_validator = MagicMock(spec=RuntimeValidator)
    hook = make_sandbox_hook(handle, _adapter_factory, mock_validator)

    class _NoArtifacts:
        pass

    with pytest.raises(ValueError, match="snapshot_missing_artifacts_attribute"):
        hook("task-1", _NoArtifacts())


def test_hook_propagates_invalid_json_error(tmp_path):
    handle = _make_handle(tmp_path)
    mock_validator = MagicMock(spec=RuntimeValidator)
    hook = make_sandbox_hook(handle, _adapter_factory, mock_validator)
    snapshot = _make_snapshot("not valid json {{{{")

    with pytest.raises(ValueError, match="invalid_json"):
        hook("task-1", snapshot)


def test_hook_propagates_path_escape_error(tmp_path):
    handle = _make_handle(tmp_path)
    mock_validator = MagicMock(spec=RuntimeValidator)
    mock_validator.validate.return_value = _ok_report()

    bad_artifact = json.dumps({"files": [{"path": "../../escape.py", "content": "x"}]})
    hook = make_sandbox_hook(handle, _adapter_factory, mock_validator)
    snapshot = _make_snapshot(bad_artifact)

    with pytest.raises(ValueError, match="path_escape"):
        hook("task-1", snapshot)


# ---------------------------------------------------------------------------
# Bug fix: hook must overlay fix artifact on top of writer baseline
# ---------------------------------------------------------------------------


def _make_snapshot_with_fix(writer_artifact: str, fix_artifact: str) -> object:
    """Snapshot that has both writer and fix artifacts."""
    class _Snap:
        pass
    snap = _Snap()
    snap.artifacts = {"writer": writer_artifact, "fix": fix_artifact}  # type: ignore[attr-defined]
    return snap


def test_hook_overlays_fix_artifact_on_writer_baseline(tmp_path):
    """When fix artifact exists, the hook must write it on top of the writer
    baseline so the worktree contains the corrected code, not the original."""
    writer = json.dumps({
        "files": [{"path": "calc.py", "content": "def add(a,b): return a+b\n"}]
    })
    fix = json.dumps({
        "files": [{"path": "calc.py", "content": "def add(a: int, b: int) -> int:\n    return a + b\n"}]
    })

    handle = _make_handle(tmp_path)
    mock_validator = MagicMock(spec=RuntimeValidator)
    mock_validator.validate.return_value = _ok_report()

    hook = make_sandbox_hook(handle, _adapter_factory, mock_validator)
    snapshot = _make_snapshot_with_fix(writer, fix)
    hook("task-fix-overlay", snapshot)

    result = (tmp_path / "calc.py").read_text(encoding="utf-8")
    # Must contain the FIXED version (with type annotations), not the original
    assert "int" in result, f"worktree should have fixed code, got: {result!r}"
    assert "a: int" in result, f"fix artifact should override writer, got: {result!r}"


def test_hook_uses_writer_when_no_fix_artifact(tmp_path):
    """Without a fix artifact the hook behaves exactly as before."""
    writer = json.dumps({
        "files": [{"path": "utils.py", "content": "def greet(): return 'hi'\n"}]
    })
    handle = _make_handle(tmp_path)
    mock_validator = MagicMock(spec=RuntimeValidator)
    mock_validator.validate.return_value = _ok_report()

    hook = make_sandbox_hook(handle, _adapter_factory, mock_validator)
    snapshot = _make_snapshot(writer)
    hook("task-no-fix", snapshot)

    result = (tmp_path / "utils.py").read_text(encoding="utf-8")
    assert "greet" in result


def test_hook_silently_ignores_malformed_fix_artifact(tmp_path):
    """If fix artifact is malformed JSON, hook falls back to writer baseline
    without raising — the validation gate for writer already ran successfully."""
    writer = json.dumps({
        "files": [{"path": "base.py", "content": "x = 1\n"}]
    })

    handle = _make_handle(tmp_path)
    mock_validator = MagicMock(spec=RuntimeValidator)
    mock_validator.validate.return_value = _ok_report()

    hook = make_sandbox_hook(handle, _adapter_factory, mock_validator)

    class _Snap:
        pass
    snap = _Snap()
    snap.artifacts = {"writer": writer, "fix": "THIS IS NOT JSON {{{{"}  # type: ignore[attr-defined]

    # Must not raise — fallback to writer baseline
    report = hook("task-bad-fix", snap)
    assert report.ok is True
    result = (tmp_path / "base.py").read_text(encoding="utf-8")
    assert "x = 1" in result
