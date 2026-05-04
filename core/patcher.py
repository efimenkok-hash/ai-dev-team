"""
core/patcher.py

Diff/preview/apply for code changes. apply_change is atomic: writes to a
temporary sibling file, then os.replace-s into place. A half-written file is
impossible — either the old content is intact, or the new content is fully
present.

CONTRACTS:
1. apply_change validates path/content via core.contracts BEFORE writing.
2. If the contract check fails, the target file is not touched.
3. Write goes to a tmp sibling in the same directory and is renamed atomically.
4. If anything goes wrong mid-write, the tmp file is removed.
5. read_text/write_text use UTF-8.
"""

import difflib
import os
import tempfile
from pathlib import Path

from core.contracts import enforce_code_change


def read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_text(path: str, content: str) -> None:
    Path(path).write_text(content, encoding="utf-8")


def generate_diff(path: str, new_content: str) -> str:
    old_content = read_text(path).splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)

    diff = difflib.unified_diff(
        old_content,
        new_lines,
        fromfile=path,
        tofile=path,
        lineterm="",
    )

    return "\n".join(diff)


def preview_change(path: str, new_content: str) -> str:
    diff = generate_diff(path, new_content)

    if not diff.strip():
        return "NO_CHANGES"

    return diff


def apply_change(path: str, new_content: str) -> None:
    enforce_code_change(path, new_content)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".tmp_patch_",
        suffix=target.suffix or ".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_content)
        os.replace(tmp_path, str(target))
    except BaseException:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise
