"""
tests/test_json_extractor.py

Unit tests for core/json_extractor.py — extract_json_object.

Covers:
  - Strategy 1 (direct): bare JSON, BOM, whitespace
  - Strategy 2 (fence strip): ```json...```, ```...```, mixed preamble+fence
  - Strategy 3 (brace scan): prose preamble, prose postamble, preamble+postamble
  - Edge cases: nested braces, escaped quotes, unicode
  - Failure cases: non-dict JSON, invalid JSON, non-string input, empty input
"""

from core.json_extractor import extract_json_object

# ---------------------------------------------------------------------------
# Strategy 1 — direct parse
# ---------------------------------------------------------------------------


def test_bare_json_returns_dict():
    result = extract_json_object('{"key": "value"}')
    assert result == {"key": "value"}


def test_bare_json_with_leading_trailing_whitespace():
    result = extract_json_object('  \n  {"k": 1}  \n  ')
    assert result == {"k": 1}


def test_bare_json_with_bom():
    result = extract_json_object('﻿{"k": "v"}')
    # BOM at start: direct parse will fail; brace scan picks it up
    assert result == {"k": "v"}


def test_bare_json_nested_object():
    data = '{"outer": {"inner": [1, 2, 3]}, "flag": true}'
    result = extract_json_object(data)
    assert result == {"outer": {"inner": [1, 2, 3]}, "flag": True}


def test_bare_json_with_escaped_quotes_in_values():
    data = '{"msg": "say \\"hello\\" world"}'
    result = extract_json_object(data)
    assert result == {"msg": 'say "hello" world'}


# ---------------------------------------------------------------------------
# Strategy 2 — strip markdown fences
# ---------------------------------------------------------------------------


def test_json_fence_with_json_label():
    text = '```json\n{"plan": "done"}\n```'
    result = extract_json_object(text)
    assert result == {"plan": "done"}


def test_json_fence_without_label():
    text = '```\n{"a": 1}\n```'
    result = extract_json_object(text)
    assert result == {"a": 1}


def test_json_fence_uppercase_json_label():
    text = '```JSON\n{"x": 99}\n```'
    result = extract_json_object(text)
    assert result == {"x": 99}


def test_json_fence_with_whitespace_around_braces():
    text = '```json\n  {"q": "r"}  \n```'
    result = extract_json_object(text)
    assert result == {"q": "r"}


def test_json_fence_embedded_in_prose():
    text = "Sure! Here is your plan:\n```json\n{\"verdict\": \"PASS\"}\n```\nHope this helps!"
    result = extract_json_object(text)
    assert result == {"verdict": "PASS"}


# ---------------------------------------------------------------------------
# Strategy 3 — brace scan
# ---------------------------------------------------------------------------


def test_prose_preamble_before_json():
    text = 'Here is the result: {"status": "ok", "code": 0}'
    result = extract_json_object(text)
    assert result == {"status": "ok", "code": 0}


def test_prose_postamble_after_json():
    text = '{"done": true} That concludes the plan.'
    result = extract_json_object(text)
    assert result == {"done": True}


def test_prose_preamble_and_postamble():
    text = 'As requested: {"id": "abc-123", "ready": false} Let me know if you need more.'
    result = extract_json_object(text)
    assert result == {"id": "abc-123", "ready": False}


def test_brace_scan_with_nested_braces_in_values():
    text = 'Output:\n{"outer": {"a": {"b": 1}}}\nEnd.'
    result = extract_json_object(text)
    assert result == {"outer": {"a": {"b": 1}}}


def test_brace_scan_with_escaped_quotes_in_string_values():
    text = 'Planning done: {"msg": "She said \\"go\\" and left"}'
    result = extract_json_object(text)
    assert result == {"msg": 'She said "go" and left'}


def test_brace_scan_with_brace_chars_inside_string_values():
    text = 'Result: {"template": "use {0} and {1} here", "count": 2}'
    result = extract_json_object(text)
    assert result == {"template": "use {0} and {1} here", "count": 2}


def test_brace_scan_unicode_content():
    text = 'Ответ агента: {"задача": "написать тест", "статус": "готово"}'
    result = extract_json_object(text)
    assert result == {"задача": "написать тест", "статус": "готово"}


# ---------------------------------------------------------------------------
# Failure cases — must return None, never raise
# ---------------------------------------------------------------------------


def test_returns_none_for_json_array():
    result = extract_json_object("[1, 2, 3]")
    assert result is None


def test_returns_none_for_json_string():
    result = extract_json_object('"just a string"')
    assert result is None


def test_returns_none_for_json_number():
    result = extract_json_object("42")
    assert result is None


def test_returns_none_for_json_null():
    result = extract_json_object("null")
    assert result is None


def test_returns_none_for_truncated_json():
    result = extract_json_object('{"key": "val')
    assert result is None


def test_returns_none_for_empty_string():
    result = extract_json_object("")
    assert result is None


def test_returns_none_for_whitespace_only():
    result = extract_json_object("   \n\t  ")
    assert result is None


def test_returns_none_for_none_input():
    result = extract_json_object(None)
    assert result is None


def test_returns_none_for_integer_input():
    result = extract_json_object(42)  # type: ignore[arg-type]
    assert result is None


def test_returns_none_for_prose_with_no_json():
    result = extract_json_object("Here is my plan. It involves several steps.")
    assert result is None


def test_returns_none_for_trailing_comma_malformed_json():
    # Contract: does NOT attempt to fix malformed JSON
    result = extract_json_object('{"a": 1,}')
    assert result is None


# ---------------------------------------------------------------------------
# Idempotency / no-side-effects
# ---------------------------------------------------------------------------


def test_same_input_returns_equal_results_on_repeated_calls():
    text = '```json\n{"x": 1}\n```'
    r1 = extract_json_object(text)
    r2 = extract_json_object(text)
    assert r1 == r2


def test_does_not_mutate_input_string():
    original = '```json\n{"a": 1}\n```'
    copy = original
    extract_json_object(original)
    assert original == copy
