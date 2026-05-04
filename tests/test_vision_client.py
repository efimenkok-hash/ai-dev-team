"""Tests for core.vision_client (Step 14a: image description via OpenRouter)."""

import base64
import json

import pytest
import requests

from core.vision_client import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    SUPPORTED_MIME,
    VisionClient,
    VisionError,
    VisionResult,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, json_payload=None, text=""):
        self.status_code = status_code
        self._json = json_payload
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _ok_payload(text="Описание", model="openai/gpt-4o-mini", in_t=10, out_t=5):
    return {
        "id": "x",
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": in_t, "completion_tokens": out_t},
    }


class FakeSession:
    def __init__(self, response=None, raise_exc: Exception | None = None):
        self.response = response or FakeResponse(json_payload=_ok_payload())
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def post(self, url, *, headers=None, json=None, timeout=None):
        self.calls.append(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------


def test_construction_happy_path():
    c = VisionClient(api_key="sk-or-test")
    assert c.model == DEFAULT_MODEL


def test_construction_strips_api_key():
    c = VisionClient(api_key="  sk-or-test  ")
    assert c._api_key == "sk-or-test"


@pytest.mark.parametrize("bad", ["", "  ", "\n"])
def test_construction_rejects_empty_api_key(bad):
    with pytest.raises(ValueError, match="empty_api_key"):
        VisionClient(api_key=bad)


def test_construction_rejects_non_string_api_key():
    with pytest.raises(ValueError, match="empty_api_key"):
        VisionClient(api_key=None)  # type: ignore[arg-type]


def test_construction_rejects_empty_model():
    with pytest.raises(ValueError, match="empty_model"):
        VisionClient(api_key="sk", model="")


@pytest.mark.parametrize("bad", [0, -1, -100])
def test_construction_rejects_non_positive_timeout(bad):
    with pytest.raises(ValueError, match="invalid_timeout"):
        VisionClient(api_key="sk", timeout=bad)


def test_construction_rejects_bool_timeout():
    with pytest.raises(ValueError, match="invalid_timeout"):
        VisionClient(api_key="sk", timeout=True)  # type: ignore[arg-type]


def test_construction_rejects_empty_base_url():
    with pytest.raises(ValueError, match="empty_base_url"):
        VisionClient(api_key="sk", base_url="")


def test_construction_rejects_empty_referer():
    with pytest.raises(ValueError, match="empty_referer"):
        VisionClient(api_key="sk", referer="")


def test_construction_rejects_empty_title():
    with pytest.raises(ValueError, match="empty_title"):
        VisionClient(api_key="sk", title="")


# ---------------------------------------------------------------------------
# describe input validation
# ---------------------------------------------------------------------------


def test_describe_rejects_non_bytes_image():
    c = VisionClient(api_key="sk", session=FakeSession())
    with pytest.raises(ValueError, match="image_bytes_must_be_bytes"):
        c.describe("not bytes")  # type: ignore[arg-type]


def test_describe_rejects_empty_image():
    c = VisionClient(api_key="sk", session=FakeSession())
    with pytest.raises(ValueError, match="empty_image_bytes"):
        c.describe(b"")


def test_describe_accepts_bytearray():
    session = FakeSession()
    c = VisionClient(api_key="sk", session=session)
    result = c.describe(bytearray(b"\x89PNG"))
    assert result.text == "Описание"


def test_describe_rejects_unsupported_mime():
    c = VisionClient(api_key="sk", session=FakeSession())
    with pytest.raises(ValueError, match="unsupported_mime_type"):
        c.describe(b"\x00", mime_type="image/bmp")


@pytest.mark.parametrize("good_mime", sorted(SUPPORTED_MIME))
def test_describe_accepts_all_supported_mimes(good_mime):
    session = FakeSession()
    c = VisionClient(api_key="sk", session=session)
    c.describe(b"\x00", mime_type=good_mime)
    body = session.calls[0]["json"]
    image_url = body["messages"][0]["content"][1]["image_url"]["url"]
    assert image_url.startswith(f"data:{good_mime};base64,")


@pytest.mark.parametrize("bad", ["", "  "])
def test_describe_rejects_empty_prompt(bad):
    c = VisionClient(api_key="sk", session=FakeSession())
    with pytest.raises(ValueError, match="empty_prompt"):
        c.describe(b"\x00", prompt=bad)


@pytest.mark.parametrize("bad", [0, -1, -100])
def test_describe_rejects_non_positive_max_tokens(bad):
    c = VisionClient(api_key="sk", session=FakeSession())
    with pytest.raises(ValueError, match="invalid_max_tokens"):
        c.describe(b"\x00", max_tokens=bad)


def test_describe_rejects_bool_max_tokens():
    c = VisionClient(api_key="sk", session=FakeSession())
    with pytest.raises(ValueError, match="invalid_max_tokens"):
        c.describe(b"\x00", max_tokens=True)  # type: ignore[arg-type]


def test_describe_rejects_empty_model_override():
    c = VisionClient(api_key="sk", session=FakeSession())
    with pytest.raises(ValueError, match="empty_model"):
        c.describe(b"\x00", model="  ")


# ---------------------------------------------------------------------------
# describe HTTP wiring
# ---------------------------------------------------------------------------


def test_describe_uses_correct_url_and_headers():
    session = FakeSession()
    c = VisionClient(api_key="sk-or-xyz", session=session)
    c.describe(b"abc")
    call = session.calls[0]
    assert call["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sk-or-xyz"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["headers"]["HTTP-Referer"]
    assert call["headers"]["X-Title"]


def test_describe_encodes_image_as_base64_data_url():
    session = FakeSession()
    c = VisionClient(api_key="sk", session=session)
    c.describe(b"hello bytes", mime_type="image/png")
    body = session.calls[0]["json"]
    image_url = body["messages"][0]["content"][1]["image_url"]["url"]
    expected_b64 = base64.b64encode(b"hello bytes").decode()
    assert image_url == f"data:image/png;base64,{expected_b64}"


def test_describe_uses_default_prompt_when_omitted():
    session = FakeSession()
    c = VisionClient(api_key="sk", session=session)
    c.describe(b"\x00")
    body = session.calls[0]["json"]
    prompt_text = body["messages"][0]["content"][0]["text"]
    assert "Опиши" in prompt_text  # part of DEFAULT_PROMPT


def test_describe_uses_custom_prompt():
    session = FakeSession()
    c = VisionClient(api_key="sk", session=session)
    c.describe(b"\x00", prompt="custom question?")
    body = session.calls[0]["json"]
    prompt_text = body["messages"][0]["content"][0]["text"]
    assert prompt_text == "custom question?"


def test_describe_strips_prompt():
    session = FakeSession()
    c = VisionClient(api_key="sk", session=session)
    c.describe(b"\x00", prompt="   trimmed   ")
    body = session.calls[0]["json"]
    assert body["messages"][0]["content"][0]["text"] == "trimmed"


def test_describe_uses_client_default_model():
    session = FakeSession()
    c = VisionClient(api_key="sk", session=session, model="openai/gpt-4o")
    c.describe(b"\x00")
    body = session.calls[0]["json"]
    assert body["model"] == "openai/gpt-4o"


def test_describe_overrides_model_per_call():
    session = FakeSession()
    c = VisionClient(api_key="sk", session=session, model="openai/gpt-4o-mini")
    c.describe(b"\x00", model="anthropic/claude-haiku-4-5")
    body = session.calls[0]["json"]
    assert body["model"] == "anthropic/claude-haiku-4-5"


def test_describe_sends_max_tokens():
    session = FakeSession()
    c = VisionClient(api_key="sk", session=session)
    c.describe(b"\x00", max_tokens=42)
    body = session.calls[0]["json"]
    assert body["max_tokens"] == 42


def test_describe_default_max_tokens():
    session = FakeSession()
    c = VisionClient(api_key="sk", session=session)
    c.describe(b"\x00")
    body = session.calls[0]["json"]
    assert body["max_tokens"] == DEFAULT_MAX_TOKENS


def test_describe_passes_timeout():
    session = FakeSession()
    c = VisionClient(api_key="sk", session=session, timeout=33)
    c.describe(b"\x00")
    assert session.calls[0]["timeout"] == 33


def test_describe_strips_trailing_slash_from_base_url():
    session = FakeSession()
    c = VisionClient(api_key="sk", session=session, base_url="https://x.test/v1/")
    c.describe(b"\x00")
    assert session.calls[0]["url"] == "https://x.test/v1/chat/completions"


# ---------------------------------------------------------------------------
# describe response handling
# ---------------------------------------------------------------------------


def test_describe_returns_text_and_usage():
    session = FakeSession(
        response=FakeResponse(json_payload=_ok_payload(text="Это шахматка прозрачности.", in_t=8517, out_t=12))
    )
    c = VisionClient(api_key="sk", session=session)
    result = c.describe(b"\x00")
    assert isinstance(result, VisionResult)
    assert result.text == "Это шахматка прозрачности."
    assert result.prompt_tokens == 8517
    assert result.completion_tokens == 12
    assert result.provider == "openrouter"


def test_describe_strips_response_text():
    session = FakeSession(
        response=FakeResponse(json_payload=_ok_payload(text="   spaced   "))
    )
    c = VisionClient(api_key="sk", session=session)
    result = c.describe(b"\x00")
    assert result.text == "spaced"


def test_describe_falls_back_to_active_model_when_response_missing_model():
    payload = _ok_payload()
    payload.pop("model", None)
    session = FakeSession(response=FakeResponse(json_payload=payload))
    c = VisionClient(api_key="sk", session=session, model="my/custom-model")
    result = c.describe(b"\x00")
    assert result.model == "my/custom-model"


def test_describe_handles_missing_usage():
    payload = _ok_payload()
    payload.pop("usage", None)
    session = FakeSession(response=FakeResponse(json_payload=payload))
    c = VisionClient(api_key="sk", session=session)
    result = c.describe(b"\x00")
    assert result.prompt_tokens == 0
    assert result.completion_tokens == 0


def test_describe_clamps_negative_token_counts():
    payload = _ok_payload(in_t=-1, out_t=-50)
    session = FakeSession(response=FakeResponse(json_payload=payload))
    c = VisionClient(api_key="sk", session=session)
    result = c.describe(b"\x00")
    assert result.prompt_tokens == 0
    assert result.completion_tokens == 0


# ---------------------------------------------------------------------------
# describe error handling
# ---------------------------------------------------------------------------


def test_describe_raises_on_timeout():
    session = FakeSession(raise_exc=requests.Timeout("slow"))
    c = VisionClient(api_key="sk", session=session)
    with pytest.raises(VisionError) as exc_info:
        c.describe(b"\x00")
    assert exc_info.value.code == "timeout"


def test_describe_raises_on_connection_error():
    session = FakeSession(raise_exc=requests.ConnectionError("dns"))
    c = VisionClient(api_key="sk", session=session)
    with pytest.raises(VisionError) as exc_info:
        c.describe(b"\x00")
    assert exc_info.value.code == "connection"


def test_describe_raises_on_generic_request_exception():
    session = FakeSession(raise_exc=requests.RequestException("?"))
    c = VisionClient(api_key="sk", session=session)
    with pytest.raises(VisionError) as exc_info:
        c.describe(b"\x00")
    assert exc_info.value.code == "request"


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (401, "unauthorized"),
        (402, "payment_required"),
        (429, "rate_limited"),
        (500, "server_error:500"),
        (502, "server_error:502"),
        (400, "client_error:400"),
        (404, "client_error:404"),
    ],
)
def test_describe_raises_on_http_status(status, expected_code):
    session = FakeSession(response=FakeResponse(status_code=status, text="error body"))
    c = VisionClient(api_key="sk", session=session)
    with pytest.raises(VisionError) as exc_info:
        c.describe(b"\x00")
    assert exc_info.value.code == expected_code


