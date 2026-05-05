"""
core/writer_to_worktree.py

Step 14b-9: parse a writer-agent JSON artifact and materialise the files
inside a git worktree directory.

Expected writer artifact format (produced by _WRITER_SYSTEM v2.0 prompt):

    {
      "files": [
        {"path": "src/foo.py", "content": "def foo(): return 42\n"},
        {"path": "tests/test_foo.py", "content": "from src.foo import foo\n..."}
      ]
    }

CONTRACTS:
1. writer_artifact must be a valid JSON string with a non-empty "files" list.
2. Every file entry must have string "path" and string "content" keys.
3. "path" must be relative (no leading /), must not contain ".." segments,
   and the resolved destination must remain inside worktree_path. Any
   violation raises ValueError("path_escape").
4. "content" must be a UTF-8 string (str in Python; bytes are rejected).
5. On success returns a tuple of absolute Paths for each written file.
6. No partial writes are visible on failure: all writes happen after full
   validation. (Files are written in order after all paths pass checks.)
7. Parent directories are created automatically (parents=True, exist_ok=True).
"""

from __future__ import annotations

import json
from pathlib import Path


def write_artifact_to_worktree(
    writer_artifact: str,
    worktree_path: Path,
) -> tuple[Path, ...]:
    """Parse writer JSON → write each file into worktree → return written paths.

    Args:
        writer_artifact: JSON string produced by the writer agent.
        worktree_path:   Absolute path to the git worktree directory.

    Returns:
        Tuple of absolute Path objects for every file written to disk.

    Raises:
        ValueError: on invalid JSON, missing/empty 'files', bad path, or
                    non-string content.
    """
    if not isinstance(writer_artifact, str):
        raise ValueError(
            f"writer_artifact_must_be_str:{type(writer_artifact).__name__}"
        )
    if not isinstance(worktree_path, Path):
        raise ValueError(
            f"worktree_path_must_be_path:{type(worktree_path).__name__}"
        )

    # ── 1. Parse JSON ────────────────────────────────────────────────────────
    try:
        data = json.loads(writer_artifact)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid_json:{exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"artifact_must_be_object:got_{type(data).__name__}")

    files = data.get("files")
    if not isinstance(files, list):
        raise ValueError("missing_files_key_or_not_list")
    if not files:
        raise ValueError("empty_files_list")

    # ── 2. Validate all entries before touching the filesystem ───────────────
    resolved_root = worktree_path.resolve()
    validated: list[tuple[Path, str]] = []

    for idx, entry in enumerate(files):
        if not isinstance(entry, dict):
            raise ValueError(f"file_entry_{idx}_not_dict")

        raw_path = entry.get("path")
        content = entry.get("content")

        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError(f"file_entry_{idx}_missing_path")
        if not isinstance(content, str):
            raise ValueError(
                f"file_entry_{idx}_content_must_be_str:"
                f"got_{type(content).__name__}"
            )

        # Reject absolute paths and ".." segments (path traversal defence).
        p = Path(raw_path)
        if p.is_absolute():
            raise ValueError(f"path_escape:absolute_path:{raw_path!r}")
        if ".." in p.parts:
            raise ValueError(f"path_escape:dotdot_in_path:{raw_path!r}")

        dest = (resolved_root / p).resolve()
        # Ensure the resolved destination is still inside the worktree.
        try:
            dest.relative_to(resolved_root)
        except ValueError:
            raise ValueError(f"path_escape:{raw_path!r}")  # noqa: B904

        validated.append((dest, content))

    # ── 3. Write to disk ─────────────────────────────────────────────────────
    written: list[Path] = []
    for dest, content in validated:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        written.append(dest)

    return tuple(written)
