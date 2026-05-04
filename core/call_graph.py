import ast
from pathlib import Path


class CallVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.current_function = ""
        self.graph: dict[str, set[str]] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        prev = self.current_function
        self.current_function = node.name
        self.graph.setdefault(node.name, set())
        self.generic_visit(node)
        self.current_function = prev

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self.current_function:
            name = ""

            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr

            if name:
                self.graph.setdefault(self.current_function, set()).add(name)

        self.generic_visit(node)


def build_call_graph(root: str = ".") -> dict[str, list[str]]:
    base = Path(root)
    merged: dict[str, set[str]] = {}

    for path in base.rglob("*.py"):
        if ".venv" in path.parts or "__pycache__" in path.parts:
            continue

        tree = ast.parse(path.read_text(encoding="utf-8"))
        visitor = CallVisitor()
        visitor.visit(tree)

        for fn, calls in visitor.graph.items():
            merged.setdefault(fn, set()).update(calls)

    return {k: sorted(v) for k, v in merged.items()}
