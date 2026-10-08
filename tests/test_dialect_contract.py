"""Dialect-contract unit tests: pure decision logic + scripted probe.

No live endpoint or DB needed; the probe takes an injected post() and the
gate is exercised through ensure_facts monkeypatching.
"""

from __future__ import annotations

import json

import pytest

from hr.dialect_contract import (
    PROBE_VERSION,
    DialectFacts,
    ProbeUnreachableError,
    contract_id_for,
    measurement_flags,
    pick_effort,
    probe_dialect,
    unmet_requirements,
    status_lines,
)
from hr.models import BenchmarkCategory


def facts(**kw) -> DialectFacts:
    base = dict(endpoint_url="http://h:8000/v1", model_slug="m",
                accepted_efforts=["xhigh", "medium", "low"],
                thinking_key="reasoning", usage_in_stream=True, tool_calls_ok=True)
    base.update(kw)
    return DialectFacts(**base)


class FakeResp:
    def __init__(self, status=200, lines=None, body=None):
        self.status_code = status
        self._lines = lines or []
        self._body = body or {}

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)

    def json(self):
        return self._body


def script(effort_statuses: dict[str, int], stream_lines=None, tool_body=None, vision_resp=None):
    calls: list[dict] = []

    def _is_vision(body: dict) -> bool:
        msgs = body.get("messages") or [{}]
        content = msgs[0].get("content")
        return isinstance(content, list) and any(p.get("type") == "image_url" for p in content)

    def post(url, *, headers, json: dict, timeout, stream=False):
        calls.append(json)
        eff = json.get("reasoning_effort")
        if eff is not None:
            return FakeResp(effort_statuses.get(eff, 400))
        if json.get("stream"):
            return FakeResp(200, lines=stream_lines or [])
        if _is_vision(json):
            return vision_resp if vision_resp is not None else FakeResp(200, body=tool_body or {})
        return FakeResp(200, body=tool_body or {})

    return post, calls


def test_probe_parses_vocabulary_keys_usage_and_tools() -> None:
    lines = [
        'data: {"choices":[{"delta":{"reasoning":"th"}}]}',
        'data: {"choices":[{"delta":{"content":"an"}}]}',
        'data: {"usage":{"prompt_tokens":5,"completion_tokens":9}}',
        "data: [DONE]",
    ]
    tool_body = {"choices": [{"message": {"tool_calls": [{"id": "c1"}]}}]}
    post, calls = script({"low": 200, "medium": 200, "xhigh": 200, "high": 400, "max": 400},
                         stream_lines=lines, tool_body=tool_body)
    f = probe_dialect("http://h:8000/v1", {}, "m", post)
    assert f.accepted_efforts == ["low", "medium", "xhigh"]
    assert f.thinking_key == "reasoning"
    assert f.usage_in_stream is True
    assert f.tool_calls_ok is True
    assert f.answer_chars_at_small_budget == 2
    assert len(calls) == 8  # 5 effort + 1 stream + 1 tool + 1 vision (B1): probe budget honoured


def _vision_msg_body(**message) -> dict:
    return {"choices": [{"message": {"role": "assistant", **message}}]}


def test_vision_probe_grants_on_content_or_reasoning_channels() -> None:
    """191 field regression: qwen3.8-flash-next answered the image probe with
    content=null and the full description in reasoning/reasoning_content; the
    v3 content-only grant SKIPped a proven-seeing endpoint (72-measurement
    sweep 2026-10-08). v4 grants on any non-empty answer channel."""
    for message in (
        {"content": "red"},
        {"content": None, "reasoning": "four colored squares"},
        {"content": None, "reasoning_content": "red"},
    ):
        post, _ = script({}, vision_resp=FakeResp(200, body=_vision_msg_body(**message)))
        f = probe_dialect("http://h:8000/v1", {}, "m", post)
        assert f.vision_ok is True, message


def test_vision_probe_refuses_rejection_and_empty_answers() -> None:
    """Status-first refusal survives the v4 widening: non-200 or an answer
    that is empty on every channel never grants."""
    for resp in (
        FakeResp(400),
        FakeResp(200, body=_vision_msg_body(content=None)),
        FakeResp(200, body=_vision_msg_body(content="   ")),
        FakeResp(200, body=_vision_msg_body(content=None, reasoning="")),
    ):
        post, _ = script({}, vision_resp=resp)
        f = probe_dialect("http://h:8000/v1", {}, "m", post)
        assert f.vision_ok is False, resp


def test_gate_blocks_thinking_battery_without_effort_evidence() -> None:
    assert unmet_requirements(BenchmarkCategory.reasoning, True, facts(accepted_efforts=[])) \
        == "accepted_efforts unproven for thinking"
    assert unmet_requirements(BenchmarkCategory.reasoning, False, facts(accepted_efforts=[])) is None
    assert unmet_requirements(BenchmarkCategory.tool_use, True, facts(tool_calls_ok=False)) \
        == "tool_calls_ok unproven for tool_calls"
    assert unmet_requirements(BenchmarkCategory.reasoning, True, None).startswith("no dialect contract")
    assert unmet_requirements(BenchmarkCategory.speed, True, facts()) is None


