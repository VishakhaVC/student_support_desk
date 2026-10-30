"""GroqProvider tested against the REAL groq SDK exception types with a fake client (no network, no API key)."""
from types import SimpleNamespace

import groq
import httpx
import pytest

from app.agents.providers import GroqProvider, ProviderError

REQ = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")


def status_exc(cls, code, msg="err", headers=None):
    return cls(msg, response=httpx.Response(code, request=REQ, headers=headers or {}), body=None)


def fake_response(content="hi", tool_calls=None, usage=(10, 5, 15)):
    u = SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1], total_tokens=usage[2]) if usage else None
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=u, model="m")


class FakeClient:
    def __init__(self, behaviours):
        self.behaviours, self.calls = list(behaviours), 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.last_kwargs = None

    def _create(self, **kw):
        self.calls += 1
        self.last_kwargs = kw
        b = self.behaviours.pop(0) if len(self.behaviours) > 1 else self.behaviours[0]
        if isinstance(b, Exception):
            raise b
        return b


def provider(behaviours, retries=2):
    p = GroqProvider("test-key", "test-model", max_retries=retries, sleep=lambda s: None)
    p.client = FakeClient(behaviours)
    return p


def test_requires_api_key():
    with pytest.raises(ProviderError) as e:
        GroqProvider("", "m")
    assert e.value.kind == "not_configured"


def test_uses_tools_and_auto_tool_choice_and_reads_usage():
    tc = SimpleNamespace(id="c1", function=SimpleNamespace(name="get_my_fee_status", arguments="{}"))
    p = provider([fake_response("", [tc])])
    r = p.chat([{"role": "user", "content": "x"}], [{"type": "function"}])
    assert p.client.last_kwargs["tool_choice"] == "auto" and p.client.last_kwargs["model"] == "test-model"
    assert r.tool_calls[0].name == "get_my_fee_status" and r.usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


def test_missing_usage_is_none():
    assert provider([fake_response(usage=None)]).chat([], []).usage is None


def test_rate_limit_is_retried_then_succeeds():
    p = provider([status_exc(groq.RateLimitError, 429, headers={"retry-after": "1"}), fake_response("ok")])
    assert p.chat([], []).content == "ok" and p.client.calls == 2


def test_rate_limit_exhausted_raises_rate_limit_error():
    p = provider([status_exc(groq.RateLimitError, 429)], retries=1)
    with pytest.raises(ProviderError) as e:
        p.chat([], [])
    assert e.value.kind == "rate_limit" and p.client.calls == 2


def test_timeout_and_connection_errors_are_classified():
    with pytest.raises(ProviderError) as e:
        provider([groq.APITimeoutError(request=REQ)], retries=0).chat([], [])
    assert e.value.kind == "timeout"
    with pytest.raises(ProviderError) as e:
        provider([groq.APIConnectionError(request=REQ)], retries=0).chat([], [])
    assert e.value.kind == "connection"


def test_auth_and_model_not_found_are_not_retried():
    p = provider([status_exc(groq.AuthenticationError, 401)])
    with pytest.raises(ProviderError) as e:
        p.chat([], [])
    assert e.value.kind == "auth" and p.client.calls == 1
    p = provider([status_exc(groq.NotFoundError, 404)])
    with pytest.raises(ProviderError) as e:
        p.chat([], [])
    assert e.value.kind == "model_unavailable" and "GROQ_MODEL" in str(e.value)


def test_invalid_tool_call_is_detected():
    p = provider([status_exc(groq.BadRequestError, 400, msg="Error code: 400 - tool_use_failed: Failed to call a function")])
    with pytest.raises(ProviderError) as e:
        p.chat([], [])
    assert e.value.kind == "invalid_tool_call"


def test_server_errors_are_retried():
    p = provider([status_exc(groq.InternalServerError, 500), fake_response("back")])
    assert p.chat([], []).content == "back"


def test_api_key_never_appears_in_error_messages():
    p = provider([status_exc(groq.AuthenticationError, 401, msg="bad key test-key")])
    with pytest.raises(ProviderError) as e:
        p.chat([], [])
    assert "test-key" not in str(e.value)