def test_describe_raises_on_invalid_json():
    class BadJsonResponse(FakeResponse):
        def json(self):
            raise ValueError("not json")

    session = FakeSession(response=BadJsonResponse(status_code=200, text="<html>"))
    c = VisionClient(api_key="sk", session=session)
    with pytest.raises(VisionError) as exc_info:
        c.describe(b"\x00")
    assert exc_info.value.code == "invalid_json"


def test_describe_raises_on_non_dict_payload():
    session = FakeSession(response=FakeResponse(json_payload=["arr"]))
    c = VisionClient(api_key="sk", session=session)
    with pytest.raises(VisionError) as exc_info:
        c.describe(b"\x00")
    assert exc_info.value.code == "invalid_payload_shape"


def test_describe_raises_on_missing_choices():
    session = FakeSession(response=FakeResponse(json_payload={"id": "x"}))
    c = VisionClient(api_key="sk", session=session)
    with pytest.raises(VisionError) as exc_info:
        c.describe(b"\x00")
    assert exc_info.value.code == "missing_choices"


def test_describe_raises_on_empty_choices():
    session = FakeSession(response=FakeResponse(json_payload={"choices": []}))
    c = VisionClient(api_key="sk", session=session)
    with pytest.raises(VisionError) as exc_info:
        c.describe(b"\x00")
    assert exc_info.value.code == "missing_choices"