def test_pick_effort_only_uses_server_accepted_words() -> None:
    f = facts(accepted_efforts=["xhigh", "medium", "low"])
    assert pick_effort(f, 32768, "high") == "xhigh"
    assert pick_effort(f, 8192, "high") == "medium"
    assert pick_effort(f, 100, "high") == "low"
    narrow = facts(accepted_efforts=[])
    assert pick_effort(narrow, 32768, "high") is None  # proven-empty: never send
    assert pick_effort(None, 32768, "high") == "high"  # no contract (anthropic lane): untouched


def test_canary_flags_historic_fake_shapes() -> None:
    text = "x" * 300
    assert json.loads(measurement_flags(text, 0, 0, 5000)) == ["zero_usage_with_text"]
    assert json.loads(measurement_flags(text, 10, 5, 20)) == ["implausible_latency"]
    assert measurement_flags(text, 10, 5, 5000) is None
    assert measurement_flags(None, 0, 0, 20) is None


def test_contract_id_is_content_stable() -> None:
    assert contract_id_for(facts()) == contract_id_for(facts())
    assert contract_id_for(facts()) != contract_id_for(facts(tool_calls_ok=False))


def test_engine_gate_short_circuits_before_runner(monkeypatch) -> None:
    from hr.bench import engine as eng

    class Caps:
        supports_thinking = True
        supports_vision = False

    class StubAdapter:
        def probe_capabilities(self, _m):
            return Caps()

        def endpoint_for(self, _m):
            return "http://h:8000/v1", {}, "m"

        def attach_contract(self, f, force_low=False):
            self.attached = f

    import hr.dialect_contract as dc
    monkeypatch.setattr(dc, "ensure_facts", lambda m, a: facts(accepted_efforts=[]))
    e = eng.LivebenchEngine.__new__(eng.LivebenchEngine)
    e._adapter_factory = lambda _m: StubAdapter()
    e._RUNNERS = dict(eng.LivebenchEngine._RUNNERS)

    def _no_write_outcome(battery, model_id, result):
        _no_write_outcome.last = result
        return result.outcome

    monkeypatch.setattr(e, "_to_outcome", _no_write_outcome)
    e.run_battery("local-x/m", BenchmarkCategory.reasoning)
    assert _no_write_outcome.last.outcome.status == "not_applicable"
    assert "accepted_efforts" in _no_write_outcome.last.outcome.raw_output


def test_facts_json_roundtrip_is_the_real_serializer() -> None:
    """slots=True killed self.__dict__ silently behind stubbed cursors —
    this test runs the genuine dataclass serialization end to end."""
    f = DialectFacts(
        endpoint_url="http://h:1/v1", model_slug="m",
        accepted_efforts=["low", "xhigh"], thinking_key="reasoning",
        usage_in_stream=True, tool_calls_ok=False,
        answer_chars_at_small_budget=5,
    )
    restored = DialectFacts.from_json(f.to_json())
    assert restored == f


def test_persistence_sql_matches_shipped_ddl_columns() -> None:
    """One table, one source of truth: the INSERT column list in
    dialect_contract must equal the model_contract DDL in db_schema
    (the flat-vs-JSONB drift shipped 0 rows and crashed status)."""
    import re
    from pathlib import Path as _P

    schema = _P(__file__).resolve().parents[1] / "hr" / "db_schema.py"
    ddl = re.search(
        r"CREATE TABLE IF NOT EXISTS hr\.model_contract \((.*?)\n\);",
        schema.read_text(),
        flags=re.DOTALL,
    )
    assert ddl, "model_contract DDL not found in db_schema"
    ddl_cols = {
        line.split()[0] for line in ddl.group(1).splitlines() if line.strip()
    }
    src = _P(__file__).resolve().parents[1] / "hr" / "dialect_contract.py"
    ins = re.search(
        r"INSERT INTO hr\.model_contract \(([^)]*)\)", src.read_text()
    )
    assert ins
    insert_cols = {c.strip() for c in ins.group(1).split(",")}
    assert insert_cols == ddl_cols, insert_cols ^ ddl_cols
    # behavioural pin: status_lines() must project the shipped JSONB schema —
    # column archaeology via regex kept drifting; execute the real function.
    captured: dict[str, str] = {}

    class _Cur:
        def __enter__(self) -> "_Cur":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def execute(self, sql: str, params: object = None) -> None:
            captured["sql"] = sql

        def fetchall(self) -> list[tuple]:
            return []

    class _C:
        def cursor(self) -> _Cur:
            return _Cur()

    status_lines(_C())
    assert "facts_json::text" in captured["sql"], captured["sql"]
    assert "accepted_efforts" not in captured["sql"]


def _dead_post(*a: object, **k: object) -> None:
    raise ConnectionError("connection refused")


def test_probe_raises_instead_of_persisting_zero_capabilities() -> None:
    """Testbed 191, session3: every request 404'd behind a swallowed loop and
    an all-empty fact row froze the gate for 14 days. Unreachable now raises."""
    with pytest.raises(ProbeUnreachableError):
        probe_dialect("http://dead/v1", {}, "m", _dead_post)


