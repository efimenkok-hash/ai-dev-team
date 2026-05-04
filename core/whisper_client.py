"""
core/whisper_client.py

Step 14a: thin client for OpenAI Whisper audio-transcription API. Wraps a
single endpoint (POST /audio/transcriptions) so the Telegram bridge can
turn a voice message (.ogg from Telegram, .wav, .mp3, etc.) into Russian
or English text, then feed that text to the orchestrator as if the user
typed it.

DESIGN DECISIONS:
- We do NOT pull in the official `openai` SDK. The dep surface there is
  large, version-coupled, and we already use raw `requests` elsewhere
  (core/router.py). Doing the same keeps the project consistent.
- HTTP retries are NOT done here. The caller (telegram_bridge) decides
  whether a transcription failure is worth retrying or whether to apologise
  to the user and ask them to type instead.
- Response is normalised to TranscriptionResult (frozen). Raw provider
  fields (segments/words) are dropped — we only need text + duration.

CONTRACTS:
1. WhisperClient(api_key) — api_key must be non-empty string.
2. timeout > 0 seconds.
3. base_url defaults to OpenAI; can be overridden for testing or for
   OpenAI-compatible providers (e.g. self-hosted whisper.cpp servers).
4. transcribe(audio_bytes, *, filename, mime_type, language) -> TranscriptionResult
   - audio_bytes: non-empty bytes
   - filename: non-empty (used by multipart and by OpenAI to detect format)
   - mime_type: non-empty
   - language: optional ISO-639-1 code; if None, Whisper auto-detects
5. transcribe() raises:
   - ValueError on bad inputs
   - WhisperError on HTTP/network/parse failures (single exception type
     so callers can write a single except)
6. Cost is computed from response duration via $0.006/min; if duration
   missing, cost_usd is 0.0 with cost_estimated=False.
7. raw_text is stripped; empty transcription -> WhisperError("empty_text").
"""

from dataclasses import dataclass

import requests

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "whisper-1"
DEFAULT_TIMEOUT = 60
WHISPER_PRICE_PER_MINUTE_USD = 0.006

_VALID_LANGS = frozenset({
    "ru", "en", "uk", "be", "kk", "de", "fr", "es", "it", "pl", "tr",
    "pt", "nl", "sv", "no", "da", "fi", "cs", "sk", "ro", "hu", "el",
    "he", "ar", "ja", "ko", "zh", "vi", "th", "id",
})


class WhisperError(RuntimeError):
    """Single failure mode for callers — wraps HTTP/network/parse errors."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}:{detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    duration_seconds: float | None
    cost_usd: float
    cost_estimated: bool
    language: str | None
    provider: str = "openai"
    model: str = DEFAULT_MODEL


class WhisperClient:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        timeout: int = DEFAULT_TIMEOUT,
        base_url: str = DEFAULT_BASE_URL,
        session: requests.Session | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("empty_api_key")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("empty_model")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError(f"invalid_timeout:{timeout}")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("empty_base_url")
        self._api_key = api_key.strip()
        self._model = model.strip()
        self._timeout = timeout
        self._base_url = base_url.rstrip("/")
        self._session = session if session is not None else requests.Session()

    @property
    def model(self) -> str:
        return self._model

    def transcribe(
        self,
        audio_bytes: bytes,
        *,
        filename: str = "audio.ogg",
        mime_type: str = "audio/ogg",
        language: str | None = None,
    ) -> TranscriptionResult:
        if not isinstance(audio_bytes, (bytes, bytearray)):
            raise ValueError("audio_bytes_must_be_bytes")
        if not audio_bytes:
            raise ValueError("empty_audio_bytes")
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError("empty_filename")
        if not isinstance(mime_type, str) or not mime_type.strip():
            raise ValueError("empty_mime_type")
        if language is not None:
            if not isinstance(language, str) or not language.strip():
                raise ValueError("empty_language")
            if language.strip().lower() not in _VALID_LANGS:
                raise ValueError(f"unsupported_language:{language}")

        url = f"{self._base_url}/audio/transcriptions"
        files = {
            "file": (filename, bytes(audio_bytes), mime_type),
        }
        data: dict[str, str] = {
            "model": self._model,
            "response_format": "verbose_json",
        }
        if language is not None:
            data["language"] = language.strip().lower()

        try:
            response = self._session.post(
                url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                files=files,
                data=data,
                timeout=self._timeout,
            )
        except requests.Timeout as exc:
            raise WhisperError("timeout", str(exc)[:200]) from exc
        except requests.ConnectionError as exc:
            raise WhisperError("connection", str(exc)[:200]) from exc
        except requests.RequestException as exc:
            raise WhisperError("request", str(exc)[:200]) from exc

        if response.status_code == 401:
            raise WhisperError("unauthorized", _safe_excerpt(response.text))
        if response.status_code == 429:
            raise WhisperError("rate_limited", _safe_excerpt(response.text))
        if response.status_code >= 500:
            raise WhisperError(
                f"server_error:{response.status_code}",
                _safe_excerpt(response.text),
            )
        if response.status_code >= 400:
            raise WhisperError(
                f"client_error:{response.status_code}",
                _safe_excerpt(response.text),
            )

        try:
            payload = response.json()
        except (ValueError, requests.JSONDecodeError) as exc:
            raise WhisperError("invalid_json", str(exc)[:200]) from exc

        if not isinstance(payload, dict):
            raise WhisperError("invalid_payload_shape", type(payload).__name__)

        text = payload.get("text")
        if not isinstance(text, str):
            raise WhisperError("missing_text_field", "")
        text_stripped = text.strip()
        if not text_stripped:
            raise WhisperError("empty_text", "")

        duration_raw = payload.get("duration")
        duration: float | None
        cost_estimated = False
        if isinstance(duration_raw, (int, float)) and duration_raw > 0:
            duration = float(duration_raw)
            cost_usd = round(duration / 60.0 * WHISPER_PRICE_PER_MINUTE_USD, 6)
        else:
            duration = None
            cost_usd = 0.0
            cost_estimated = True

        detected_language = payload.get("language")
        if not isinstance(detected_language, str) or not detected_language:
            detected_language = language

        return TranscriptionResult(
            text=text_stripped,
            duration_seconds=duration,
            cost_usd=cost_usd,
            cost_estimated=cost_estimated,
            language=detected_language,
            model=self._model,
        )


def _safe_excerpt(text: str | None, limit: int = 300) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"
