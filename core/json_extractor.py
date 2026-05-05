"""
core/json_extractor.py

Step A2: Robust JSON extraction from LLM output.

LLMs frequently wrap valid JSON in markdown fences, add prose preambles like
"Here is the plan:", or append postamble text. json.loads() fails on all of
these. This module provides a single function that tolerates all common LLM
output formats and returns a dict or None.

CONTRACTS:
1. extract_json_object is a pure function (no I/O, no side effects).
2. Returns dict on success, None on any failure — never raises.
3. Tries strategies in order of strictness; returns on first success.
4. Does NOT attempt to fix malformed JSON (e.g. trailing commas).
"""

from __future__ import annotations

import json
import re


def extract_json_object(text: str) -> dict | None:
    """Robust JSON extraction from LLM output.

    Handles:
      - Bare JSON:              {"k": "v"}
      - Markdown fences:        ```json\\n{...}\\n``` or ```\\n{...}\\n```
      - Prose preamble:         "Here is the plan: {...}"
      - Prose postamble:        "{...} Hope this helps!"
      - Mixed preamble+fence:   "Sure!\\n```json\\n{...}\\n```\\nDone."
      - Trailing whitespace / BOM
      - Nested braces in values

    Strategy (in order):
      1. Direct: json.loads(text.strip())
      2. Strip markdown code fences (```json...``` or ```...```), retry.
      3. Brace scan: find first '{' and last matching '}', try that substring.

    Returns:
      dict on success, None on failure.
    """
    if not isinstance(text, str):
        return None

    text = text.strip()
    if not text:
        return None

    # Strategy 1: direct parse
    result = _try_loads(text)
    if result is not None:
        return result

    # Strategy 2: strip markdown fences
    stripped = _strip_fences(text)
    if stripped != text:
        result = _try_loads(stripped)
        if result is not None:
            return result

    # Strategy 3: brace scan — find first '{' ... last matching '}'
    extracted = _extract_by_brace_scan(text)
    if extracted is not None:
        return extracted

    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _try_loads(text: str) -> dict | None:
    """Attempt json.loads; return dict or None."""
    try:
        result = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(result, dict):
        return result
    return None


_FENCE_RE = re.compile(
    r"```(?:json)?\s*\n(.*?)\n\s*```",
    re.DOTALL | re.IGNORECASE,
)


def _strip_fences(text: str) -> str:
    """Remove the outermost ```json ... ``` or ``` ... ``` fences.

    Returns the inner content stripped of leading/trailing whitespace.
    If no fence found, returns original text unchanged.
    """
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    return text


def _extract_by_brace_scan(text: str) -> dict | None:
    """Find the first '{' and the matching '}' by counting depth.

    Ignores braces inside JSON strings (handles escaped quotes).
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False

    for i in range(start, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                return _try_loads(candidate)

    return None
