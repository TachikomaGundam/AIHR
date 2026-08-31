from __future__ import annotations

from tests.bench.test_engine import engine  # noqa: F401
from tests.bench.test_engine import (
    BenchmarkCategory,
    ChatRequest,
    FakeAdapter,
    FlakyStressAdapter,
    ForgetfulStressAdapter,
    LIVEBENCH_BATTERIES,
    LivebenchEngine,
    MODEL,
    NoVisionAdapter,
    ToolsRejectedAdapter,
    engine_mod,
    pytest
)

def test_every_battery_runs_through_chat_request(engine: LivebenchEngine) -> None:  # noqa: F811 (fixture param shadows re-exported engine)
    for battery in LIVEBENCH_BATTERIES:
        outcome = engine.run_battery(MODEL, battery)
        assert outcome.model_id == MODEL
        assert outcome.battery == battery
        assert 0.0 <= outcome.score <= 100.0
        assert isinstance(outcome.latency_ms, int)
        assert isinstance(outcome.tokens_in, int) or outcome.tokens_in is None
        assert outcome.raw_output != ""

@pytest.mark.sandbox
def test_code_gen_request_shape(engine: LivebenchEngine) -> None:  # noqa: F811 (fixture param shadows re-exported engine)
    outcome = engine.run_battery(MODEL, BenchmarkCategory.code_gen)
    assert outcome.score == pytest.approx(100.0)
    fake = engine_mod.adapter_for(MODEL)
    _model_id, cr = fake.requests[-1]
    assert isinstance(cr, ChatRequest)
    assert cr.max_output == 32768
    assert cr.thinking_budget is None  # direct-output benchmark: no thinking
    assert cr.images is None and cr.tools is None

def test_reasoning_request_carries_thinking_budget(engine: LivebenchEngine) -> None:  # noqa: F811 (fixture param shadows re-exported engine)
    outcome = engine.run_battery(MODEL, BenchmarkCategory.reasoning)
    assert outcome.score == pytest.approx(100.0)
    fake = engine_mod.adapter_for(MODEL)
    _model_id, cr = fake.requests[-1]
    assert cr.thinking_budget == 4096
    assert cr.max_output == 32768

def test_instruction_follow_request_shape(engine: LivebenchEngine) -> None:  # noqa: F811 (fixture param shadows re-exported engine)
    outcome = engine.run_battery(MODEL, BenchmarkCategory.instruction_follow)
    assert outcome.score == pytest.approx(100.0)
    fake = engine_mod.adapter_for(MODEL)
    _model_id, cr = fake.requests[-1]
    assert cr.thinking_budget is None
    assert cr.max_output == 32768

def test_tool_use_multi_turn_loop_and_tools_payload(engine: LivebenchEngine) -> None:  # noqa: F811 (fixture param shadows re-exported engine)
    outcome = engine.run_battery(MODEL, BenchmarkCategory.tool_use)
    assert outcome.score == pytest.approx(100.0)
    fake = engine_mod.adapter_for(MODEL)
    tool_calls = [cr for _m, cr in fake.requests if cr.tools]
    # tools ride on both turns: the initial request AND the follow-up turn
    # after the tool_result.
    assert len(tool_calls) == 2
    cr = tool_calls[0]
    assert cr.tools[0]["name"] == "calculate"
    assert cr.max_output == 4096
    assert cr.thinking_budget == 4096  # supports_thinking -> budget like v1
    # the loop must have produced a second turn carrying the tool_result
    result_turns = [
        cr
        for _m, cr in fake.requests
        if any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for m in cr.messages
            if isinstance(m.get("content"), list)
            for b in m["content"]
        )
    ]
    assert result_turns, "expected a tool_result turn after the tool call"

def test_tool_use_falls_back_without_tools_when_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejected = ToolsRejectedAdapter()
    monkeypatch.setattr(engine_mod, "adapter_for", lambda model_id: rejected)
    outcome = LivebenchEngine().run_battery(MODEL, BenchmarkCategory.tool_use)
    # Retried without tools on turn 0; final text graded (60 without tool use).
    assert outcome.score == pytest.approx(60.0)
    assert outcome.passed is False

def test_long_context_request_shape(engine: LivebenchEngine) -> None:  # noqa: F811 (fixture param shadows re-exported engine)
    outcome = engine.run_battery(MODEL, BenchmarkCategory.long_context)
    assert outcome.score == pytest.approx(100.0)
    fake = engine_mod.adapter_for(MODEL)
    _model_id, cr = fake.requests[-1]
    msg_text = cr.messages[0]["content"]
    assert "RECOVERY codes" in msg_text
    assert len(msg_text) > 200_000

def test_attention_probe_request_shape(engine: LivebenchEngine) -> None:  # noqa: F811 (fixture param shadows re-exported engine)
    outcome = engine.run_battery(MODEL, BenchmarkCategory.attention_probe)
    assert outcome.score == pytest.approx(100.0)
    fake = engine_mod.adapter_for(MODEL)
    _model_id, cr = fake.requests[-1]
    msg_text = cr.messages[0]["content"]
    assert "Answer each line exactly" in msg_text
    assert len(msg_text) > 200_000  # ~240K char haystack
    assert cr.thinking_budget == 8192
    assert cr.max_output == 16384
    assert len(outcome.items) == 8
    assert [i.label for i in outcome.items] == [
        "pos_head", "pos_mid_early", "pos_mid", "pos_mid_late", "pos_tail",
        "assoc_literal", "assoc_infer", "decoy_resist",
    ]


