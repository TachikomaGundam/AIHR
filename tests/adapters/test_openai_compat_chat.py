"""Offline unit tests for OpenAICompatAdapter.chat()/probe_capabilities().

Every HTTP interaction is replaced by a scripted stand-in for
``requests.post``; endpoint resolution runs against tmp config trees and a
``base_url_override``, so nothing ever leaves the machine. Retry backoff
sleeps are monkeypatched to no-ops.
"""

from __future__ import annotations

import json

import pytest

import requests

from hr.adapters.openai_compat import MAX_RETRIES, OpenAICompatAdapter
from hr.scheduler.taxonomy import FailureCode

MODELS_JSON = {
    "providers": {
        "acme": {
            "name": "Acme",
            "api": "https://cache.invalid",
            "models": {
                "m": {
                    "contextWindow": 200000,
                    "maxOutputTokens": 8192,
                    "reasoning_options": {"effort": "high"},
                }
            },
        }
    }
}
AUTH_JSON = {"acme": {"key": "sk-test-000"}}

SSE_USAGE = {"usage": {"prompt_tokens": 11, "completion_tokens": 5}}


class FakeStreamResponse:
    """Scripted streaming response stand-in with close accounting."""

    def __init__(self, status_code: int = 200, lines: list[str] | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._lines = lines if lines is not None else []
        self.text = text
        self.closed = 0

    def iter_lines(self, decode_unicode: bool = True) -> object:
        for line in self._lines:
            yield line

    def close(self) -> None:
        self.closed += 1


def _sse(*chunks: object) -> list[str]:
    return [chunk if chunk == "[DONE]" else f"data: {json.dumps(chunk)}" for chunk in chunks]


@pytest.fixture
def sandbox_adapter(hr_sandbox: dict, monkeypatch: pytest.MonkeyPatch) -> OpenAICompatAdapter:
    """Adapter whose config/auth live in the staging workspace.

    fleet.yaml carries a wire_override so `provider_for` resolves 'acme' to
    openai-compat without touching real configs; base_url_override keeps the
    endpoint URL synthetic; auth.json provides the key.
    """
    configs = hr_sandbox["configs"]
    (configs / "fleet.yaml").write_text("wire_overrides:\n  acme: openai-compat\n")
    models_path = hr_sandbox["tmp_path"] / "models.json"
    auth_path = hr_sandbox["tmp_path"] / "auth.json"
    models_path.write_text(json.dumps(MODELS_JSON), encoding="utf-8")
    auth_path.write_text(json.dumps(AUTH_JSON), encoding="utf-8")
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    return OpenAICompatAdapter(
        opencode_config_path=str(models_path),
        auth_json_path=str(auth_path),
        base_url_override="https://api.invalid",
    )


def _script_post(monkeypatch: pytest.MonkeyPatch, *responses: object) -> list[dict]:
    """Replace requests.post with a queue of scripted responses/call-records."""
    queue = list(responses)
    calls: list[dict] = []

    def fake_post(url, headers=None, json=None, stream=None, timeout=None):
        calls.append({"url": url, "json": json})
        if not queue:
            raise AssertionError("scripted post queue exhausted")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("requests.post", fake_post)
    return calls


# ---------------------------------------------------------------------------
# probe_capabilities + endpoint cache + model extras
# ---------------------------------------------------------------------------
def test_probe_capabilities_reads_models_extra(sandbox_adapter) -> None:
    caps = sandbox_adapter.probe_capabilities("acme/m")
    assert caps.model_id == "acme/m"
    assert caps.provider == "acme"
    assert caps.api_base_url == "https://api.invalid/chat/completions"
    assert caps.supports_thinking is False  # default (no models.yaml)
    assert caps.supports_vision is False
    assert caps.extra == {
        "context_window": 200000,
        "max_output_tokens": 8192,
        "reasoning_options": {"effort": "high"},
    }


def test_read_model_extra_empty_when_config_unreadable(monkeypatch, hr_sandbox) -> None:
    configs = hr_sandbox["configs"]
    (configs / "fleet.yaml").write_text("wire_overrides:\n  acme: openai-compat\n")
    auth_path = hr_sandbox["tmp_path"] / "auth.json"
    auth_path.write_text(json.dumps(AUTH_JSON), encoding="utf-8")
    # opencode_config_path does not exist -> _read_model_extra returns {}
    adapter = OpenAICompatAdapter(
        opencode_config_path=str(hr_sandbox["tmp_path"] / "missing.json"),
        auth_json_path=str(auth_path),
        base_url_override="https://api.invalid",
    )
    caps = adapter.probe_capabilities("acme/m")
    assert caps.extra == {}


def test_list_models_unreadable_config_yields_empty(sandbox_adapter) -> None:
    adapter = OpenAICompatAdapter(
        opencode_config_path="/nonexistent/models.json",
        auth_json_path="/nonexistent/auth.json",
    )
    assert adapter.list_models() == []


# ---------------------------------------------------------------------------
# chat: successful streaming
# ---------------------------------------------------------------------------
def test_chat_streams_text_thinking_tool_calls_and_usage(sandbox_adapter, monkeypatch) -> None:
    calls = _script_post(
        monkeypatch,
        FakeStreamResponse(
            lines=_sse(
                {"choices": [{"delta": {"reasoning_content": "step"}}]},
                {"choices": [{"delta": {"content": "Hello"}}]},
                {"choices": [{"delta": {"content": " world"}}]},
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "id": "call-1", "function": {"name": "calc", "arguments": '{"a": 1}'}}
                                ]
                            }
                        }
                    ]
                },
                {"usage": {"prompt_tokens": 11, "completion_tokens": 5}},
                "[DONE]",
            )
        ),
    )
    response = sandbox_adapter.chat(
        "acme/m",
        [{"role": "user", "content": "hi"}],
        tools=[{"name": "calc"}],
        thinking_budget=20_000,
        max_output=2048,
    )
    assert response.text == "Hello world"
    assert response.thinking == "step"
    assert response.tool_calls == [{"name": "calc", "input": {"a": 1}}]
    assert response.tokens_in == 11
    assert response.tokens_out == 5
    assert response.latency_ms >= 0
    assert calls[0]["url"] == "https://api.invalid/chat/completions"
    body = calls[0]["json"]
    assert body["model"] == "m"
    assert body["max_tokens"] == 2048
    assert body["stream"] is True
    assert body["reasoning_effort"] == "max"
    assert body["tools"][0]["function"]["name"] == "calc"
    assert body["messages"] == [{"role": "user", "content": "hi"}]


