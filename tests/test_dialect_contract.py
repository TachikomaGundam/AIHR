"""Dialect-contract unit tests: pure decision logic + scripted probe.

No live endpoint or DB needed; the probe takes an injected post() and the
gate is exercised through ensure_facts monkeypatching.
"""

from __future__ import annotations

import json

import pytest

from hr.dialect_contract import (
    DialectFacts,
    contract_id_for,
    measurement_flags,
    pick_effort,
    probe_dialect,
    unmet_requirements,
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


def script(effort_statuses: dict[str, int], stream_lines=None, tool_body=None):
    calls: list[dict] = []

    def post(url, *, headers, json: dict, timeout, stream=False):
        calls.append(json)
        eff = json.get("reasoning_effort")
        if eff is not None:
            return FakeResp(effort_statuses.get(eff, 400))
        if json.get("stream"):
            return FakeResp(200, lines=stream_lines or [])
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
    assert len(calls) == 7  # 5 effort + 1 stream + 1 tool: probe budget honoured


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

        def attach_contract(self, f):
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
    out = e.run_battery("local-x/m", BenchmarkCategory.reasoning)
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
    status_cols = {
        part.split()[0].replace("::text", "")
        for part in re.search(
            'SELECT ([^\']*) FROM hr\\.model_contract ORDER BY',
            src.read_text(),
        ).group(1).split(",")
    }
    assert {"model_id", "endpoint_url", "facts_json", "probe_version", "probed_at"} == status_cols
