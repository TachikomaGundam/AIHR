"""Stage-0 call + grade contract tests (committed surface).

Exercises hr.stage0_call.call_and_grade end to end with fakes: adapter
probing, thinking-budget wiring, infra-failure classification, routing
lookups, grader errors, and result shaping. Offline, deterministic.
"""

from __future__ import annotations

import pytest

import hr.calibrate as calibrate_mod
from hr.adapters.base import Capabilities
from hr.graders.base import GradeResult, ModelResponse
from hr.items.schema import ItemType, build_envelope
from hr.stage0_call import _build_messages, call_and_grade


class _FakeAdapter:
    def __init__(self, *, supports_thinking: bool = True, raise_chat: Exception | None = None):
        self.supports_thinking = supports_thinking
        self.raise_chat = raise_chat
        self.chat_calls: list[dict] = []
        self.probe_raised = False

    def probe_capabilities(self, model_id: str) -> Capabilities:
        if self.probe_raised:
            raise RuntimeError("probe exploded")
        return Capabilities(
            model_id=model_id, provider="fake", supports_thinking=self.supports_thinking
        )

    def chat(self, model_id: str, messages, **kwargs) -> ModelResponse:
        if self.raise_chat is not None:
            raise self.raise_chat
        self.chat_calls.append({"model_id": model_id, "messages": messages, "kwargs": kwargs})
        return ModelResponse(
            text="hello", thinking="hmm", tokens_in=11, tokens_out=22, latency_ms=33
        )


class _FakeGrader:
    def __init__(self, spec: str, *, raise_grade: Exception | None = None):
        self.spec = spec
        self.raise_grade = raise_grade

    def grade(self, payload, params, response) -> GradeResult:  # noqa: ARG002
        if self.raise_grade is not None:
            raise self.raise_grade
        return GradeResult(score=0.75, passed=True, detail={"k": 1})


class _FakeRegistry:
    def __init__(self, grader: _FakeGrader, *, raise_get: Exception | None = None):
        self.grader = grader
        self.raise_get = raise_get

    def get(self, spec: str) -> _FakeGrader:
        if self.raise_get is not None:
            raise self.raise_get
        self.grader.spec = spec
        return self.grader


def make_env(item_key: str, type_: ItemType) -> object:
    return build_envelope(
        item_key=item_key,
        type=type_,
        payload={},
        grading={"grader": "passthrough@1.0"},
        meta={"seats": ["f1"]},
    )


@pytest.fixture(autouse=True)
def _patch_calibrate(monkeypatch):
    monkeypatch.setattr(
        calibrate_mod, "_ROUTING", {ItemType.REASONING: ("exact_match@1.0", "passthrough")}
    )
    monkeypatch.setattr(
        calibrate_mod,
        "build_messages",
        lambda env: [{"role": "user", "content": env.payload.get("question", "q")}],
    )
    monkeypatch.setattr(calibrate_mod, "maybe_vision_image", lambda env, repo: None)
    monkeypatch.setattr(calibrate_mod, "build_grading_params", lambda env: {"expected": "x"})


def test_happy_path_full_result(monkeypatch) -> None:
    adapter = _FakeAdapter(supports_thinking=True)
    registry = _FakeRegistry(_FakeGrader("x"))
    env = make_env("reasoning.001", ItemType.REASONING)
    ok, result = call_and_grade(adapter, "m1", env, "ignored", registry)
    assert ok is True
    assert result.score == 0.75
    assert result.passed is True
    assert result.detail == {"k": 1}
    assert result.tokens_in == 11
    assert result.tokens_out == 22
    assert result.latency_ms == 33
    assert result.response_text == "hello"
    assert result.thinking_text == "hmm"
    (call,) = adapter.chat_calls
    assert call["kwargs"]["thinking_budget"] == 8192
    assert call["kwargs"]["max_output"] == 16384
    assert call["kwargs"]["timeout_s"] == 600
    assert call["kwargs"]["images"] is None
    assert call["kwargs"]["tools"] is None


def test_thinking_disabled_when_capabilities_say_so() -> None:
    adapter = _FakeAdapter(supports_thinking=False)
    ok, _result = call_and_grade(adapter, "m1", make_env("reasoning.1", ItemType.REASONING), "r", _FakeRegistry(_FakeGrader("x")))
    assert ok is True
    (call,) = adapter.chat_calls
    assert call["kwargs"]["thinking_budget"] is None


