"""
core/llm_dispatcher.py

Step 14b-1: HTTP layer that sends prompts to OpenRouter using the model
chain defined by the active TierConfig. Walks the chain top-down: tries
the primary model first, falls back to the next on timeout / network /
HTTP / empty-response / invalid-JSON errors. Returns the first successful
response with rich metadata (which model won, how many attempts, tokens,
estimated cost).

Every attempt is logged to Observability if available, so self-improvement
later can see which models tend to fail and propose chain reordering.

CONTRACTS:
1. LLMRequest is frozen; agent_role must be a known role; messages must
   be a non-empty tuple of {"role": str, "content": str} dicts.
2. dispatch(req, tier) returns LLMResponse on success, raises
   LLMDispatchError if the entire chain fails.
3. Per-attempt failures are NEVER raised — they are logged and trigger
   the next model.
4. Empty / whitespace-only response text counts as failure (next model).
5. HTTP 401/402/403 are NOT retried (auth issues won't fix on next model)
   — they immediately raise LLMDispatchError.
6. HTTP 429 (rate limit) on a model triggers fallback (next model
   probably has different limits).
7. Output text is stripped before being returned.
"""

import contextlib
import time
from collections.abc import Sequence
from dataclasses import dataclass

import requests

from core.model_tier import TierConfig
from core.observability import Observability

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_TOKENS = 2048

# These status codes mean a different model won't help — auth / billing.
_FATAL_HTTP_STATUS = frozenset({401, 402, 403})


