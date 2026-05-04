"""Unit tests for core.code_retriever.

We mock sentence_transformers.SentenceTransformer to avoid the torch
dependency in CI. The mock returns fixed-size deterministic vectors so the
retriever's behaviour (build_index over project files, top-k search) can
be verified without a real model.
"""

from pathlib import Path

import numpy as np
import pytest


class _FakeST:
    """Stand-in for sentence_transformers.SentenceTransformer.

    Each call to encode() returns a deterministic embedding: a 384-d vector
    where the first three coords are derived from the first 3 hashable
    chars of the text, and the rest are zeros. Good enough to differentiate
    inputs in tests.
    """

    def __init__(self, name: str):
        self.name = name

    def encode(self, texts, convert_to_numpy: bool = True) -> np.ndarray:
        out = np.zeros((len(texts), 384), dtype="float32")
        for i, t in enumerate(texts):
            for j, ch in enumerate(t[:3]):
                out[i, j] = float(ord(ch) % 97)
        return out


@pytest.fixture(autouse=True)
def _stub_sentence_transformer(monkeypatch):
    # Build a fake sentence_transformers module hierarchy if it isn't
    # importable. Then patch the symbol the retriever uses.
    import sys
    import types

    if "sentence_transformers" not in sys.modules:
        fake_module = types.ModuleType("sentence_transformers")
        fake_module.SentenceTransformer = _FakeST  # type: ignore[attr-defined]
        sys.modules["sentence_transformers"] = fake_module

    import core.code_retriever as cr

    monkeypatch.setattr(cr, "SentenceTransformer", _FakeST)
    return cr


def _seed_project(tmp_path: Path, files: list[tuple[str, str]]) -> Path:
    for rel, content in files:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_build_index_collects_text_files(tmp_path: Path, _stub_sentence_transformer):
    project = _seed_project(tmp_path, [
        ("a.py", "alpha"),
        ("b.py", "beta"),
        ("c.md", "gamma"),
    ])
    cr = _stub_sentence_transformer
    retriever = cr.CodeRetriever()
    retriever.build_index(str(project))
    assert sorted(retriever.paths) == ["a.py", "b.py", "c.md"]
    assert retriever.store.index.ntotal == 3


def test_build_index_skips_empty_files(tmp_path: Path, _stub_sentence_transformer):
    project = _seed_project(tmp_path, [
        ("good.py", "content"),
        ("empty.py", "   \n  "),
    ])
    cr = _stub_sentence_transformer
    retriever = cr.CodeRetriever()
    retriever.build_index(str(project))
    assert retriever.paths == ["good.py"]


def test_build_index_handles_no_text_files(tmp_path: Path, _stub_sentence_transformer):
    """Project with only a binary-like file -> empty index, no exception."""
    (tmp_path / "binary.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    cr = _stub_sentence_transformer
    retriever = cr.CodeRetriever()
    retriever.build_index(str(tmp_path))
    assert retriever.paths == []
    assert retriever.store.index.ntotal == 0


def test_search_returns_existing_paths(tmp_path: Path, _stub_sentence_transformer):
    project = _seed_project(tmp_path, [
        ("alpha.py", "alpha content"),
        ("beta.py", "beta content"),
    ])
    cr = _stub_sentence_transformer
    retriever = cr.CodeRetriever()
    retriever.build_index(str(project))
    results = retriever.search("alpha", k=2)
    assert all(r[0] in {"alpha.py", "beta.py"} for r in results)
    assert len(results) == 2


def test_search_k_limits_results(tmp_path: Path, _stub_sentence_transformer):
    project = _seed_project(tmp_path, [
        (f"file_{i}.py", f"content_{i}") for i in range(5)
    ])
    cr = _stub_sentence_transformer
    retriever = cr.CodeRetriever()
    retriever.build_index(str(project))
    results = retriever.search("query", k=2)
    assert len(results) == 2


def test_search_on_empty_index_returns_empty(_stub_sentence_transformer):
    cr = _stub_sentence_transformer
    retriever = cr.CodeRetriever()
    # No build_index call, so index is empty.
    results = retriever.search("anything", k=3)
    assert results == []


def test_build_index_truncates_large_files(tmp_path: Path, _stub_sentence_transformer):
    """Files larger than the truncation limit are still indexed without error."""
    big_content = "x" * 20_000
    project = _seed_project(tmp_path, [("big.py", big_content)])
    cr = _stub_sentence_transformer
    retriever = cr.CodeRetriever()
    retriever.build_index(str(project))
    assert retriever.paths == ["big.py"]


def test_model_name_is_minilm(_stub_sentence_transformer):
    cr = _stub_sentence_transformer
    assert cr.MODEL_NAME == "all-MiniLM-L6-v2"
