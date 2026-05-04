from pathlib import Path

import pytest

from core.patcher import (
    apply_change,
    generate_diff,
    preview_change,
    read_text,
    write_text,
)


def _seed(path: Path, content: str = "alpha\nbeta\n") -> Path:
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# read / write helpers
# ---------------------------------------------------------------------------


def test_read_text_reads_utf8(tmp_path: Path):
    p = _seed(tmp_path / "x.txt", "Привет\n")
    assert read_text(str(p)) == "Привет\n"


def test_write_text_writes_utf8(tmp_path: Path):
    p = tmp_path / "y.txt"
    write_text(str(p), "γ-line\n")
    assert p.read_text(encoding="utf-8") == "γ-line\n"


# ---------------------------------------------------------------------------
# generate_diff / preview_change
# ---------------------------------------------------------------------------


def test_generate_diff_returns_unified_diff_for_change(tmp_path: Path):
    p = _seed(tmp_path / "a.py", "a = 1\nb = 2\n")
    diff = generate_diff(str(p), "a = 1\nb = 99\n")
    assert "-b = 2" in diff
    assert "+b = 99" in diff
    assert str(p) in diff  # filename in header


def test_generate_diff_empty_for_no_change(tmp_path: Path):
    p = _seed(tmp_path / "b.py", "x\n")
    diff = generate_diff(str(p), "x\n")
    assert diff.strip() == ""


def test_preview_change_returns_no_changes_marker(tmp_path: Path):
    p = _seed(tmp_path / "c.py", "x\n")
    assert preview_change(str(p), "x\n") == "NO_CHANGES"


def test_preview_change_returns_diff_when_different(tmp_path: Path):
    p = _seed(tmp_path / "d.py", "x = 1\n")
    out = preview_change(str(p), "x = 2\n")
    assert "+x = 2" in out
    assert "-x = 1" in out


# ---------------------------------------------------------------------------
# apply_change
# ---------------------------------------------------------------------------


def test_apply_change_writes_file(tmp_path: Path):
    p = _seed(tmp_path / "e.py", "old\n")
    apply_change(str(p), "new content\n")
    assert p.read_text(encoding="utf-8") == "new content\n"


def test_apply_change_blocks_protected_file(tmp_path: Path):
    # Use the real protected path; file does not need to exist for validation.
    with pytest.raises(ValueError, match="protected_file:core/agents.py"):
        apply_change("core/agents.py", "x = 1\n")


def test_apply_change_blocks_forbidden_token(tmp_path: Path):
    p = _seed(tmp_path / "f.py", "x\n")
    with pytest.raises(ValueError, match="forbidden_token:TODO"):
        apply_change(str(p), "TODO: rewrite\n")
    # File must remain untouched.
    assert p.read_text(encoding="utf-8") == "x\n"


def test_apply_change_blocks_empty_content(tmp_path: Path):
    p = _seed(tmp_path / "g.py", "x\n")
    with pytest.raises(ValueError, match="empty_field:content"):
        apply_change(str(p), "")
    assert p.read_text(encoding="utf-8") == "x\n"


# ---------------------------------------------------------------------------
# atomicity guarantees
# ---------------------------------------------------------------------------


def test_apply_change_creates_parent_dirs(tmp_path: Path):
    target = tmp_path / "deep" / "nested" / "new_file.py"
    apply_change(str(target), "x = 1\n")
    assert target.read_text(encoding="utf-8") == "x = 1\n"


def test_apply_change_does_not_leave_tmp_files(tmp_path: Path):
    p = _seed(tmp_path / "h.py", "old\n")
    apply_change(str(p), "new\n")
    siblings = list(tmp_path.iterdir())
    assert all(not s.name.startswith(".tmp_patch_") for s in siblings)


def test_apply_change_cleans_tmp_on_write_error(tmp_path: Path, monkeypatch):
    p = _seed(tmp_path / "i.py", "old\n")

    def boom(src, dst):  # type: ignore[no-untyped-def]
        raise OSError("simulated_fs_failure")

    monkeypatch.setattr("core.patcher.os.replace", boom)
    with pytest.raises(OSError, match="simulated_fs_failure"):
        apply_change(str(p), "new\n")

    # Original file untouched.
    assert p.read_text(encoding="utf-8") == "old\n"
    # No leaked tmp file in the directory.
    siblings = list(tmp_path.iterdir())
    assert all(not s.name.startswith(".tmp_patch_") for s in siblings)


def test_apply_change_overwrites_atomically(tmp_path: Path):
    p = _seed(tmp_path / "j.py", "v1\n")
    apply_change(str(p), "v2 line one\nv2 line two\n")
    assert p.read_text(encoding="utf-8") == "v2 line one\nv2 line two\n"