# ---------------------------------------------------------------------------
# chat: retryable transport failures
# ---------------------------------------------------------------------------
def test_chat_429_exhaustion_raises_rate_limit(sandbox_adapter, monkeypatch) -> None:
    calls = _script_post(monkeypatch, *(FakeStreamResponse(status_code=429, text="nope") for _ in range(MAX_RETRIES)))
    with pytest.raises(Exception) as exc:
        sandbox_adapter.chat("acme/m", [{"role": "user", "content": "hi"}])
    assert exc.value.status_code == 429
    assert exc.value.failure.code == FailureCode.RATE_LIMIT
    assert len(calls) == MAX_RETRIES


def test_chat_connection_error_not_retryable(sandbox_adapter, monkeypatch) -> None:
    calls = _script_post(
        monkeypatch,
        requests.exceptions.ConnectionError("boom"),
    )
    with pytest.raises(Exception) as exc:
        sandbox_adapter.chat("acme/m", [{"role": "user", "content": "hi"}])
    assert exc.value.status_code == 0
    assert exc.value.failure.code == FailureCode.UNKNOWN
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# chat: empty body fallthrough
# ---------------------------------------------------------------------------
def test_chat_closes_response_after_stream(sandbox_adapter, monkeypatch) -> None:
    stream = FakeStreamResponse(lines=_sse({"choices": [{"delta": {"content": "x"}}]}, "[DONE]"))
    _script_post(monkeypatch, stream)
    sandbox_adapter.chat("acme/m", [{"role": "user", "content": "hi"}])
    assert stream.closed == 1