class LLMDispatchError(RuntimeError):
    """Raised when EVERY model in the chain has failed.

    The `attempts` field records each tried model + the reason it was
    rejected, so the bot can show the user a useful failure report.
    """

    def __init__(self, code: str, detail: str, attempts: tuple) -> None:
        super().__init__(f"{code}:{detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.attempts = attempts


@dataclass(frozen=True)
class LLMRequest:
    agent_role: str
    messages: tuple[dict, ...]
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = 0.2

    def __post_init__(self) -> None:
        if not isinstance(self.agent_role, str) or not self.agent_role.strip():
            raise ValueError("empty_agent_role")
        if not isinstance(self.messages, tuple):
            raise ValueError("messages_must_be_tuple")
        if not self.messages:
            raise ValueError("empty_messages")
        for i, msg in enumerate(self.messages):
            if not isinstance(msg, dict):
                raise ValueError(f"non_dict_message_at:{i}")
            if "role" not in msg or "content" not in msg:
                raise ValueError(f"missing_keys_at:{i}")
            if not isinstance(msg["role"], str) or not msg["role"].strip():
                raise ValueError(f"empty_role_at:{i}")
            if not isinstance(msg["content"], str) or not msg["content"].strip():
                raise ValueError(f"empty_content_at:{i}")
        if (
            isinstance(self.max_tokens, bool)
            or not isinstance(self.max_tokens, int)
            or self.max_tokens <= 0
        ):
            raise ValueError(f"invalid_max_tokens:{self.max_tokens!r}")
        if (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
        ):
            raise ValueError("invalid_temperature_type")
        if self.temperature < 0.0 or self.temperature > 2.0:
            raise ValueError(f"temperature_out_of_range:{self.temperature}")


@dataclass(frozen=True)
class LLMAttempt:
    """Audit record of one model attempt."""

    model: str
    ok: bool
    reason: str  # "ok" or short failure code
    duration_ms: int


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model_used: str
    prompt_tokens: int
    completion_tokens: int
    attempts: tuple[LLMAttempt, ...]

    @property
    def total_attempts(self) -> int:
        return len(self.attempts)

    @property
    def fallback_used(self) -> bool:
        """True if more than one model was tried before success."""
        return self.total_attempts > 1


class LLMDispatcher:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        referer: str = "https://github.com/efimenkok-hash/ai-dev-team",
        title: str = "AI Dev Team",
        session: requests.Session | None = None,
        observability: Observability | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("empty_api_key")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("empty_base_url")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int)
            or timeout <= 0
        ):
            raise ValueError(f"invalid_timeout:{timeout!r}")
        if not isinstance(referer, str) or not referer.strip():
            raise ValueError("empty_referer")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("empty_title")
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._referer = referer.strip()
        self._title = title.strip()
        self._session = session if session is not None else requests.Session()
        self._obs = observability

    def dispatch(
        self,
        request: LLMRequest,
        tier: TierConfig,
    ) -> LLMResponse:
        """Walks tier.dispatch_chain_for(request.agent_role), returns first success.

        Raises LLMDispatchError if the entire chain fails.
        """
        if not isinstance(request, LLMRequest):
            raise ValueError(
                f"invalid_request_type:{type(request).__name__}"
            )
        if not isinstance(tier, TierConfig):
            raise ValueError(
                f"invalid_tier_type:{type(tier).__name__}"
            )

        chain = tier.dispatch_chain_for(request.agent_role)
        attempts: list[LLMAttempt] = []

        for model in chain:
            started = time.perf_counter()
            try:
                text, prompt_tokens, completion_tokens = self._try_model(
                    model, request,
                )
            except _FatalDispatchError as exc:
                # Auth / billing — propagate immediately, no fallback helps.
                elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
                attempt = LLMAttempt(
                    model=model,
                    ok=False,
                    reason=exc.code,
                    duration_ms=elapsed_ms,
                )
                attempts.append(attempt)
                self._log_attempt(request, attempt)
                raise LLMDispatchError(
                    exc.code, exc.detail, tuple(attempts),
                ) from exc
            except _RetryableDispatchError as exc:
                elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
                attempt = LLMAttempt(
                    model=model,
                    ok=False,
                    reason=exc.code,
                    duration_ms=elapsed_ms,
                )
                attempts.append(attempt)
                self._log_attempt(request, attempt)
                continue
            except Exception as exc:
                # Defensive: anything else also triggers fallback.
                elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
                attempt = LLMAttempt(
                    model=model,
                    ok=False,
                    reason=f"unexpected:{type(exc).__name__}",
                    duration_ms=elapsed_ms,
                )
                attempts.append(attempt)
                self._log_attempt(request, attempt)
                continue

            # Success.
            elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
            attempt = LLMAttempt(
                model=model,
                ok=True,
                reason="ok",
                duration_ms=elapsed_ms,
            )
            attempts.append(attempt)
            self._log_attempt(request, attempt)
            return LLMResponse(
                text=text,
                model_used=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                attempts=tuple(attempts),
            )

        # Entire chain exhausted.
        last_reason = attempts[-1].reason if attempts else "no_models"
        raise LLMDispatchError(
            "chain_exhausted",
            f"all {len(chain)} model(s) failed; last reason: {last_reason}",
            tuple(attempts),
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _try_model(
        self,
        model: str,
        request: LLMRequest,
    ) -> tuple[str, int, int]:
        url = f"{self._base_url}/chat/completions"
        body = {
            "model": model,
            "messages": list(request.messages),
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
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
            raise _RetryableDispatchError("timeout", _short(exc)) from exc
        except requests.ConnectionError as exc:
            raise _RetryableDispatchError("connection", _short(exc)) from exc
        except requests.RequestException as exc:
            raise _RetryableDispatchError("request", _short(exc)) from exc

        if response.status_code in _FATAL_HTTP_STATUS:
            raise _FatalDispatchError(
                f"http_{response.status_code}",
                _excerpt(response.text),
            )
        if response.status_code == 429:
            raise _RetryableDispatchError(
                "rate_limited", _excerpt(response.text),
            )
        if response.status_code >= 500:
            raise _RetryableDispatchError(
                f"server_{response.status_code}", _excerpt(response.text),
            )
        if response.status_code >= 400:
            # Other 4xx (model-not-found, bad request) — try fallback.
            raise _RetryableDispatchError(
                f"client_{response.status_code}", _excerpt(response.text),
            )

        try:
            payload = response.json()
        except (ValueError, requests.JSONDecodeError) as exc:
            raise _RetryableDispatchError("invalid_json", _short(exc)) from exc

        if not isinstance(payload, dict):
            raise _RetryableDispatchError(
                "invalid_payload_shape",
                type(payload).__name__,
            )

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise _RetryableDispatchError("missing_choices", "")

        first = choices[0]
        if not isinstance(first, dict):
            raise _RetryableDispatchError("invalid_choice_shape", "")

        message = first.get("message")
        if not isinstance(message, dict):
            raise _RetryableDispatchError("missing_message", "")

        content = message.get("content")
        if not isinstance(content, str):
            raise _RetryableDispatchError("missing_content", "")

        text = content.strip()
        if not text:
            raise _RetryableDispatchError("empty_text", "")

        usage = payload.get("usage") or {}
        prompt_tokens = _safe_int(usage.get("prompt_tokens"))
        completion_tokens = _safe_int(usage.get("completion_tokens"))

        return text, prompt_tokens, completion_tokens

    def _log_attempt(
        self,
        request: LLMRequest,
        attempt: LLMAttempt,
    ) -> None:
        if self._obs is None:
            return
        # Observability must never break dispatch — suppress any failure.
        with contextlib.suppress(Exception):
            self._obs.record_agent_call(
                agent_name=request.agent_role,
                task_id="dispatcher",
                duration_ms=attempt.duration_ms,
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
                ok=attempt.ok,
                error=None if attempt.ok else attempt.reason,
            )


# ---------------------------------------------------------------------------
# private exception types — split fatal vs retryable failures
# ---------------------------------------------------------------------------


class _RetryableDispatchError(Exception):
    """One model failed in a way that another model might fix."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}:{detail}" if detail else code)
        self.code = code
        self.detail = detail


class _FatalDispatchError(Exception):
    """Auth/billing failure — propagating to the next model wouldn't help."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}:{detail}" if detail else code)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _safe_int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _short(exc: BaseException, limit: int = 200) -> str:
    text = f"{type(exc).__name__}:{exc}"
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def _excerpt(text: str | None, limit: int = 300) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def build_simple_request(
    agent_role: str,
    user_prompt: str,
    *,
    system_prompt: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.2,
) -> LLMRequest:
    """Convenience builder for the common case of one user prompt."""
    if not isinstance(user_prompt, str) or not user_prompt.strip():
        raise ValueError("empty_user_prompt")
    msgs: list[dict] = []
    if system_prompt is not None:
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("empty_system_prompt")
        msgs.append({"role": "system", "content": system_prompt.strip()})
    msgs.append({"role": "user", "content": user_prompt.strip()})
    return LLMRequest(
        agent_role=agent_role,
        messages=tuple(msgs),
        max_tokens=max_tokens,
        temperature=temperature,
    )


def attempt_summary(attempts: Sequence[LLMAttempt]) -> str:
    """Compact one-line summary of attempts for logs / chat replies."""
    if not attempts:
        return "(no attempts)"
    parts: list[str] = []
    for a in attempts:
        marker = "✓" if a.ok else "✗"
        parts.append(f"{marker} {a.model} ({a.reason}, {a.duration_ms}ms)")
    return " → ".join(parts)
