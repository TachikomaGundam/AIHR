"""0.4.7 offline lane: honest rendering, empty-final guard, --quick selection."""
from __future__ import annotations

from types import SimpleNamespace

from hr.bench.engine_interactive import EngineInteractiveMixin
from hr.bench.engine_results import BenchOutcome, ItemResult
from hr.cli_inventory import bench_row_markdown
from hr.graders.base import ModelResponse


def _outcome(**kw):
    base = dict(battery=None, model_id="m/x", score=0.0, passed=False)
    base.update(kw)
    return BenchOutcome(**base)


def test_not_applicable_renders_skip_not_zero_fail() -> None:
    row = bench_row_markdown(
        "m/x", "livebench_code_gen",
        _outcome(status="not_applicable", raw_output="SKIP: sandbox unavailable (frozen-binary)"),
    )
    assert "SKIP(sandbox unavailable (frozen-binary))" in row
    assert "FAIL" not in row and "0.0" not in row


def test_inconclusive_renders_loudly() -> None:
    row = bench_row_markdown(
        "m/x", "livebench_tool_use",
        _outcome(status="inconclusive", raw_output="INCONCLUSIVE: empty final answer"),
    )
    assert "INCONCLUSIVE(empty final answer)" in row and "FAIL" not in row


def test_scored_rows_keep_pass_fail_contract() -> None:
    items = [ItemResult(item_id="i1", label="i1", passed=True, score=100.0)]
    ok = bench_row_markdown("m/x", "livebench_speed", _outcome(score=100.0, passed=True, items=items))
    bad = bench_row_markdown("m/x", "livebench_speed", _outcome(items=[]))
    assert "| PASS |" in ok and "FAIL" not in ok
    assert "| FAIL |" in bad


class _LoopEng(EngineInteractiveMixin):
    _timeout_s = 5

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._rs = list(responses)

    def _chat(self, model_id, adapter, caps, cr):  # type: ignore[override]
        return self._rs.pop(0)


def _resp(**kw):
    base = dict(text="", thinking="", tool_calls=[], raw={}, latency_ms=1000,
                tokens_in=100, tokens_out=0)
    base.update(kw)
    return ModelResponse(**base)


def test_empty_final_answer_after_generated_tokens_is_inconclusive() -> None:
    """191 sweep 790a73: model generated (tool calls) but no final text was
    recorded — must NOT be stamped scored-0."""
    eng = _LoopEng([
        _resp(tool_calls=[{"id": "t1", "name": "calculate",
                           "input": {"expression": "52.50+49.98"}}], tokens_out=120),
        _resp(tokens_out=30),  # empty content -> loop breaks with tokens spent
    ])
    res = eng._run_tool_use("m/x", None, SimpleNamespace(supports_thinking=True))
    assert res.outcome.status == "inconclusive"
    assert "empty final answer" in res.outcome.raw_output
    assert res.tokens_out == 150  # accounting preserved for the ops view


from typer.testing import CliRunner

from hr.cli import app

_runner = CliRunner()


def test_quick_flag_selects_five_lane_batteries(monkeypatch) -> None:
    result = _runner.invoke(
        app, ["bench", "--models", "fake/x", "--quick", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "tool_use" in result.output and "reasoning" in result.output
    assert "attention_stress" not in result.output  # full-lane batteries excluded
    assert "long_horizon" not in result.output


def test_explicit_battery_beats_quick(monkeypatch) -> None:
    result = _runner.invoke(
        app, ["bench", "--models", "fake/x", "--battery", "speed", "--quick", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "speed" in result.output and "reasoning" not in result.output
