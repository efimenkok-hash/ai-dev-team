from pathlib import Path

from core.dependency_graph import build_dependency_graph


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_build_dependency_graph_collects_imports(tmp_path: Path):
    _write(tmp_path / "pkg/a.py", "import os\nimport json\n")
    _write(tmp_path / "pkg/b.py", "from collections import deque\nimport os\n")
    graph = build_dependency_graph(str(tmp_path))
    assert "json" in graph["pkg.a"]
    assert "os" in graph["pkg.a"]
    assert "collections" in graph["pkg.b"]
    assert "os" in graph["pkg.b"]


def test_build_dependency_graph_handles_relative_imports(tmp_path: Path):
    _write(tmp_path / "pkg/a.py", "from . import sibling\n")
    _write(tmp_path / "pkg/sibling.py", "x = 1\n")
    graph = build_dependency_graph(str(tmp_path))
    # Relative without module name gets node.module=None and is dropped.
    assert "pkg.a" in graph
    assert "pkg.sibling" in graph


def test_build_dependency_graph_excludes_venv(tmp_path: Path):
    _write(tmp_path / "core/x.py", "import json\n")
    _write(tmp_path / ".venv/lib/y.py", "import sys\n")
    graph = build_dependency_graph(str(tmp_path))
    assert "core.x" in graph
    assert not any(k.startswith(".venv") for k in graph)


def test_build_dependency_graph_excludes_pycache(tmp_path: Path):
    _write(tmp_path / "core/m.py", "import os\n")
    _write(tmp_path / "core/__pycache__/m.cpython-310.pyc", "binary content")
    graph = build_dependency_graph(str(tmp_path))
    assert "core.m" in graph
    assert not any("__pycache__" in k for k in graph)


def test_build_dependency_graph_returns_sorted_imports(tmp_path: Path):
    _write(tmp_path / "p/x.py", "import zeta\nimport alpha\nimport beta\n")
    graph = build_dependency_graph(str(tmp_path))
    assert graph["p.x"] == sorted(graph["p.x"])


def test_build_dependency_graph_empty_when_no_python(tmp_path: Path):
    _write(tmp_path / "doc.md", "# title\n")
    assert build_dependency_graph(str(tmp_path)) == {}
