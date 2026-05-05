"""Tests for core.llm_dispatcher (Step 14b-1: model fallback chain)."""

import pytest
import requests

from core.llm_dispatcher import (
    DEFAULT_MAX_TOKENS,
    LLMAttempt,
    LLMDispatcher,
    LLMDispatchError,
    LLMRequest,
    LLMResponse,
    attempt_summary,
    build_simple_request,
)
from core.model_tier import REQUIRED_ROLES, TierConfig

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _full_chain():
    return {role: ("model-a", "model-b", "model-c") for role in REQUIRED_ROLES}


def _make_tier(chain_for_planning=None):
    chains = {role: ("default-m",) for role in REQUIRED_ROLES}
    if chain_for_planning is not None:
        chains["planning_agent"] = chain_for_planning
    return TierConfig(
        name="test",
        description="test tier",
        estimated_cost_usd=1.0,
        models_per_role=chains,
    )


def _ok_payload(text="hello", model="model-a", in_t=10, out_t=5):
    return {
        "id": "x",
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": in_t, "completion_tokens": out_t},
    }


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
    """Returns canned responses by call index, in order. After the list is
    exhausted, raises if more calls come in (catches over-polling tests).
    """

    def __init__(self, responses=None, raise_excs=None):
        self.responses = list(responses or [])
        self.raise_excs = list(raise_excs or [])
        self.calls: list[dict] = []

    def post(self, url, *, headers=None, json=None, timeout=None):
        self.calls.append(
            {"url": url, "headers": headers, "body": json, "timeout": timeout}
        )
        idx = len(self.calls) - 1
        if idx < len(self.raise_excs) and self.raise_excs[idx] is not None:
            raise self.raise_excs[idx]
        if idx < len(self.responses) and self.responses[idx] is not None:
            return self.responses[idx]
        raise RuntimeError(f"unexpected call #{idx}")


# ---------------------------------------------------------------------------
# LLMRequest validation
# ---------------------------------------------------------------------------


def test_request_happy_path():
    r = LLMRequest(
        agent_role="planning_agent",
        messages=({"role": "user", "content": "hi"},),
    )
    assert r.agent_role == "planning_agent"
    assert r.max_tokens == DEFAULT_MAX_TOKENS


def test_request_is_frozen():
    r = LLMRequest(
        agent_role="planning_agent",
        messages=({"role": "user", "content": "hi"},),
    )
    with pytest.raises(Exception):
        r.agent_role = "x"  # type: ignore[misc]


def test_request_rejects_empty_role():
    with pytest.raises(ValueError, match="empty_agent_role"):
        LLMRequest(agent_role="", messages=({"role": "user", "content": "x"},))


def test_request_rejects_non_tuple_messages():
    with pytest.raises(ValueError, match="messages_must_be_tuple"):
        LLMRequest(
            agent_role="r",
            messages=[{"role": "user", "content": "x"}],  # type: ignore[arg-type]
        )


def test_request_rejects_empty_messages():
    with pytest.raises(ValueError, match="empty_messages"):
        LLMRequest(agent_role="r", messages=())


def test_request_rejects_non_dict_message():
    with pytest.raises(ValueError, match="non_dict_message_at"):
        LLMRequest(agent_role="r", messages=("x",))  # type: ignore[arg-type]


@pytest.mark.parametrize("missing", ["role", "content"])
def test_request_rejects_missing_keys(missing):
    msg = {"role": "user", "content": "x"}
    msg.pop(missing)
    with pytest.raises(ValueError, match="missing_keys_at"):
        LLMRequest(agent_role="r", messages=(msg,))


@pytest.mark.parametrize("bad_field", ["role", "content"])
@pytest.mark.parametrize("bad_value", ["", "  "])
def test_request_rejects_empty_role_or_content(bad_field, bad_value):
    msg = {"role": "user", "content": "x"}
    msg[bad_field] = bad_value
    with pytest.raises(ValueError):
        LLMRequest(agent_role="r", messages=(msg,))


@pytest.mark.parametrize("bad", [0, -1, True])
def test_request_rejects_invalid_max_tokens(bad):
    with pytest.raises(ValueError, match="invalid_max_tokens"):
        LLMRequest(
            agent_role="r",
            messages=({"role": "user", "content": "x"},),
            max_tokens=bad,
        )


@pytest.mark.parametrize("bad", [-0.1, 2.5, 100])
def test_request_rejects_out_of_range_temperature(bad):
    with pytest.raises(ValueError, match="temperature_out_of_range"):
        LLMRequest(
            agent_role="r",
            messages=({"role": "user", "content": "x"},),
            temperature=bad,
        )