def test_probe_exception_degrades_to_no_thinking() -> None:
    adapter = _FakeAdapter(supports_thinking=True)
    adapter.probe_raised = True
    ok, _result = call_and_grade(adapter, "m1", make_env("reasoning.1", ItemType.REASONING), "r", _FakeRegistry(_FakeGrader("x")))
    assert ok is True
    (call,) = adapter.chat_calls
    assert call["kwargs"]["thinking_budget"] is None


@pytest.mark.parametrize(
    ("exc", "expected_infra"),
    [
        (RuntimeError("HTTP 429 rate limited"), "rate_limit"),
        (TimeoutError("request timed out"), "timeout"),
        (ValueError("boom"), "unknown"),
    ],
)
def test_infra_failure_classification(exc: Exception, expected_infra: str) -> None:
    adapter = _FakeAdapter(raise_chat=exc)
    ok, result = call_and_grade(adapter, "m1", make_env("reasoning.1", ItemType.REASONING), "r", _FakeRegistry(_FakeGrader("x")))
    assert ok is False
    assert result.score == 0.0
    assert result.passed is False
    assert result.infra_failure == expected_infra
    assert result.detail["infra_failure"] == expected_infra
    assert result.tokens_in == 0


def test_no_routing_returns_zero(monkeypatch) -> None:
    monkeypatch.setattr(calibrate_mod, "_ROUTING", {})
    adapter = _FakeAdapter()
    ok, result = call_and_grade(adapter, "m1", make_env("reasoning.1", ItemType.REASONING), "r", _FakeRegistry(_FakeGrader("x")))
    assert ok is True
    assert result.score == 0.0
    assert result.passed is False
    assert result.detail == {"no_routing": True}
    assert result.tokens_in == 11


def test_registry_get_error() -> None:
    adapter = _FakeAdapter()
    registry = _FakeRegistry(_FakeGrader("x"), raise_get=ValueError("no such grader"))
    ok, result = call_and_grade(adapter, "m1", make_env("reasoning.1", ItemType.REASONING), "r", registry)
    assert ok is True
    assert result.score == 0.0
    assert "no such grader" in result.detail["grader_error"]


def test_grader_grade_error() -> None:
    adapter = _FakeAdapter()
    registry = _FakeRegistry(_FakeGrader("x", raise_grade=KeyError("missing expected")))
    ok, result = call_and_grade(adapter, "m1", make_env("reasoning.1", ItemType.REASONING), "r", registry)
    assert ok is True
    assert result.score == 0.0
    assert "missing expected" in result.detail["grader_error"]


def test_vision_passes_images(monkeypatch) -> None:
    monkeypatch.setattr(
        calibrate_mod, "maybe_vision_image",
        lambda env, repo: [{"data": "QUJD", "media_type": "image/png"}],
    )
    monkeypatch.setattr(calibrate_mod, "_ROUTING", {ItemType.VISION: ("exact_match@1.0", "passthrough")})
    adapter = _FakeAdapter()
    ok, _result = call_and_grade(adapter, "m1", make_env("vision.ui_read.1", ItemType.VISION), "r", _FakeRegistry(_FakeGrader("x")))
    assert ok is True
    (call,) = adapter.chat_calls
    assert call["kwargs"]["images"] == [{"data": "QUJD", "media_type": "image/png"}]


def test_tool_a_passes_tools() -> None:
    env = build_envelope(
        item_key="tool_a.calc.add",
        type=ItemType.TOOL_A,
        payload={"tools": [{"name": "calc"}]},
        grading={"grader": "schema_valid@1.0"},
        meta={"seats": ["f1"]},
    )
    adapter = _FakeAdapter()
    ok, _result = call_and_grade(adapter, "m1", env, "r", _FakeRegistry(_FakeGrader("x")))
    assert ok is True
    (call,) = adapter.chat_calls
    assert call["kwargs"]["tools"] == [{"name": "calc"}]


def test_build_messages_delegates_to_calibrate() -> None:
    env = make_env("reasoning.1", ItemType.REASONING)
    assert _build_messages(env) == [{"role": "user", "content": "q"}]