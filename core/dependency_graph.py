import ast
from pathlib import Path


def _module_name(path: Path, root: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    return ".".join(rel.parts)


def _imports_from_source(source: str) -> set[str]:
    tree = ast.parse(source)
    result: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                result.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)

    return result


def build_dependency_graph(root: str = ".") -> dict[str, list[str]]:
    base = Path(root)
    graph: dict[str, list[str]] = {}

    for path in base.rglob("*.py"):
        if ".venv" in path.parts or "__pycache__" in path.parts:
            continue

        module = _module_name(path, base)
        source = path.read_text(encoding="utf-8")
        imports = sorted(_imports_from_source(source))
        graph[module] = imports

    return graph