def test_request_rejects_bool_temperature():
    with pytest.raises(ValueError, match="invalid_temperature_type"):
        LLMRequest(
            agent_role="r",
            messages=({"role": "user", "content": "x"},),
            temperature=True,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# LLMDispatcher construction
# ---------------------------------------------------------------------------


def test_dispatcher_construction_happy_path():
    d = LLMDispatcher(api_key="sk-or-test")
    assert d._api_key == "sk-or-test"


@pytest.mark.parametrize("bad", ["", "  "])
def test_dispatcher_rejects_empty_api_key(bad):
    with pytest.raises(ValueError, match="empty_api_key"):
        LLMDispatcher(api_key=bad)


@pytest.mark.parametrize("bad", [0, -1, True])
def test_dispatcher_rejects_invalid_timeout(bad):
    with pytest.raises(ValueError, match="invalid_timeout"):
        LLMDispatcher(api_key="sk", timeout=bad)


def test_dispatcher_rejects_empty_base_url():
    with pytest.raises(ValueError, match="empty_base_url"):
        LLMDispatcher(api_key="sk", base_url="")


def test_dispatcher_rejects_empty_referer():
    with pytest.raises(ValueError, match="empty_referer"):
        LLMDispatcher(api_key="sk", referer="")


def test_dispatcher_rejects_empty_title():
    with pytest.raises(ValueError, match="empty_title"):
        LLMDispatcher(api_key="sk", title="")


# ---------------------------------------------------------------------------
# Successful dispatch — first model wins
# ---------------------------------------------------------------------------


def test_dispatch_first_model_succeeds():
    session = FakeSession(responses=[FakeResponse(json_payload=_ok_payload(text="ok"))])
    d = LLMDispatcher(api_key="sk", session=session)
    tier = _make_tier(chain_for_planning=("model-a", "model-b"))
    req = LLMRequest(
        agent_role="planning_agent",
        messages=({"role": "user", "content": "hi"},),
    )
    response = d.dispatch(req, tier)
    assert isinstance(response, LLMResponse)
    assert response.text == "ok"
    assert response.model_used == "model-a"
    assert response.fallback_used is False
    assert response.total_attempts == 1


def test_dispatch_uses_correct_url_and_headers():
    session = FakeSession(responses=[FakeResponse(json_payload=_ok_payload())])
    d = LLMDispatcher(api_key="sk-xyz", session=session)
    tier = _make_tier(chain_for_planning=("model-a",))
    d.dispatch(
        LLMRequest(agent_role="planning_agent", messages=({"role": "user", "content": "x"},)),
        tier,
    )
    call = session.calls[0]
    assert call["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sk-xyz"
    assert call["headers"]["HTTP-Referer"]
    assert call["headers"]["X-Title"]


def test_dispatch_sends_correct_body():
    session = FakeSession(responses=[FakeResponse(json_payload=_ok_payload())])
    d = LLMDispatcher(api_key="sk", session=session)
    tier = _make_tier(chain_for_planning=("primary",))
    d.dispatch(
        LLMRequest(
            agent_role="planning_agent",
            messages=(
                {"role": "system", "content": "you are helpful"},
                {"role": "user", "content": "task"},
            ),
            max_tokens=512,
            temperature=0.7,
        ),
        tier,
    )
    body = session.calls[0]["body"]
    assert body["model"] == "primary"
    assert body["max_tokens"] == 512
    assert body["temperature"] == 0.7
    assert len(body["messages"]) == 2


def test_dispatch_strips_response_text():
    session = FakeSession(
        responses=[FakeResponse(json_payload=_ok_payload(text="  ответ  "))]
    )
    d = LLMDispatcher(api_key="sk", session=session)
    tier = _make_tier(chain_for_planning=("model-a",))
    response = d.dispatch(
        LLMRequest(agent_role="planning_agent", messages=({"role": "user", "content": "x"},)),
        tier,
    )
    assert response.text == "ответ"


def test_dispatch_records_token_counts():
    session = FakeSession(
        responses=[FakeResponse(json_payload=_ok_payload(in_t=100, out_t=42))]
    )
    d = LLMDispatcher(api_key="sk", session=session)
    tier = _make_tier(chain_for_planning=("model-a",))
    response = d.dispatch(
        LLMRequest(agent_role="planning_agent", messages=({"role": "user", "content": "x"},)),
        tier,
    )
    assert response.prompt_tokens == 100
    assert response.completion_tokens == 42


# ---------------------------------------------------------------------------
# Fallback chain — second/third model recovers
# ---------------------------------------------------------------------------


def test_fallback_on_timeout():
    """First model times out → second succeeds."""
    session = FakeSession(
        responses=[None, FakeResponse(json_payload=_ok_payload(text="recovered"))],
        raise_excs=[requests.Timeout("slow"), None],
    )
    d = LLMDispatcher(api_key="sk", session=session)
    tier = _make_tier(chain_for_planning=("model-a", "model-b"))
    response = d.dispatch(
        LLMRequest(agent_role="planning_agent", messages=({"role": "user", "content": "x"},)),
        tier,
    )
    assert response.text == "recovered"
    assert response.model_used == "model-b"
    assert response.fallback_used is True
    assert response.total_attempts == 2
    assert response.attempts[0].reason == "timeout"
    assert response.attempts[0].ok is False
    assert response.attempts[1].ok is True


def test_fallback_on_500_error():
    session = FakeSession(
        responses=[
            FakeResponse(status_code=503, text="upstream"),
            FakeResponse(json_payload=_ok_payload()),
        ]
    )
    d = LLMDispatcher(api_key="sk", session=session)
    tier = _make_tier(chain_for_planning=("model-a", "model-b"))
    response = d.dispatch(
        LLMRequest(agent_role="planning_agent", messages=({"role": "user", "content": "x"},)),
        tier,
    )
    assert response.model_used == "model-b"
    assert "server_503" in response.attempts[0].reason


def test_fallback_on_429():
    session = FakeSession(
        responses=[
            FakeResponse(status_code=429, text="rate limited"),
            FakeResponse(json_payload=_ok_payload()),
        ]
    )
    d = LLMDispatcher(api_key="sk", session=session)
    tier = _make_tier(chain_for_planning=("model-a", "model-b"))
    response = d.dispatch(
        LLMRequest(agent_role="planning_agent", messages=({"role": "user", "content": "x"},)),
        tier,
    )
    assert response.model_used == "model-b"
    assert response.attempts[0].reason == "rate_limited"


def test_fallback_on_empty_text():
    session = FakeSession(
        responses=[
            FakeResponse(json_payload=_ok_payload(text="   ")),
            FakeResponse(json_payload=_ok_payload(text="real answer")),
        ]
    )
    d = LLMDispatcher(api_key="sk", session=session)
    tier = _make_tier(chain_for_planning=("model-a", "model-b"))
    response = d.dispatch(
        LLMRequest(agent_role="planning_agent", messages=({"role": "user", "content": "x"},)),
        tier,
    )
    assert response.text == "real answer"
    assert response.attempts[0].reason == "empty_text"


def test_fallback_on_invalid_json():
    class BadJsonResponse(FakeResponse):
        def json(self):
            raise ValueError("not json")

    session = FakeSession(
        responses=[
            BadJsonResponse(status_code=200, text="<html>"),
            FakeResponse(json_payload=_ok_payload()),
        ]
    )
    d = LLMDispatcher(api_key="sk", session=session)
    tier = _make_tier(chain_for_planning=("model-a", "model-b"))
    response = d.dispatch(
        LLMRequest(agent_role="planning_agent", messages=({"role": "user", "content": "x"},)),
        tier,
    )
    assert response.model_used == "model-b"
    assert response.attempts[0].reason == "invalid_json"


def test_fallback_through_three_models():
    """First two fail, third succeeds."""
    session = FakeSession(
        responses=[
            FakeResponse(status_code=500, text="x"),
            None,  # timeout placeholder
            FakeResponse(json_payload=_ok_payload(text="third wins")),
        ],
        raise_excs=[None, requests.Timeout("slow"), None],
    )
    d = LLMDispatcher(api_key="sk", session=session)
    tier = _make_tier(chain_for_planning=("model-a", "model-b", "model-c"))
    response = d.dispatch(
        LLMRequest(agent_role="planning_agent", messages=({"role": "user", "content": "x"},)),
        tier,
    )
    assert response.text == "third wins"
    assert response.model_used == "model-c"
    assert response.total_attempts == 3


# ---------------------------------------------------------------------------
# Failure modes — all chains exhausted, fatal HTTP errors
# ---------------------------------------------------------------------------


def test_dispatch_chain_exhausted_raises():
    session = FakeSession(
        responses=[
            FakeResponse(status_code=500),
            FakeResponse(status_code=500),
        ]
    )
    d = LLMDispatcher(api_key="sk", session=session)
    tier = _make_tier(chain_for_planning=("model-a", "model-b"))
    with pytest.raises(LLMDispatchError) as exc_info:
        d.dispatch(
            LLMRequest(agent_role="planning_agent", messages=({"role": "user", "content": "x"},)),
            tier,
        )
    assert exc_info.value.code == "chain_exhausted"
    assert len(exc_info.value.attempts) == 2


@pytest.mark.parametrize("status", [401, 402, 403])
def test_dispatch_fatal_http_status_raises_immediately(status):
    """Auth/billing errors stop the chain immediately — no fallback tried."""
    session = FakeSession(
        responses=[FakeResponse(status_code=status, text="auth failed")]
    )
    d = LLMDispatcher(api_key="sk", session=session)
    tier = _make_tier(chain_for_planning=("model-a", "model-b", "model-c"))
    with pytest.raises(LLMDispatchError) as exc_info:
        d.dispatch(
            LLMRequest(agent_role="planning_agent", messages=({"role": "user", "content": "x"},)),
            tier,
        )
    assert f"http_{status}" in exc_info.value.code
    assert len(session.calls) == 1  # only one model tried


def test_dispatch_invalid_request_type_raises():
    d = LLMDispatcher(api_key="sk")
    tier = _make_tier()
    with pytest.raises(ValueError, match="invalid_request_type"):
        d.dispatch("not a request", tier)  # type: ignore[arg-type]


def test_dispatch_invalid_tier_type_raises():
    d = LLMDispatcher(api_key="sk")
    with pytest.raises(ValueError, match="invalid_tier_type"):
        d.dispatch(
            LLMRequest(agent_role="planning_agent", messages=({"role": "user", "content": "x"},)),
            "not a tier",  # type: ignore[arg-type]
        )


def test_dispatch_unexpected_exception_treated_as_retry():
    """Defensive: any unexpected exception type triggers next-model fallback."""

    class WeirdSession:
        def post(self, *a, **kw):
            raise KeyError("something exotic")

    d = LLMDispatcher(api_key="sk", session=WeirdSession())  # type: ignore[arg-type]
    tier = _make_tier(chain_for_planning=("model-a",))
    with pytest.raises(LLMDispatchError) as exc_info:
        d.dispatch(
            LLMRequest(agent_role="planning_agent", messages=({"role": "user", "content": "x"},)),
            tier,
        )
    assert exc_info.value.attempts[0].reason.startswith("unexpected:")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_build_simple_request_user_only():
    r = build_simple_request("planning_agent", "сделай X")
    assert len(r.messages) == 1
    assert r.messages[0]["role"] == "user"
    assert r.messages[0]["content"] == "сделай X"


def test_build_simple_request_with_system():
    r = build_simple_request("planning_agent", "task", system_prompt="you are helpful")
    assert len(r.messages) == 2
    assert r.messages[0]["role"] == "system"
    assert r.messages[1]["role"] == "user"


def test_build_simple_request_strips_prompts():
    r = build_simple_request("planning_agent", "  task  ", system_prompt="  sys  ")
    assert r.messages[0]["content"] == "sys"
    assert r.messages[1]["content"] == "task"


def test_build_simple_request_rejects_empty_user():
    with pytest.raises(ValueError, match="empty_user_prompt"):
        build_simple_request("planning_agent", "  ")


def test_build_simple_request_rejects_empty_system():
    with pytest.raises(ValueError, match="empty_system_prompt"):
        build_simple_request("planning_agent", "task", system_prompt="  ")


def test_attempt_summary_with_attempts():
    attempts = (
        LLMAttempt(model="m1", ok=False, reason="timeout", duration_ms=500),
        LLMAttempt(model="m2", ok=True, reason="ok", duration_ms=1200),
    )
    summary = attempt_summary(attempts)
    assert "m1" in summary
    assert "m2" in summary
    assert "timeout" in summary
    assert "✓" in summary
    assert "✗" in summary


def test_attempt_summary_empty():
    assert "(no attempts)" in attempt_summary([])


# ---------------------------------------------------------------------------
# Observability integration
# ---------------------------------------------------------------------------


class CapturingObservability:
    """Minimal stand-in for Observability.record_agent_call."""

    def __init__(self):
        self.calls: list[dict] = []

    def record_agent_call(self, **kwargs):
        self.calls.append(kwargs)


def test_dispatch_logs_attempts_to_observability():
    obs = CapturingObservability()
    session = FakeSession(
        responses=[
            FakeResponse(status_code=500),
            FakeResponse(json_payload=_ok_payload()),
        ]
    )
    d = LLMDispatcher(api_key="sk", session=session, observability=obs)  # type: ignore[arg-type]
    tier = _make_tier(chain_for_planning=("model-a", "model-b"))
    d.dispatch(
        LLMRequest(agent_role="planning_agent", messages=({"role": "user", "content": "x"},)),
        tier,
    )
    assert len(obs.calls) == 2
    assert obs.calls[0]["ok"] is False
    assert obs.calls[1]["ok"] is True


def test_observability_failure_does_not_break_dispatch():
    """If observability raises, dispatch must still succeed."""

    class BadObs:
        def record_agent_call(self, **kwargs):
            raise RuntimeError("obs broken")

    session = FakeSession(responses=[FakeResponse(json_payload=_ok_payload())])
    d = LLMDispatcher(api_key="sk", session=session, observability=BadObs())  # type: ignore[arg-type]
    tier = _make_tier(chain_for_planning=("model-a",))
    response = d.dispatch(
        LLMRequest(agent_role="planning_agent", messages=({"role": "user", "content": "x"},)),
        tier,
    )
    assert response.text == "hello"
