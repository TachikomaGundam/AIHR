"""Engine e2e tests — mocked adapter, NO network (task 12).

- run_battery drives every benchmark through a ChatRequest-shaped call.
- store() writes hr2 sweep/run/measurement rows with battery linkage.
- SQL asserts: 10 livebench batteries, item counts 13/13/16/1/3/8/4/1/1/4,
  seat_battery links, item_pool rows, per-battery means == expected scores.
- Garbage adapter -> score 0 run recorded as failed (never crash).
"""

from __future__ import annotations

import uuid

import pytest

import hr.bench.engine as engine_mod
from hr.adapters.base import ChatRequest
from hr.bench.engine import LivebenchEngine
from hr.bench.livebench import (
    LIVEBENCH_BATTERIES,
    battery_code,
    battery_item_labels,
)
from hr.graders.base import ModelResponse
from hr.models import BenchmarkCategory
from tests.bench.fake_adapter import (
    CORRECT_CODE,
    FakeAdapter,
    FlakyStressAdapter,
    ForgetfulStressAdapter,
    GARBAGE_TEXT,
    NoVisionAdapter,
    PERFECT_INSTRUCTION_JSON,
    PERFECT_NEEDLES,
    PERFECT_VISION,
    ToolsRejectedAdapter,
    perfect_long_horizon_answer,
    perfect_reasoning_answer,
)

MODEL = "fake/test-model"


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> LivebenchEngine:
    fake = FakeAdapter()
    monkeypatch.setattr(engine_mod, "adapter_for", lambda model_id: fake)
    return LivebenchEngine()


# ---------------------------------------------------------------------------
# run_battery: ChatRequest-shaped calls per benchmark (no DB)
# ---------------------------------------------------------------------------


def test_every_battery_runs_through_chat_request(engine: LivebenchEngine) -> None:
    for battery in LIVEBENCH_BATTERIES:
        outcome = engine.run_battery(MODEL, battery)
        assert outcome.model_id == MODEL
        assert outcome.battery == battery
        assert 0.0 <= outcome.score <= 100.0
        assert isinstance(outcome.latency_ms, int)
        assert isinstance(outcome.tokens_in, int) or outcome.tokens_in is None
        assert outcome.raw_output != ""


def test_code_gen_request_shape(engine: LivebenchEngine) -> None:
    outcome = engine.run_battery(MODEL, BenchmarkCategory.code_gen)
    assert outcome.score == pytest.approx(100.0)
    fake = engine_mod.adapter_for(MODEL)
    _model_id, cr = fake.requests[-1]
    assert isinstance(cr, ChatRequest)
    assert cr.max_output == 32768
    assert cr.thinking_budget is None  # direct-output benchmark: no thinking
    assert cr.images is None and cr.tools is None


def test_reasoning_request_carries_thinking_budget(engine: LivebenchEngine) -> None:
    outcome = engine.run_battery(MODEL, BenchmarkCategory.reasoning)
    assert outcome.score == pytest.approx(100.0)
    fake = engine_mod.adapter_for(MODEL)
    _model_id, cr = fake.requests[-1]
    assert cr.thinking_budget == 4096
    assert cr.max_output == 32768


def test_instruction_follow_request_shape(engine: LivebenchEngine) -> None:
    outcome = engine.run_battery(MODEL, BenchmarkCategory.instruction_follow)
    assert outcome.score == pytest.approx(100.0)
    fake = engine_mod.adapter_for(MODEL)
    _model_id, cr = fake.requests[-1]
    assert cr.thinking_budget is None
    assert cr.max_output == 32768


def test_tool_use_multi_turn_loop_and_tools_payload(engine: LivebenchEngine) -> None:
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


def test_long_context_request_shape(engine: LivebenchEngine) -> None:
    outcome = engine.run_battery(MODEL, BenchmarkCategory.long_context)
    assert outcome.score == pytest.approx(100.0)
    fake = engine_mod.adapter_for(MODEL)
    _model_id, cr = fake.requests[-1]
    msg_text = cr.messages[0]["content"]
    assert "RECOVERY codes" in msg_text
    assert len(msg_text) > 200_000  # ~240K char haystack


def test_attention_probe_request_shape(engine: LivebenchEngine) -> None:
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


def test_attention_stress_request_shape(engine: LivebenchEngine) -> None:
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


def test_speed_uses_response_tokens_and_latency(engine: LivebenchEngine) -> None:
    out = engine.run_battery(MODEL, BenchmarkCategory.speed)
    # Fake: 2000 tokens / 2s -> 1000 t/s -> top tier 90.
    assert out.score == pytest.approx(90.0)


