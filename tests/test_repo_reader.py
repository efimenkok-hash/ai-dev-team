from pathlib import Path

import pytest

from core.repo_reader import (
    EXCLUDED_PARTS,
    TEXT_EXTENSIONS,
    is_excluded,
    is_text_file,
    list_project_files,
    read_file,
    read_many,
)


def test_is_text_file_true_for_known_extensions(tmp_path: Path):
    for ext in [".py", ".md", ".txt", ".json", ".yaml"]:
        p = tmp_path / f"a{ext}"
        p.write_text("x", encoding="utf-8")
        assert is_text_file(p) is True


def test_is_text_file_false_for_binary(tmp_path: Path):
    p = tmp_path / "img.png"
    p.write_text("x", encoding="utf-8")
    assert is_text_file(p) is False


def test_is_excluded_handles_excluded_parts(tmp_path: Path):
    for part in EXCLUDED_PARTS:
        p = tmp_path / part / "x.py"
        assert is_excluded(p) is True


def test_is_excluded_blocks_ds_store(tmp_path: Path):
    p = tmp_path / ".DS_Store"
    assert is_excluded(p) is True


def test_is_excluded_returns_false_for_normal_path(tmp_path: Path):
    p = tmp_path / "core" / "module.py"
    assert is_excluded(p) is False


def test_text_extensions_set_is_immutable_in_intent():
    # We do not want anyone to mutate the source-of-truth set.
    assert ".py" in TEXT_EXTENSIONS
    assert ".pyc" not in TEXT_EXTENSIONS


def test_list_project_files_collects_only_text_outside_excluded(tmp_path: Path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "a.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "core" / "b.py").write_text("y\n", encoding="utf-8")
    (tmp_path / "img.png").write_text("not text", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "ignored.py").write_text("ignored\n", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_text("pyc", encoding="utf-8")

    files = list_project_files(str(tmp_path))
    assert files == ["core/a.py", "core/b.py"]


def test_list_project_files_returns_sorted_paths(tmp_path: Path):
    (tmp_path / "z.md").write_text("z", encoding="utf-8")
    (tmp_path / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "m.txt").write_text("m", encoding="utf-8")
    files = list_project_files(str(tmp_path))
    assert files == ["a.py", "m.txt", "z.md"]


def test_read_file_reads_utf8(tmp_path: Path):
    p = tmp_path / "x.txt"
    p.write_text("Привет, мир\n", encoding="utf-8")
    assert read_file(str(p)) == "Привет, мир\n"


def test_read_many_returns_dict_of_paths(tmp_path: Path):
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("alpha\n", encoding="utf-8")
    b.write_text("beta\n", encoding="utf-8")
    result = read_many([str(a), str(b)])
    assert result == {str(a): "alpha\n", str(b): "beta\n"}


def test_read_many_empty_input_returns_empty_dict():
    assert read_many([]) == {}


def test_read_file_raises_for_missing_path(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        read_file(str(tmp_path / "no_such_file.py"))
