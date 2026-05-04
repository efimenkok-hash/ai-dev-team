"""
core/vision_client.py

Step 14a: thin client for OpenRouter Vision (image → text description).
Used by the Telegram bridge when the user sends a screenshot of an error
stack trace, a bug, or a UI mockup. The model "sees" the image and returns
a textual description that we then route to the orchestrator as if the
user had typed it.

The same OPENROUTER_API_KEY that drives our agents handles vision — no
separate provider, no extra credentials.

DESIGN DECISIONS:
- Same multipart-free `requests` posting style as whisper_client; image
  goes inline as data URL (base64). This works around upstream issues
  with public-URL image fetching that we hit during probe (see Step C
  diagnosis).
- Default model is OPENROUTER_VISION_DEFAULT_MODEL ("openai/gpt-4o-mini") —
  cheap, reliable, well-supported. Caller can override.
- Single failure type VisionError so callers can write one except clause.

CONTRACTS:
1. VisionClient(api_key) — api_key non-empty.
2. timeout > 0; base_url non-empty.
3. describe(image_bytes, *, mime_type, prompt, model, max_tokens) ->
   VisionResult.
4. image_bytes: non-empty bytes; mime_type in SUPPORTED_MIME; prompt
   non-empty string; max_tokens > 0.
5. Network/HTTP errors raise VisionError(code, detail).
6. Empty / non-string text in response -> VisionError("empty_text").
7. Response usage extracted into prompt_tokens / completion_tokens when
   provider includes them; otherwise zeros.
"""

import base64
from dataclasses import dataclass

import requests

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "openai/gpt-4o-mini"
DEFAULT_TIMEOUT = 60
DEFAULT_MAX_TOKENS = 600
DEFAULT_PROMPT = (
    "Опиши, что на этом изображении. Если это скриншот ошибки или стек-трейс — "
    "выпиши текст ошибки и ключевые строки точно."
)

SUPPORTED_MIME = frozenset({
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/gif",
})


class VisionError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}:{detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class VisionResult:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    provider: str = "openrouter"


class VisionClient:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        timeout: int = DEFAULT_TIMEOUT,
        base_url: str = DEFAULT_BASE_URL,
        referer: str = "https://github.com/efimenkok-hash/ai-dev-team",
        title: str = "AI Dev Team",
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
        if not isinstance(referer, str) or not referer.strip():
            raise ValueError("empty_referer")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("empty_title")
        self._api_key = api_key.strip()
        self._model = model.strip()
        self._timeout = timeout
        self._base_url = base_url.rstrip("/")
        self._referer = referer.strip()
        self._title = title.strip()
        self._session = session if session is not None else requests.Session()

    @property
    def model(self) -> str:
        return self._model

    def describe(
        self,
        image_bytes: bytes,
        *,
        mime_type: str = "image/png",
        prompt: str = DEFAULT_PROMPT,
        model: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> VisionResult:
        if not isinstance(image_bytes, (bytes, bytearray)):
            raise ValueError("image_bytes_must_be_bytes")
        if not image_bytes:
            raise ValueError("empty_image_bytes")
        if not isinstance(mime_type, str) or mime_type not in SUPPORTED_MIME:
            raise ValueError(f"unsupported_mime_type:{mime_type}")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("empty_prompt")
        if (
            not isinstance(max_tokens, int)
            or isinstance(max_tokens, bool)
            or max_tokens <= 0
        ):
            raise ValueError(f"invalid_max_tokens:{max_tokens}")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ValueError("empty_model")

        active_model = model.strip() if model else self._model
        b64 = base64.b64encode(bytes(image_bytes)).decode("ascii")
        data_url = f"data:{mime_type};base64,{b64}"

        url = f"{self._base_url}/chat/completions"
        body = {
            "model": active_model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt.strip()},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self._referer,
            "X-Title": self._title,
        }
        try:
            response = self._session.post(
                url,
                headers=headers,
                json=body,
                timeout=self._timeout,
            )
        except requests.Timeout as exc:
            raise VisionError("timeout", str(exc)[:200]) from exc
        except requests.ConnectionError as exc:
            raise VisionError("connection", str(exc)[:200]) from exc
        except requests.RequestException as exc:
            raise VisionError("request", str(exc)[:200]) from exc

        if response.status_code == 401:
            raise VisionError("unauthorized", _excerpt(response.text))
        if response.status_code == 402:
            raise VisionError("payment_required", _excerpt(response.text))
        if response.status_code == 429:
            raise VisionError("rate_limited", _excerpt(response.text))
        if response.status_code >= 500:
            raise VisionError(
                f"server_error:{response.status_code}",
                _excerpt(response.text),
            )
        if response.status_code >= 400:
            raise VisionError(
                f"client_error:{response.status_code}",
                _excerpt(response.text),
            )

        try:
            payload = response.json()
        except (ValueError, requests.JSONDecodeError) as exc:
            raise VisionError("invalid_json", str(exc)[:200]) from exc
        if not isinstance(payload, dict):
            raise VisionError("invalid_payload_shape", type(payload).__name__)

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise VisionError("missing_choices", "")
        first = choices[0]
        if not isinstance(first, dict):
            raise VisionError("invalid_choice_shape", "")
        message = first.get("message")
        if not isinstance(message, dict):
            raise VisionError("missing_message", "")
        text_raw = message.get("content")
        if not isinstance(text_raw, str):
            raise VisionError("missing_content", "")
        text_stripped = text_raw.strip()
        if not text_stripped:
            raise VisionError("empty_text", "")

        usage = payload.get("usage") or {}
        prompt_tokens = _safe_int(usage.get("prompt_tokens"))
        completion_tokens = _safe_int(usage.get("completion_tokens"))

        returned_model = payload.get("model")
        if not isinstance(returned_model, str) or not returned_model.strip():
            returned_model = active_model

        return VisionResult(
            text=text_stripped,
            model=returned_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )


def _safe_int(value) -> int:
    try:
        result = int(value)
        return max(0, result)
    except (TypeError, ValueError):
        return 0


def _excerpt(text: str | None, limit: int = 300) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"