def test_long_horizon_request_shape(engine: LivebenchEngine) -> None:
    out = engine.run_battery(MODEL, BenchmarkCategory.long_horizon)
    assert out.score == pytest.approx(100.0)
    fake = engine_mod.adapter_for(MODEL)
    _model_id, cr = fake.requests[-1]
    assert cr.thinking_budget == 8192
    assert cr.max_output == 16384
    assert len(out.items) == 4


def test_unknown_model_is_recorded_failed_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(model_id: str):
        raise ValueError(f"no provider type configured for {model_id!r}")

    monkeypatch.setattr(engine_mod, "adapter_for", _boom)
    out = LivebenchEngine().run_battery("bogus/model", BenchmarkCategory.speed)
    assert out.score == 0.0
    assert out.passed is False
    assert "ERROR" in out.raw_output


def test_garbage_adapter_scores_zero_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    garbage = FakeAdapter(garbage=True)
    monkeypatch.setattr(engine_mod, "adapter_for", lambda model_id: garbage)
    engine = LivebenchEngine()
    for battery in LIVEBENCH_BATTERIES:
        out = engine.run_battery(MODEL, battery)
        if battery is BenchmarkCategory.speed:
            # speed measures the response's tok/s tier: garbage still ships
            # tokens/latency, so it scores its tier (90 here) — never a 0.
            assert out.score == pytest.approx(90.0)
            assert out.passed is True
        else:
            assert out.score == 0.0
            assert out.passed is False


# ---------------------------------------------------------------------------
# store + SQL asserts (scratch DB, credential-gated)
# ---------------------------------------------------------------------------


def _sql(conn, sql: str, params: tuple | None = None) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return list(cur.fetchall())


def _run_all_batteries(engine: LivebenchEngine, conn, sweep_id: str, model: str) -> None:
    for battery in LIVEBENCH_BATTERIES:
        out = engine.run_battery(model, battery)
        engine.store(conn, sweep_id, model, battery, out)


def test_e2e_all_batteries_write_measurements_with_linkage(
    engine: LivebenchEngine, scratch_conn
) -> None:
    sweep_id = f"qa-bench12-{uuid.uuid4().hex[:8]}"
    engine.ensure_registered(scratch_conn)
    _run_all_batteries(engine, scratch_conn, sweep_id, MODEL)

    # -- battery rows ----------------------------------------------------
    codes = sorted(
        r[0] for r in _sql(scratch_conn, "SELECT battery_code FROM hr2.battery")
    )
    expected_codes = sorted(battery_code(b) for b in LIVEBENCH_BATTERIES)
    assert codes == expected_codes

    # -- battery_item counts (13/13/16/1/3/8/4/1/1/4) ----------------------
    rows = _sql(
        scratch_conn,
        """
        SELECT b.battery_code, COUNT(bi.item_id)::int
          FROM hr2.battery b
          LEFT JOIN hr2.battery_item bi ON bi.battery_id = b.battery_id
         WHERE b.battery_code LIKE 'livebench_%%'
         GROUP BY b.battery_code
        """,
    )
    counts = dict(rows)
    for battery in LIVEBENCH_BATTERIES:
        assert counts[battery_code(battery)] == len(battery_item_labels(battery))

    # -- seat_battery links (honest n_initial/n_max) ----------------------
    links = _sql(
        scratch_conn,
        """
        SELECT b.battery_code, sb.n_initial, sb.n_max
          FROM hr2.seat_battery sb
          JOIN hr2.battery b ON b.battery_id = sb.battery_id
         WHERE b.battery_code LIKE 'livebench_%%'
         ORDER BY b.battery_code
        """,
    )
    assert len(links) == 10
    for code, n_initial, n_max in links:
        assert n_initial is not None and n_max is not None
        assert 1 <= n_initial <= n_max

    # -- item_pool --------------------------------------------------------
    n_pool = _sql(
        scratch_conn, "SELECT COUNT(*) FROM hr2.item_pool WHERE kind = 'livebench'"
    )[0][0]
    assert n_pool == 64

    # -- sweep / runs / measurements --------------------------------------
    assert _sql(
        scratch_conn, "SELECT COUNT(*) FROM hr2.sweep WHERE sweep_id = %s", (sweep_id,)
    )[0][0] == 1
    runs = _sql(
        scratch_conn,
        """
        SELECT COUNT(*), COUNT(DISTINCT battery_id)
          FROM hr2.run WHERE sweep_id = %s
        """,
        (sweep_id,),
    )[0]
    assert runs == (10, 10)
    n_meas = _sql(
        scratch_conn,
        "SELECT COUNT(*) FROM hr2.measurement m JOIN hr2.run r ON r.run_id = m.run_id "
        "WHERE r.sweep_id = %s",
        (sweep_id,),
    )[0][0]
    assert n_meas == 13 + 13 + 16 + 1 + 3 + 8 + 4 + 1 + 1 + 4

    # -- per-battery means equal the v1 battery score --------------------
    # (100.0 everywhere except speed: fake responds 2000 tok / 2s -> tier 90)
    expected = {battery_code(b): 100.0 for b in LIVEBENCH_BATTERIES}
    expected[battery_code(BenchmarkCategory.speed)] = 90.0
    means = _sql(
        scratch_conn,
        """
        SELECT b.battery_code, AVG(m.score)::float8
          FROM hr2.measurement m
          JOIN hr2.run r ON r.run_id = m.run_id
          JOIN hr2.battery b ON b.battery_id = r.battery_id
         WHERE r.sweep_id = %s
         GROUP BY b.battery_code
        """,
        (sweep_id,),
    )
    assert dict(means) == expected

    # -- requested_max_output recorded from ChatRequest --------------------
    caps = _sql(
        scratch_conn,
        """
        SELECT DISTINCT b.battery_code, m.requested_max_output
          FROM hr2.measurement m
          JOIN hr2.run r ON r.run_id = m.run_id
          JOIN hr2.battery b ON b.battery_id = r.battery_id
         WHERE r.sweep_id = %s AND m.requested_max_output IS NOT NULL
         ORDER BY b.battery_code
        """,
        (sweep_id,),
    )
    by_code = dict(caps)
    assert by_code[battery_code(BenchmarkCategory.code_gen)] == 32768
    assert by_code[battery_code(BenchmarkCategory.reasoning)] == 32768
    assert by_code[battery_code(BenchmarkCategory.tool_use)] == 4096
    assert by_code[battery_code(BenchmarkCategory.long_context)] == 16384
    assert by_code[battery_code(BenchmarkCategory.attention_probe)] == 16384
    assert by_code[battery_code(BenchmarkCategory.attention_stress)] == 16384
    assert by_code[battery_code(BenchmarkCategory.long_horizon)] == 16384

    # -- response text stored (proof of the mined-output path) -------------
    with_text = _sql(
        scratch_conn,
        """
        SELECT COUNT(*) FROM hr2.measurement m
          JOIN hr2.run r ON r.run_id = m.run_id
         WHERE r.sweep_id = %s AND m.response_text IS NOT NULL
        """,
        (sweep_id,),
    )[0][0]
    assert with_text == n_meas


