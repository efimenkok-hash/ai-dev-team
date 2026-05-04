from core.impact_analysis import impacted_modules, reverse_dependency_graph


def test_reverse_dependency_graph_basic():
    graph = {"a": ["b"], "b": ["c"], "c": []}
    rev = reverse_dependency_graph(graph)
    assert rev["b"] == ["a"]
    assert rev["c"] == ["b"]
    assert rev["a"] == []


def test_reverse_dependency_graph_multiple_dependents():
    graph = {"a": ["util"], "b": ["util"], "c": ["util"], "util": []}
    rev = reverse_dependency_graph(graph)
    assert rev["util"] == ["a", "b", "c"]


def test_reverse_dependency_graph_empty():
    assert reverse_dependency_graph({}) == {}


def test_impacted_modules_finds_transitive():
    graph = {"a": ["b"], "b": ["c"], "c": []}
    # Changing c affects b and a.
    assert impacted_modules(graph, "c") == ["a", "b"]


def test_impacted_modules_no_dependents_is_empty():
    graph = {"a": [], "b": []}
    assert impacted_modules(graph, "a") == []


def test_impacted_modules_diamond_shape():
    graph = {
        "core": [],
        "left": ["core"],
        "right": ["core"],
        "top": ["left", "right"],
    }
    impacted = impacted_modules(graph, "core")
    assert set(impacted) == {"left", "right", "top"}
    assert impacted == sorted(impacted)


def test_impacted_modules_unknown_module_returns_empty():
    graph = {"a": ["b"]}
    assert impacted_modules(graph, "nonexistent") == []


def test_impacted_modules_does_not_include_self():
    graph = {"x": ["y"], "y": []}
    assert "y" not in impacted_modules(graph, "y")


def test_impacted_modules_handles_cycle_safely():
    # a -> b -> a (cyclic). Algorithm must not loop forever.
    graph = {"a": ["b"], "b": ["a"]}
    result = impacted_modules(graph, "a")
    assert set(result) <= {"a", "b"}