def test_probe_posts_to_the_url_it_was_given() -> None:
    """endpoint_for returns the full chat/completions URL; the probe must not
    append a second suffix (the drift that produced the empty facts)."""
    seen: list[str] = []

    def spy(url: str, **k: object) -> object:
        seen.append(url)
        raise ConnectionError("stop after capture")

    with pytest.raises(ProbeUnreachableError):
        probe_dialect("http://h:1/v1/chat/completions", {}, "m", spy)
    assert seen and all(u == "http://h:1/v1/chat/completions" for u in seen)


def test_probe_version_bump_invalidates_poisoned_rows() -> None:
    """Session4 lesson: a v1 all-empty row (URL-suffix bug artifact) stayed
    'valid' for 14 days and the fixed binary trusted it. Contract loading is
    version-filtered, so bumping PROBE_VERSION is the sanctioned way to retire
    rows produced by a defective probe - evidence preserved, gate re-probes."""
    # v3 = B1 adds the vision fact; v2 rows retire via version filter.
    # v4 = vision grant accepts reasoning-channel answers after the 191
    #      content=null false negative; v3 rows retire the same way.
    assert PROBE_VERSION == 4


def test_survival_downgrade_matrix() -> None:
    from hr.dialect_contract import SURVIVAL_MIN_CHARS, survival_downgrade

    starving = facts(answer_chars_at_small_budget=SURVIVAL_MIN_CHARS - 1)
    healthy = facts(answer_chars_at_small_budget=9_999)
    unknown = facts(answer_chars_at_small_budget=None)
    assert survival_downgrade(BenchmarkCategory.code_gen, starving) is True
    assert survival_downgrade(BenchmarkCategory.tool_use, starving) is True
    assert survival_downgrade(BenchmarkCategory.code_gen, healthy) is False
    assert survival_downgrade(BenchmarkCategory.code_gen, unknown) is False
    assert survival_downgrade(BenchmarkCategory.code_gen, None) is False
    assert survival_downgrade(BenchmarkCategory.reasoning, starving) is False


def test_pick_effort_force_low_overrides_budget_map() -> None:
    from hr.dialect_contract import pick_effort

    starving = facts(answer_chars_at_small_budget=9)
    assert pick_effort(starving, 32768, "high") == "xhigh"
    assert pick_effort(starving, 32768, "high", force_low=True) == "low"
    # no contract: force_low must not invent a value for non-contract lanes
    assert pick_effort(None, 32768, "high", force_low=True) == "high"


def test_db_dsn_command_names_the_winner_without_leaking(monkeypatch) -> None:
    from typer.testing import CliRunner

    from hr.cli import app

    monkeypatch.setenv(
        "HR_DSN", "postgresql://u:sekretPW@dbhost:6543/targetdb"
    )
    result = CliRunner().invoke(app, ["db-dsn"])
    assert result.exit_code == 0, result.output
    assert "DSN source: HR_DSN env var" in result.output
    assert "dbhost:6543/targetdb" in result.output
    assert "sekretPW" not in result.output


def test_status_on_empty_board_is_a_state_not_an_error(monkeypatch) -> None:
    from typer.testing import CliRunner

    import hr.cli_apply as ca
    from hr.cli import app

    def _boom(_fn):
        raise ValueError("no sweeps found in hr.sweep")

    monkeypatch.setattr(ca, "_with_conn", _boom)
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "no data yet" in result.output


def test_measurement_rows_bind_contract_when_hint_present(scratch_db) -> None:
    """191 audit (sweep a8d46a): 0/50 rows carried contract_id because the
    livebench writer never forwarded model_id to _insert_measurement."""
    import psycopg2

    from hr.dialect_contract import _BINDING, bound_contract_id
    from hr.stage0_storage import _insert_measurement
    from tests._db_contracts_helpers import (
        seed_battery,
        seed_item_pool,
        seed_provider_models,
        seed_seat,
        seed_sweep,
    )

    _name, dsn = scratch_db
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        seed_provider_models(conn, ("m/x",))
        seed_seat(conn, "oracle", "High-IQ consultant")
        seed_seat(conn, "_stage0_sweep", "stage0 sweep")
        seed_sweep(conn, "sw-bind")
        battery = seed_battery(conn, "speed")
        seed_item_pool(conn, "item-1")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO hr.run (run_id, sweep_id, model_id, battery_id, "
                "round, started_at, status, contract_id) VALUES "
                "('run-bind', 'sw-bind', 'm/x', %s, "
                "1, now(), 'scored', 'dc-abc123')",
                (battery,),
            )
        _BINDING["m/x"] = "dc-abc123"
        assert bound_contract_id("m/x") == "dc-abc123"
        _insert_measurement(
            conn, "meas-bind", "run-bind", "item-1", 1, 100.0,
            tokens_in=300, tokens_out=200, latency_ms=5000,
            response_text="x" * 100, model_id="m/x",
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT contract_id FROM hr.measurement WHERE measurement_id='meas-bind'"
            )
            assert cur.fetchone()[0] == "dc-abc123"
    finally:
        _BINDING.pop("m/x", None)
        conn.close()
