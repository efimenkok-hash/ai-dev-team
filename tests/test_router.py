"""Unit tests for core.router with mocked HTTP layer.

router.py is the single boundary between our agents and external LLM
endpoints (Ollama on localhost, OpenRouter for cloud). We mock requests.post
to verify URL/headers/body construction without making any network call.
"""

from typing import Any

import pytest

import core.router as router


class _MockResponse:
    def __init__(self, status: int, payload: dict[str, Any] | None = None,
                 raise_status: Exception | None = None):
        self._status = status
        self._payload = payload or {}
        self._raise_status = raise_status

    def raise_for_status(self) -> None:
        if self._raise_status is not None:
            raise self._raise_status

    def json(self) -> dict[str, Any]:
        return self._payload


def _capture_post(monkeypatch, response):
    captured: dict[str, Any] = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return response

    monkeypatch.setattr(router.requests, "post", fake_post)
    return captured


# ---------------------------------------------------------------------------
# ask_ollama
# ---------------------------------------------------------------------------


def test_ask_ollama_calls_local_endpoint(monkeypatch):
    captured = _capture_post(
        monkeypatch,
        _MockResponse(status=200, payload={"response": "hello world"}),
    )
    out = router.ask_ollama("hi there")
    assert out == "hello world"
    assert captured["url"] == "http://localhost:11434/api/generate"
    assert captured["json"]["model"] == "qwen"
    assert captured["json"]["prompt"] == "hi there"
    assert captured["json"]["stream"] is False
    assert captured["timeout"] == 120


def test_ask_ollama_propagates_http_errors(monkeypatch):
    err = RuntimeError("ollama down")
    _capture_post(monkeypatch, _MockResponse(status=500, raise_status=err))
    with pytest.raises(RuntimeError, match="ollama down"):
        router.ask_ollama("x")


def test_ask_ollama_extracts_response_field(monkeypatch):
    _capture_post(
        monkeypatch,
        _MockResponse(status=200, payload={"response": "answer"}),
    )
    assert router.ask_ollama("q") == "answer"


# ---------------------------------------------------------------------------
# ask_openrouter
# ---------------------------------------------------------------------------


def test_ask_openrouter_calls_openrouter_endpoint(monkeypatch):
    monkeypatch.setattr(router, "OPENROUTER_API_KEY", "test-key-XYZ")
    captured = _capture_post(
        monkeypatch,
        _MockResponse(
            status=200,
            payload={"choices": [{"message": {"content": "cloud answer"}}]},
        ),
    )
    out = router.ask_openrouter("hi cloud")
    assert out == "cloud answer"
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-key-XYZ"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["json"]["model"] == "qwen/qwen3-coder"
    assert captured["json"]["messages"] == [
        {"role": "user", "content": "hi cloud"}
    ]
    assert captured["timeout"] == 120


def test_ask_openrouter_propagates_http_errors(monkeypatch):
    monkeypatch.setattr(router, "OPENROUTER_API_KEY", "k")
    err = RuntimeError("openrouter 503")
    _capture_post(monkeypatch, _MockResponse(status=503, raise_status=err))
    with pytest.raises(RuntimeError, match="openrouter 503"):
        router.ask_openrouter("x")


# ---------------------------------------------------------------------------
# route()
# ---------------------------------------------------------------------------


def test_route_short_prompt_uses_ollama(monkeypatch):
    """Prompts shorter than 300 chars route to Ollama."""
    captured: dict[str, str] = {}

    def fake_ollama(p: str) -> str:
        captured["used"] = "ollama"
        captured["prompt"] = p
        return "from ollama"

    def fake_openrouter(p: str) -> str:
        captured["used"] = "openrouter"
        return "from openrouter"

    monkeypatch.setattr(router, "ask_ollama", fake_ollama)
    monkeypatch.setattr(router, "ask_openrouter", fake_openrouter)
    out = router.route("short prompt")
    assert out == "from ollama"
    assert captured["used"] == "ollama"


def test_route_long_prompt_uses_openrouter(monkeypatch):
    """Prompts >= 300 chars route to OpenRouter."""
    captured: dict[str, str] = {}

    def fake_ollama(p: str) -> str:
        captured["used"] = "ollama"
        return "x"

    def fake_openrouter(p: str) -> str:
        captured["used"] = "openrouter"
        return "from openrouter"

    monkeypatch.setattr(router, "ask_ollama", fake_ollama)
    monkeypatch.setattr(router, "ask_openrouter", fake_openrouter)
    out = router.route("L" * 400)
    assert out == "from openrouter"
    assert captured["used"] == "openrouter"


def test_route_boundary_at_300_chars_uses_openrouter(monkeypatch):
    """Exactly 300 chars triggers openrouter (len < 300 is the ollama gate)."""
    captured: dict[str, str] = {}
    monkeypatch.setattr(router, "ask_ollama", lambda p: captured.setdefault("used", "ollama") or "o")
    monkeypatch.setattr(
        router, "ask_openrouter",
        lambda p: captured.setdefault("used", "openrouter") or "or",
    )
    router.route("X" * 300)
    assert captured["used"] == "openrouter"
