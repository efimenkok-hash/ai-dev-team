"""Unit tests for core.vector_store with the real FAISS index.

These tests do not mock FAISS. They exercise the actual L2 index to make
sure our wrapper preserves ordering, returns plain Python types, and
behaves correctly on edge cases (empty store, k > stored items).
"""

import faiss
import numpy as np
import pytest

from core.vector_store import VectorStore


def _vec(*xs: float) -> np.ndarray:
    return np.array(xs, dtype="float32")


def test_construction_initializes_empty_store():
    store = VectorStore(dim=4)
    assert store.dim == 4
    assert isinstance(store.index, faiss.IndexFlatL2)
    assert store.items == []
    assert store.index.ntotal == 0


def test_add_inserts_vectors_and_keeps_aligned_items():
    store = VectorStore(dim=3)
    vectors = np.array([[1.0, 0.0, 0.0],
                        [0.0, 1.0, 0.0]], dtype="float32")
    store.add(["one", "two"], vectors)
    assert store.items == ["one", "two"]
    assert store.index.ntotal == 2


def test_add_empty_input_is_noop():
    store = VectorStore(dim=3)
    store.add([], np.zeros((0, 3), dtype="float32"))
    assert store.items == []
    assert store.index.ntotal == 0


def test_search_returns_ranked_items():
    store = VectorStore(dim=3)
    store.add(
        ["x_axis", "y_axis", "z_axis"],
        np.array(
            [[1.0, 0.0, 0.0],
             [0.0, 1.0, 0.0],
             [0.0, 0.0, 1.0]],
            dtype="float32",
        ),
    )
    # Query closer to x_axis.
    result = store.search(_vec(0.9, 0.1, 0.0), k=3)
    assert len(result) == 3
    assert result[0][0] == "x_axis"


def test_search_returns_floats_for_distances():
    store = VectorStore(dim=2)
    store.add(["only"], np.array([[1.0, 0.0]], dtype="float32"))
    result = store.search(_vec(1.0, 0.0), k=1)
    assert result == [("only", pytest.approx(0.0, abs=1e-6))]
    assert isinstance(result[0][1], float)


def test_search_on_empty_store_returns_empty_list():
    store = VectorStore(dim=4)
    result = store.search(_vec(0.0, 0.0, 0.0, 0.0), k=5)
    assert result == []


def test_search_k_larger_than_store_returns_only_existing():
    store = VectorStore(dim=2)
    store.add(["a", "b"], np.array([[1.0, 0.0], [0.0, 1.0]], dtype="float32"))
    result = store.search(_vec(0.5, 0.5), k=10)
    # FAISS pads with -1 for missing; our wrapper filters those out.
    assert len(result) == 2
    assert {r[0] for r in result} == {"a", "b"}


def test_add_supports_incremental_inserts():
    store = VectorStore(dim=2)
    store.add(["a"], np.array([[1.0, 0.0]], dtype="float32"))
    store.add(["b"], np.array([[0.0, 1.0]], dtype="float32"))
    assert store.items == ["a", "b"]
    assert store.index.ntotal == 2


def test_search_ordering_is_by_ascending_distance():
    store = VectorStore(dim=1)
    store.add(
        ["far", "mid", "near"],
        np.array([[100.0], [10.0], [1.0]], dtype="float32"),
    )
    ordered = store.search(_vec(0.0), k=3)
    assert [name for name, _ in ordered] == ["near", "mid", "far"]
    distances = [d for _, d in ordered]
    assert distances == sorted(distances)
