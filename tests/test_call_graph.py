from pathlib import Path

from core.call_graph import build_call_graph


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_build_call_graph_links_function_to_callees(tmp_path: Path):
    _write(
        tmp_path / "m.py",
        "def helper():\n    return 1\n\n"
        "def main():\n    x = helper()\n    return x\n",
    )
    graph = build_call_graph(str(tmp_path))
    assert "helper" in graph["main"]


def test_build_call_graph_handles_method_calls_via_attribute(tmp_path: Path):
    _write(
        tmp_path / "m.py",
        "def runner():\n    obj.do_thing()\n",
    )
    graph = build_call_graph(str(tmp_path))
    assert "do_thing" in graph["runner"]


def test_build_call_graph_handles_async_functions(tmp_path: Path):
    _write(
        tmp_path / "m.py",
        "import asyncio\n"
        "async def fetch():\n    await asyncio.sleep(0)\n\n"
        "async def main():\n    await fetch()\n",
    )
    graph = build_call_graph(str(tmp_path))
    assert "fetch" in graph["main"]


def test_build_call_graph_returns_sorted_callees(tmp_path: Path):
    _write(
        tmp_path / "m.py",
        "def f():\n    z()\n    a()\n    m()\n\n"
        "def a(): pass\n\ndef m(): pass\n\ndef z(): pass\n",
    )
    graph = build_call_graph(str(tmp_path))
    assert graph["f"] == sorted(graph["f"])


def test_build_call_graph_empty_function_has_empty_callees(tmp_path: Path):
    _write(tmp_path / "m.py", "def empty():\n    pass\n")
    graph = build_call_graph(str(tmp_path))
    assert "empty" in graph
    assert graph["empty"] == []


def test_build_call_graph_excludes_venv(tmp_path: Path):
    _write(tmp_path / "core/x.py", "def f(): pass\n")
    _write(tmp_path / ".venv/lib/y.py", "def g(): pass\n")
    graph = build_call_graph(str(tmp_path))
    assert "f" in graph
    assert "g" not in graph


def test_build_call_graph_merges_calls_across_files(tmp_path: Path):
    _write(tmp_path / "a.py", "def shared():\n    other()\n\ndef other(): pass\n")
    _write(tmp_path / "b.py", "def shared():\n    third()\n\ndef third(): pass\n")
    graph = build_call_graph(str(tmp_path))
    assert "other" in graph["shared"]
    assert "third" in graph["shared"]


def test_build_call_graph_handles_module_with_no_functions(tmp_path: Path):
    _write(tmp_path / "m.py", "x = 1\ny = 2\n")
    graph = build_call_graph(str(tmp_path))
    assert graph == {}