def test_attention_probe_is_reproducible_for_the_same_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = FakeAdapter()
    second = FakeAdapter()
    engines = iter((first, second))
    monkeypatch.setattr(engine_mod, "adapter_for", lambda model_id: next(engines))

    LivebenchEngine(seed=17).run_battery(MODEL, BenchmarkCategory.attention_probe)
    LivebenchEngine(seed=17).run_battery(MODEL, BenchmarkCategory.attention_probe)

    assert first.requests[-1][1].messages == second.requests[-1][1].messages

def test_attention_stress_request_shape(engine: LivebenchEngine) -> None:  # noqa: F811 (fixture param shadows re-exported engine)
    outcome = engine.run_battery(MODEL, BenchmarkCategory.attention_stress)
    assert outcome.score == pytest.approx(100.0)
    fake = engine_mod.adapter_for(MODEL)
    _model_id, cr = fake.requests[-1]
    assert cr.thinking_budget == 8192
    assert cr.max_output == 16384
    assert len(fake.requests) == 20  # instruction + 19 canned turns, sequential
    assert fake.requests[0][1].messages[0]["content"].startswith(
        "We are starting a long working session"
    )
    assert len(outcome.items) == 4
    assert [i.label for i in outcome.items] == [
        "survive_t5", "survive_t10", "survive_t15", "survive_t20",
    ]
    # history accumulates: the 20th request carries all previous assistant
    # replies as text-only blocks (thinking stripped)
    last_messages = fake.requests[-1][1].messages
    assistant_blocks = [
        m for m in last_messages if m.get("role") == "assistant"
    ]
    assert len(assistant_blocks) == 19
    assert all(
        isinstance(m["content"], list)
        and len(m["content"]) == 1
        and m["content"][0]["type"] == "text"
        for m in assistant_blocks
    )

def test_attention_stress_forgetful_model_fails_late_checkpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forgetful = ForgetfulStressAdapter(drop_after=11)
    monkeypatch.setattr(engine_mod, "adapter_for", lambda model_id: forgetful)
    out = LivebenchEngine().run_battery(MODEL, BenchmarkCategory.attention_stress)
    assert out.score == pytest.approx(50.0)
    assert out.passed is False
    assert [i.label for i in out.items if not i.passed] == [
        "survive_t15", "survive_t20",
    ]
    assert "survive_t15: end_token" in out.raw_output

def test_attention_stress_transient_error_retries_same_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-off 503 mid-conversation must retry the failing turn only."""
    flaky = FlakyStressAdapter(fail_turn=17)
    monkeypatch.setattr(engine_mod, "adapter_for", lambda model_id: flaky)
    out = LivebenchEngine().run_battery(MODEL, BenchmarkCategory.attention_stress)
    assert flaky.failed is True
    assert out.score == pytest.approx(100.0)
    assert out.passed is True
    # 20 recorded turns only — the conversation was never restarted
    requests = flaky.requests
    assert len(requests) == 20
    # the retried turn resends the exact same accumulated history
    retried = requests[flaky.fail_turn - 1][1].messages
    assert retried == flaky.failed_messages
    assert [i.passed for i in out.items] == [True, True, True, True]

def test_vision_request_carries_image_and_respects_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAdapter()
    monkeypatch.setattr(engine_mod, "adapter_for", lambda model_id: fake)
    out = LivebenchEngine().run_battery(MODEL, BenchmarkCategory.vision)
    assert out.score == pytest.approx(100.0)
    _model_id, cr = fake.requests[-1]
    assert cr.images is not None and cr.images[0]["media_type"] == "image/png"
    assert cr.images[0]["data"]

    # No vision support -> SKIP outcome, zero score, never calls chat.
    blind = NoVisionAdapter()
    monkeypatch.setattr(engine_mod, "adapter_for", lambda model_id: blind)
    skipped = LivebenchEngine().run_battery(MODEL, BenchmarkCategory.vision)
    assert skipped.score == 0.0
    assert "SKIP" in skipped.raw_output
    assert skipped.status == "not_applicable"


def test_adapter_setup_failure_is_inconclusive() -> None:
    def failing_factory(model_id: str):
        del model_id
        raise RuntimeError("gateway unavailable")

    outcome = LivebenchEngine(adapter_factory=failing_factory).run_battery(
        MODEL, BenchmarkCategory.reasoning
    )

    assert outcome.status == "inconclusive"
    assert outcome.passed is False

def test_speed_uses_response_tokens_and_latency(engine: LivebenchEngine) -> None:  # noqa: F811 (fixture param shadows re-exported engine)
    out = engine.run_battery(MODEL, BenchmarkCategory.speed)
    # Fake: 2000 tokens / 2s -> 1000 t/s -> top tier 90.
    assert out.score == pytest.approx(90.0)

def test_long_horizon_request_shape(engine: LivebenchEngine) -> None:  # noqa: F811 (fixture param shadows re-exported engine)
    out = engine.run_battery(MODEL, BenchmarkCategory.long_horizon)
    assert out.score == pytest.approx(100.0)
    fake = engine_mod.adapter_for(MODEL)
    _model_id, cr = fake.requests[-1]
    assert cr.thinking_budget == 8192
    assert cr.max_output == 16384
    assert len(out.items) == 4