def test_describe_raises_on_invalid_choice_shape():
    session = FakeSession(response=FakeResponse(json_payload={"choices": ["str"]}))
    c = VisionClient(api_key="sk", session=session)
    with pytest.raises(VisionError) as exc_info:
        c.describe(b"\x00")
    assert exc_info.value.code == "invalid_choice_shape"


def test_describe_raises_on_missing_message():
    session = FakeSession(response=FakeResponse(json_payload={"choices": [{}]}))
    c = VisionClient(api_key="sk", session=session)
    with pytest.raises(VisionError) as exc_info:
        c.describe(b"\x00")
    assert exc_info.value.code == "missing_message"


def test_describe_raises_on_missing_content():
    session = FakeSession(
        response=FakeResponse(json_payload={"choices": [{"message": {"role": "assistant"}}]})
    )
    c = VisionClient(api_key="sk", session=session)
    with pytest.raises(VisionError) as exc_info:
        c.describe(b"\x00")
    assert exc_info.value.code == "missing_content"


def test_describe_raises_on_empty_text():
    payload = _ok_payload(text="   ")
    session = FakeSession(response=FakeResponse(json_payload=payload))
    c = VisionClient(api_key="sk", session=session)
    with pytest.raises(VisionError) as exc_info:
        c.describe(b"\x00")
    assert exc_info.value.code == "empty_text"


def test_vision_error_str_format():
    e = VisionError("rate_limited", "wait 60s")
    assert "rate_limited" in str(e)
    assert "wait 60s" in str(e)


def test_vision_error_without_detail():
    e = VisionError("timeout")
    assert str(e) == "timeout"


# ---------------------------------------------------------------------------
# truncation safety
# ---------------------------------------------------------------------------


def test_describe_truncates_long_error_text_in_detail():
    huge = "X" * 10_000
    session = FakeSession(response=FakeResponse(status_code=400, text=huge))
    c = VisionClient(api_key="sk", session=session)
    with pytest.raises(VisionError) as exc_info:
        c.describe(b"\x00")
    assert len(exc_info.value.detail) < 500


def test_request_body_is_json_serializable():
    """Sanity: the payload we hand to requests must be JSON-serializable."""
    session = FakeSession()
    c = VisionClient(api_key="sk", session=session)
    c.describe(b"\x00\x01\x02")
    body = session.calls[0]["json"]
    # If this raises, our body shape is broken
    json.dumps(body)
