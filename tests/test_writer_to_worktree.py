"""Tests for core.writer_to_worktree (Step 14b-9)."""

import json

import pytest

from core.writer_to_worktree import write_artifact_to_worktree

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _json(files: list) -> str:
    return json.dumps({"files": files})


def _entry(path: str, content: str = "# ok\n") -> dict:
    return {"path": path, "content": content}


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_single_file_written(tmp_path):
    artifact = _json([_entry("src/foo.py", "def foo(): return 42\n")])
    written = write_artifact_to_worktree(artifact, tmp_path)
    assert len(written) == 1
    dest = tmp_path / "src" / "foo.py"
    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == "def foo(): return 42\n"


def test_returns_absolute_paths(tmp_path):
    artifact = _json([_entry("a.py", "x = 1\n")])
    written = write_artifact_to_worktree(artifact, tmp_path)
    assert all(p.is_absolute() for p in written)


def test_multiple_files_written(tmp_path):
    artifact = _json([
        _entry("src/a.py", "A = 1\n"),
        _entry("src/b.py", "B = 2\n"),
        _entry("tests/test_a.py", "from src.a import A\n"),
    ])
    written = write_artifact_to_worktree(artifact, tmp_path)
    assert len(written) == 3
    assert (tmp_path / "src" / "a.py").read_text() == "A = 1\n"
    assert (tmp_path / "src" / "b.py").read_text() == "B = 2\n"
    assert (tmp_path / "tests" / "test_a.py").read_text() == "from src.a import A\n"


def test_nested_directories_created(tmp_path):
    artifact = _json([_entry("a/b/c/d.py", "pass\n")])
    write_artifact_to_worktree(artifact, tmp_path)
    assert (tmp_path / "a" / "b" / "c" / "d.py").exists()


def test_empty_content_is_valid(tmp_path):
    artifact = _json([_entry("empty.py", "")])
    written = write_artifact_to_worktree(artifact, tmp_path)
    assert (tmp_path / "empty.py").read_text() == ""
    assert len(written) == 1


def test_returns_tuple(tmp_path):
    artifact = _json([_entry("x.py")])
    result = write_artifact_to_worktree(artifact, tmp_path)
    assert isinstance(result, tuple)


def test_content_utf8_preserved(tmp_path):
    content = "# кириллица и 日本語\n"
    artifact = _json([_entry("unicode.py", content)])
    write_artifact_to_worktree(artifact, tmp_path)
    assert (tmp_path / "unicode.py").read_text(encoding="utf-8") == content


# ---------------------------------------------------------------------------
# malicious / invalid paths
# ---------------------------------------------------------------------------


def test_absolute_path_raises(tmp_path):
    artifact = _json([_entry("/etc/passwd", "bad")])
    with pytest.raises(ValueError, match="path_escape"):
        write_artifact_to_worktree(artifact, tmp_path)


def test_dotdot_in_path_raises(tmp_path):
    artifact = _json([_entry("../../etc/passwd", "bad")])
    with pytest.raises(ValueError, match="path_escape"):
        write_artifact_to_worktree(artifact, tmp_path)


def test_dotdot_segment_anywhere_raises(tmp_path):
    artifact = _json([_entry("src/../../../etc/hosts", "bad")])
    with pytest.raises(ValueError, match="path_escape"):
        write_artifact_to_worktree(artifact, tmp_path)


def test_windows_style_absolute_path_raises(tmp_path):
    # On Linux this is treated as a relative path starting with "C:" —
    # it will NOT escape the worktree, so it should be written safely.
    # We just verify no crash and no real /etc write.
    artifact = _json([_entry("C:/Windows/System32/evil.txt", "nope")])
    written = write_artifact_to_worktree(artifact, tmp_path)
    # The path is relative on POSIX — it lands inside tmp_path
    assert all(str(p).startswith(str(tmp_path)) for p in written)


# ---------------------------------------------------------------------------
# invalid JSON / structure
# ---------------------------------------------------------------------------


def test_invalid_json_raises(tmp_path):
    with pytest.raises(ValueError, match="invalid_json"):
        write_artifact_to_worktree("not json {{{", tmp_path)


def test_non_object_json_raises(tmp_path):
    with pytest.raises(ValueError, match="artifact_must_be_object"):
        write_artifact_to_worktree('["files"]', tmp_path)


def test_missing_files_key_raises(tmp_path):
    with pytest.raises(ValueError, match="missing_files_key_or_not_list"):
        write_artifact_to_worktree('{"other": []}', tmp_path)


def test_files_not_list_raises(tmp_path):
    with pytest.raises(ValueError, match="missing_files_key_or_not_list"):
        write_artifact_to_worktree('{"files": "string"}', tmp_path)


def test_empty_files_list_raises(tmp_path):
    with pytest.raises(ValueError, match="empty_files_list"):
        write_artifact_to_worktree('{"files": []}', tmp_path)


def test_file_entry_not_dict_raises(tmp_path):
    artifact = json.dumps({"files": ["not a dict"]})
    with pytest.raises(ValueError, match="file_entry_0_not_dict"):
        write_artifact_to_worktree(artifact, tmp_path)


def test_missing_path_key_raises(tmp_path):
    artifact = json.dumps({"files": [{"content": "x"}]})
    with pytest.raises(ValueError, match="file_entry_0_missing_path"):
        write_artifact_to_worktree(artifact, tmp_path)


def test_empty_path_raises(tmp_path):
    artifact = json.dumps({"files": [{"path": "   ", "content": "x"}]})
    with pytest.raises(ValueError, match="file_entry_0_missing_path"):
        write_artifact_to_worktree(artifact, tmp_path)


def test_non_string_content_raises(tmp_path):
    artifact = json.dumps({"files": [{"path": "a.py", "content": 42}]})
    with pytest.raises(ValueError, match="file_entry_0_content_must_be_str"):
        write_artifact_to_worktree(artifact, tmp_path)


def test_non_string_artifact_raises(tmp_path):
    with pytest.raises(ValueError, match="writer_artifact_must_be_str"):
        write_artifact_to_worktree({"files": []}, tmp_path)  # type: ignore[arg-type]


def test_non_path_worktree_raises():
    artifact = _json([_entry("a.py")])
    with pytest.raises(ValueError, match="worktree_path_must_be_path"):
        write_artifact_to_worktree(artifact, "/tmp/worktree")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# no partial writes on validation failure
# ---------------------------------------------------------------------------


def test_no_partial_writes_on_bad_second_entry(tmp_path):
    """If the second entry has a bad path, no files should be written."""
    artifact = json.dumps({
        "files": [
            {"path": "good.py", "content": "x = 1\n"},
            {"path": "../../escape.py", "content": "evil"},
        ]
    })
    with pytest.raises(ValueError, match="path_escape"):
        write_artifact_to_worktree(artifact, tmp_path)
    # First file must NOT have been written (validation runs before writes).
    assert not (tmp_path / "good.py").exists()
