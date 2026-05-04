"""Tests for core.whisper_client (Step 14a: audio transcription)."""

import pytest
import requests

from core.whisper_client import (
    DEFAULT_MODEL,
    WHISPER_PRICE_PER_MINUTE_USD,
    TranscriptionResult,
    WhisperClient,
    WhisperError,
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


class FakeSession:
    def __init__(self, response=None, raise_exc: Exception | None = None):
        self.response = response or FakeResponse(
            json_payload={"text": "Привет", "duration": 6.0, "language": "russian"}
        )
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def post(self, url, *, headers=None, files=None, data=None, timeout=None):
        self.calls.append(
            {"url": url, "headers": headers, "files": files, "data": data, "timeout": timeout}
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------


def test_construction_happy_path():
    c = WhisperClient(api_key="sk-test")
    assert c.model == DEFAULT_MODEL


def test_construction_strips_api_key():
    c = WhisperClient(api_key="  sk-test  ")
    # internal attribute check is acceptable for contract verification
    assert c._api_key == "sk-test"


@pytest.mark.parametrize("bad", ["", "  ", "\n"])
def test_construction_rejects_empty_api_key(bad):
    with pytest.raises(ValueError, match="empty_api_key"):
        WhisperClient(api_key=bad)


def test_construction_rejects_non_string_api_key():
    with pytest.raises(ValueError, match="empty_api_key"):
        WhisperClient(api_key=None)  # type: ignore[arg-type]


def test_construction_rejects_empty_model():
    with pytest.raises(ValueError, match="empty_model"):
        WhisperClient(api_key="sk-test", model="")


@pytest.mark.parametrize("bad", [0, -1, -100])
def test_construction_rejects_non_positive_timeout(bad):
    with pytest.raises(ValueError, match="invalid_timeout"):
        WhisperClient(api_key="sk-test", timeout=bad)


def test_construction_rejects_bool_timeout():
    with pytest.raises(ValueError, match="invalid_timeout"):
        WhisperClient(api_key="sk-test", timeout=True)  # type: ignore[arg-type]


def test_construction_rejects_empty_base_url():
    with pytest.raises(ValueError, match="empty_base_url"):
        WhisperClient(api_key="sk-test", base_url="")


# ---------------------------------------------------------------------------
# transcribe input validation
# ---------------------------------------------------------------------------


def test_transcribe_rejects_non_bytes_audio():
    c = WhisperClient(api_key="sk-test", session=FakeSession())
    with pytest.raises(ValueError, match="audio_bytes_must_be_bytes"):
        c.transcribe("not bytes")  # type: ignore[arg-type]


def test_transcribe_rejects_empty_audio():
    c = WhisperClient(api_key="sk-test", session=FakeSession())
    with pytest.raises(ValueError, match="empty_audio_bytes"):
        c.transcribe(b"")


def test_transcribe_accepts_bytearray():
    session = FakeSession()
    c = WhisperClient(api_key="sk-test", session=session)
    result = c.transcribe(bytearray(b"\x00\x01\x02"))
    assert result.text == "Привет"


@pytest.mark.parametrize("bad", ["", "  "])
def test_transcribe_rejects_empty_filename(bad):
    c = WhisperClient(api_key="sk-test", session=FakeSession())
    with pytest.raises(ValueError, match="empty_filename"):
        c.transcribe(b"\x00", filename=bad)


def test_transcribe_rejects_empty_mime_type():
    c = WhisperClient(api_key="sk-test", session=FakeSession())
    with pytest.raises(ValueError, match="empty_mime_type"):
        c.transcribe(b"\x00", mime_type="")


def test_transcribe_rejects_empty_language():
    c = WhisperClient(api_key="sk-test", session=FakeSession())
    with pytest.raises(ValueError, match="empty_language"):
        c.transcribe(b"\x00", language="  ")


def test_transcribe_rejects_unsupported_language():
    c = WhisperClient(api_key="sk-test", session=FakeSession())
    with pytest.raises(ValueError, match="unsupported_language"):
        c.transcribe(b"\x00", language="klingon")


# ---------------------------------------------------------------------------
# transcribe HTTP wiring (request shape)
# ---------------------------------------------------------------------------


def test_transcribe_uses_default_url_and_auth_header():
    session = FakeSession()
    c = WhisperClient(api_key="sk-test-xyz", session=session)
    c.transcribe(b"audio data here")
    call = session.calls[0]
    assert call["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert call["headers"] == {"Authorization": "Bearer sk-test-xyz"}


def test_transcribe_sends_multipart_file():
    session = FakeSession()
    c = WhisperClient(api_key="sk-test", session=session)
    c.transcribe(b"audio data", filename="x.ogg", mime_type="audio/ogg")
    files = session.calls[0]["files"]
    assert "file" in files
    fname, content, mtype = files["file"]
    assert fname == "x.ogg"
    assert content == b"audio data"
    assert mtype == "audio/ogg"


def test_transcribe_sends_model_and_response_format():
    session = FakeSession()
    c = WhisperClient(api_key="sk-test", session=session)
    c.transcribe(b"\x00")
    data = session.calls[0]["data"]
    assert data["model"] == "whisper-1"
    assert data["response_format"] == "verbose_json"


def test_transcribe_sends_language_when_provided():
    session = FakeSession()
    c = WhisperClient(api_key="sk-test", session=session)
    c.transcribe(b"\x00", language="ru")
    data = session.calls[0]["data"]
    assert data["language"] == "ru"


def test_transcribe_lowercases_language():
    session = FakeSession()
    c = WhisperClient(api_key="sk-test", session=session)
    c.transcribe(b"\x00", language="RU")
    data = session.calls[0]["data"]
    assert data["language"] == "ru"


def test_transcribe_omits_language_when_none():
    session = FakeSession()
    c = WhisperClient(api_key="sk-test", session=session)
    c.transcribe(b"\x00")
    data = session.calls[0]["data"]
    assert "language" not in data


def test_transcribe_passes_timeout():
    session = FakeSession()
    c = WhisperClient(api_key="sk-test", session=session, timeout=42)
    c.transcribe(b"\x00")
    assert session.calls[0]["timeout"] == 42


def test_transcribe_uses_custom_base_url():
    session = FakeSession()
    c = WhisperClient(
        api_key="sk-test",
        session=session,
        base_url="https://my.proxy/v1",
    )
    c.transcribe(b"\x00")
    assert session.calls[0]["url"] == "https://my.proxy/v1/audio/transcriptions"


def test_transcribe_strips_trailing_slash_from_base_url():
    session = FakeSession()
    c = WhisperClient(
        api_key="sk-test",
        session=session,
        base_url="https://my.proxy/v1/",
    )
    c.transcribe(b"\x00")
    assert session.calls[0]["url"] == "https://my.proxy/v1/audio/transcriptions"


# ---------------------------------------------------------------------------
# transcribe response handling
# ---------------------------------------------------------------------------


def test_transcribe_returns_text_and_duration():
    session = FakeSession(
        response=FakeResponse(
            json_payload={"text": "  hello world  ", "duration": 60.0, "language": "english"}
        )
    )
    c = WhisperClient(api_key="sk-test", session=session)
    result = c.transcribe(b"\x00")
    assert isinstance(result, TranscriptionResult)
    assert result.text == "hello world"
    assert result.duration_seconds == 60.0
    assert result.cost_usd == pytest.approx(WHISPER_PRICE_PER_MINUTE_USD)
    assert result.cost_estimated is False
    assert result.language == "english"
    assert result.provider == "openai"


def test_transcribe_handles_missing_duration():
    session = FakeSession(
        response=FakeResponse(json_payload={"text": "hi"})
    )
    c = WhisperClient(api_key="sk-test", session=session)
    result = c.transcribe(b"\x00")
    assert result.duration_seconds is None
    assert result.cost_usd == 0.0
    assert result.cost_estimated is True


def test_transcribe_uses_explicit_language_when_response_omits_it():
    session = FakeSession(
        response=FakeResponse(json_payload={"text": "hi", "duration": 1.0})
    )
    c = WhisperClient(api_key="sk-test", session=session)
    result = c.transcribe(b"\x00", language="ru")
    assert result.language == "ru"


def test_transcribe_computes_cost_from_partial_minute():
    session = FakeSession(
        response=FakeResponse(
            json_payload={"text": "hi", "duration": 30.0, "language": "ru"}
        )
    )
    c = WhisperClient(api_key="sk-test", session=session)
    result = c.transcribe(b"\x00")
    expected = 30.0 / 60.0 * WHISPER_PRICE_PER_MINUTE_USD
    assert result.cost_usd == pytest.approx(expected, abs=1e-6)


# ---------------------------------------------------------------------------
# transcribe error handling
# ---------------------------------------------------------------------------


def test_transcribe_raises_on_timeout():
    session = FakeSession(raise_exc=requests.Timeout("timed out"))
    c = WhisperClient(api_key="sk-test", session=session)
    with pytest.raises(WhisperError) as exc_info:
        c.transcribe(b"\x00")
    assert exc_info.value.code == "timeout"


def test_transcribe_raises_on_connection_error():
    session = FakeSession(raise_exc=requests.ConnectionError("dns fail"))
    c = WhisperClient(api_key="sk-test", session=session)
    with pytest.raises(WhisperError) as exc_info:
        c.transcribe(b"\x00")
    assert exc_info.value.code == "connection"


def test_transcribe_raises_on_generic_request_exception():
    session = FakeSession(raise_exc=requests.RequestException("other"))
    c = WhisperClient(api_key="sk-test", session=session)
    with pytest.raises(WhisperError) as exc_info:
        c.transcribe(b"\x00")
    assert exc_info.value.code == "request"


def test_transcribe_raises_on_401():
    session = FakeSession(response=FakeResponse(status_code=401, text="invalid_api_key"))
    c = WhisperClient(api_key="sk-test", session=session)
    with pytest.raises(WhisperError) as exc_info:
        c.transcribe(b"\x00")
    assert exc_info.value.code == "unauthorized"


def test_transcribe_raises_on_429():
    session = FakeSession(response=FakeResponse(status_code=429, text="rate limited"))
    c = WhisperClient(api_key="sk-test", session=session)
    with pytest.raises(WhisperError) as exc_info:
        c.transcribe(b"\x00")
    assert exc_info.value.code == "rate_limited"


def test_transcribe_raises_on_500():
    session = FakeSession(response=FakeResponse(status_code=503, text="upstream"))
    c = WhisperClient(api_key="sk-test", session=session)
    with pytest.raises(WhisperError) as exc_info:
        c.transcribe(b"\x00")
    assert exc_info.value.code == "server_error:503"


def test_transcribe_raises_on_400():
    session = FakeSession(response=FakeResponse(status_code=400, text="bad audio"))
    c = WhisperClient(api_key="sk-test", session=session)
    with pytest.raises(WhisperError) as exc_info:
        c.transcribe(b"\x00")
    assert exc_info.value.code == "client_error:400"


def test_transcribe_raises_on_invalid_json():
    class BadJsonResponse(FakeResponse):
        def json(self):
            raise ValueError("not json")

    session = FakeSession(response=BadJsonResponse(status_code=200, text="<html>"))
    c = WhisperClient(api_key="sk-test", session=session)
    with pytest.raises(WhisperError) as exc_info:
        c.transcribe(b"\x00")
    assert exc_info.value.code == "invalid_json"


def test_transcribe_raises_on_non_dict_payload():
    session = FakeSession(response=FakeResponse(json_payload=["hello"]))
    c = WhisperClient(api_key="sk-test", session=session)
    with pytest.raises(WhisperError) as exc_info:
        c.transcribe(b"\x00")
    assert exc_info.value.code == "invalid_payload_shape"


def test_transcribe_raises_on_missing_text():
    session = FakeSession(response=FakeResponse(json_payload={"duration": 1.0}))
    c = WhisperClient(api_key="sk-test", session=session)
    with pytest.raises(WhisperError) as exc_info:
        c.transcribe(b"\x00")
    assert exc_info.value.code == "missing_text_field"


def test_transcribe_raises_on_empty_text():
    session = FakeSession(response=FakeResponse(json_payload={"text": "   ", "duration": 1.0}))
    c = WhisperClient(api_key="sk-test", session=session)
    with pytest.raises(WhisperError) as exc_info:
        c.transcribe(b"\x00")
    assert exc_info.value.code == "empty_text"


def test_whisper_error_str_format():
    e = WhisperError("timeout", "took too long")
    assert "timeout" in str(e)
    assert "took too long" in str(e)
    assert e.code == "timeout"
    assert e.detail == "took too long"


def test_whisper_error_without_detail():
    e = WhisperError("rate_limited")
    assert str(e) == "rate_limited"


# ---------------------------------------------------------------------------
# truncation safety
# ---------------------------------------------------------------------------


def test_transcribe_truncates_long_error_text_in_detail():
    huge = "X" * 10_000
    session = FakeSession(response=FakeResponse(status_code=400, text=huge))
    c = WhisperClient(api_key="sk-test", session=session)
    with pytest.raises(WhisperError) as exc_info:
        c.transcribe(b"\x00")
    # detail must be capped — we don't want 10KB of error spam in logs
    assert len(exc_info.value.detail) < 500
