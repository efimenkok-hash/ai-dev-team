

def reverse_dependency_graph(graph: dict[str, list[str]]) -> dict[str, list[str]]:
    reversed_graph: dict[str, set[str]] = {}

    for module, deps in graph.items():
        reversed_graph.setdefault(module, set())

        for dep in deps:
            reversed_graph.setdefault(dep, set()).add(module)

    return {k: sorted(v) for k, v in reversed_graph.items()}


def impacted_modules(
    graph: dict[str, list[str]],
    changed_module: str,
) -> list[str]:
    reversed_graph = reverse_dependency_graph(graph)

    seen: set[str] = set()
    stack = [changed_module]

    while stack:
        current = stack.pop()

        for nxt in reversed_graph.get(current, []):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)

    result = sorted(seen)
    return result
