from __future__ import annotations

from tests.bench.test_engine import engine  # noqa: F401
from tests.bench.test_engine import (
    BenchmarkCategory,
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
def test_code_gen_splits_one_request_per_function(engine: LivebenchEngine) -> None:  # noqa: F811
    # 0.6.5 (ruling C): 3 functions, 3 requests — one bad fence can no
    # longer take the whole 13-test battery down.
    outcome = engine.run_battery(MODEL, BenchmarkCategory.code_gen)
    assert outcome.score == pytest.approx(100.0)
    assert len(outcome.items) == 13
    fake = engine_mod.adapter_for(MODEL)
    crs = [
        cr for _m, cr in fake.requests
        if "Write this Python function" in str(cr.messages)
    ]
    assert len(crs) == 3
    for cr in crs:
        assert cr.max_output == 16384
        assert cr.thinking_budget is None
        assert cr.images is None and cr.tools is None
    # each function rides exactly one request
    for fn in ("sliding_window_median", "burst_balloons", "count_inversions"):
        assert sum(fn in str(cr.messages) for cr in crs) == 1


@pytest.mark.sandbox
def test_code_gen_all_empty_finals_is_battery_inconclusive() -> None:
    from hr.adapters.base import Capabilities
    from hr.bench.engine import LivebenchEngine
    from hr.graders.base import ModelResponse
    from hr.models import BenchmarkCategory

    class _EmptyRambler:
        def __init__(self) -> None:
            self.calls = 0

        def probe_capabilities(self, model_id: str) -> Capabilities:
            return Capabilities(
                model_id=model_id, provider="fake",
                supports_thinking=True, supports_vision=True,
            )

        def chat(self, model_id: str, messages: list, **kw: object) -> ModelResponse:
            self.calls += 1
            return ModelResponse(text="", thinking="", tokens_out=999, latency_ms=10)

    stub = _EmptyRambler()
    outcome = LivebenchEngine(adapter_factory=lambda _mid: stub).run_battery(
        "prov/m", BenchmarkCategory.code_gen
    )
    assert stub.calls == 3
    assert outcome.status == "inconclusive"


@pytest.mark.sandbox
def test_code_gen_one_truncated_function_only_costs_its_slice(
    engine: LivebenchEngine,  # noqa: F811
) -> None:
    from hr.bench.engine import LivebenchEngine as _Eng
    from hr.graders.base import ModelResponse
    from hr.models import BenchmarkCategory
    from tests.bench.fake_adapter import FakeAdapter

    class _CIEmptiesOut(FakeAdapter):
        def chat(self, model_id: str, messages: list, **kw: object) -> ModelResponse:
            joined = " ".join(str(m.get("content", "")) for m in messages)
            if "count_inversions" in joined:
                return ModelResponse(
                    text="", thinking="plans the merge forever",
                    tokens_out=4321, latency_ms=60,
                    finish_reason="length",
                )
            return super().chat(model_id, messages, **kw)

    adapter = _CIEmptiesOut()
    outcome = _Eng(adapter_factory=lambda _mid: adapter).run_battery(
        "prov/m", BenchmarkCategory.code_gen
    )
    # swm 8 + bb 3 stay 11/13; only the inversion slice (test.11-12) is lost
    assert outcome.score == pytest.approx(11 / 13 * 100.0)
    by_label = {item.label: item for item in outcome.items}
    assert all(by_label[f"test.{i:02d}"].passed for i in range(11))
    assert by_label["test.11"].score == 0.0 and by_label["test.12"].score == 0.0
    assert by_label["test.12"].finish_reason == "length"

def test_reasoning_splits_one_request_per_question(engine: LivebenchEngine) -> None:  # noqa: F811
    # 0.6.5 (owner ruling 2026-10-12): 13 questions, 13 requests — one
    # truncation can no longer zero the battery.
    outcome = engine.run_battery(MODEL, BenchmarkCategory.reasoning)
    assert outcome.score == pytest.approx(100.0)
    fake = engine_mod.adapter_for(MODEL)
    crs = [cr for _m, cr in fake.requests if "Solve this. Work briefly" in str(cr.messages)]
    assert len(crs) == 13
    for cr in crs:
        assert cr.thinking_budget == 4096
        assert cr.max_output == 16384
    # each question rides exactly one request: the q2-only text appears once
    assert sum("7^777" in str(cr.messages) for cr in crs) == 1

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


def test_reasoning_all_empty_finals_is_battery_inconclusive() -> None:
    from hr.adapters.base import Capabilities
    from hr.bench.engine import LivebenchEngine
    from hr.graders.base import ModelResponse
    from hr.models import BenchmarkCategory

    class _EmptyRambler:
        def __init__(self) -> None:
            self.calls = 0

        def probe_capabilities(self, model_id: str) -> Capabilities:
            return Capabilities(
                model_id=model_id, provider="fake",
                supports_thinking=True, supports_vision=True,
            )

        def chat(self, model_id: str, messages: list, **kw: object) -> ModelResponse:
            self.calls += 1
            return ModelResponse(text="", thinking="", tokens_out=999, latency_ms=10)

    stub = _EmptyRambler()
    outcome = LivebenchEngine(adapter_factory=lambda _mid: stub).run_battery(
        "prov/m", BenchmarkCategory.reasoning
    )
    assert stub.calls == 13
    assert outcome.status == "inconclusive"


def test_reasoning_isolates_one_blanked_question_per_row() -> None:
    from hr.bench.engine import LivebenchEngine
    from hr.graders.base import ModelResponse
    from hr.models import BenchmarkCategory
    from tests.bench.fake_adapter import FakeAdapter

    class _Q2BlanksOut(FakeAdapter):
        def chat(self, model_id: str, messages: list, **kw: object) -> ModelResponse:
            joined = " ".join(str(m.get("content", "")) for m in messages)
            if "7^777" in joined:
                return ModelResponse(
                    text="", thinking="loops on digits",
                    tokens_out=1234, latency_ms=50,
                    finish_reason="length",
                )
            return super().chat(model_id, messages, **kw)

    adapter = _Q2BlanksOut()
    outcome = LivebenchEngine(adapter_factory=lambda _mid: adapter).run_battery(
        "prov/m", BenchmarkCategory.reasoning
    )
    assert outcome.status == "scored"
    assert outcome.score == pytest.approx(12 / 13 * 100)
    by_q2 = [item for item in outcome.items if item.item_id.endswith(".q2")]
    assert len(by_q2) == 1
    assert by_q2[0].score == 0.0
    assert by_q2[0].finish_reason == "length"
    assert by_q2[0].tokens_out == 1234
    by_q1 = [item for item in outcome.items if item.item_id.endswith(".q1")]
    assert by_q1[0].score == 100.0
    # untouched rows keep the fake's own accounting, not q2's
    assert by_q1[0].tokens_out != 1234
