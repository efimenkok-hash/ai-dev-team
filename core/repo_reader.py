from pathlib import Path

TEXT_EXTENSIONS = {
    ".py", ".md", ".txt", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".js", ".ts", ".html", ".css"
}


def is_text_file(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXTENSIONS


EXCLUDED_PARTS = {
    ".venv",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


def is_excluded(path: Path) -> bool:
    return any(part in EXCLUDED_PARTS for part in path.parts) or path.name == ".DS_Store"


def list_project_files(root: str = ".") -> list[str]:
    base = Path(root)
    files = []

    for p in base.rglob("*"):
        if p.is_file() and is_text_file(p) and not is_excluded(p):
            files.append(str(p.relative_to(base)))

    files.sort()
    return files


def read_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def read_many(paths: list[str]) -> dict[str, str]:
    result = {}

    for item in paths:
        result[item] = read_file(item)

    return result