def test_e2e_idempotent_registration(scratch_conn) -> None:
    engine = LivebenchEngine()
    engine.ensure_registered(scratch_conn)
    first = _sql(scratch_conn, "SELECT COUNT(*) FROM hr2.battery")[0][0]
    engine.ensure_registered(scratch_conn)
    second = _sql(scratch_conn, "SELECT COUNT(*) FROM hr2.battery")[0][0]
    assert first == second == 10


def test_e2e_garbage_adapter_records_failed_run_not_crash(
    monkeypatch: pytest.MonkeyPatch, scratch_conn
) -> None:
    monkeypatch.setattr(
        engine_mod, "adapter_for", lambda model_id: FakeAdapter(garbage=True)
    )
    sweep_id = f"qa-bench12-garbage-{uuid.uuid4().hex[:8]}"
    engine = LivebenchEngine()
    engine.ensure_registered(scratch_conn)
    _run_all_batteries(engine, scratch_conn, sweep_id, MODEL)
    rows = _sql(
        scratch_conn,
        """
        SELECT b.battery_code, COUNT(m.measurement_id)::int, AVG(m.score)::float8
          FROM hr2.measurement m
          JOIN hr2.run r ON r.run_id = m.run_id
          JOIN hr2.battery b ON b.battery_id = r.battery_id
         WHERE r.sweep_id = %s
         GROUP BY b.battery_code
        """,
        (sweep_id,),
    )
    assert len(rows) == 10  # all 10 batteries recorded
    for code, count, mean in rows:
        assert count >= 1
        if code == "livebench_speed":
            assert mean == pytest.approx(90.0)  # garbage still has tokens/latency
        else:
            assert mean == 0.0  # garbage -> 0, never raised


def test_store_writes_expected_response_text(engine: LivebenchEngine, scratch_conn) -> None:
    sweep_id = f"qa-bench12-text-{uuid.uuid4().hex[:8]}"
    engine.ensure_registered(scratch_conn)
    out = engine.run_battery(MODEL, BenchmarkCategory.instruction_follow)
    engine.store(scratch_conn, sweep_id, MODEL, BenchmarkCategory.instruction_follow, out)
    texts = _sql(
        scratch_conn,
        """
        SELECT m.response_text, m.thinking_text
          FROM hr2.measurement m
          JOIN hr2.run r ON r.run_id = m.run_id
         WHERE r.sweep_id = %s
        """,
        (sweep_id,),
    )
    assert len(texts) == 16
    assert all(t == PERFECT_INSTRUCTION_JSON for t, _th in texts)
    # thinking_budget None -> no thinking stored (NULL or empty string)
    assert all(th is None or th == "" for _t, th in texts)