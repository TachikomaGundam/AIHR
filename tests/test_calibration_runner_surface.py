"""Calibration runner contract tests (committed shape).

Exercises hr.calibration_runner end to end with fakes: per-item calls
with infra-failure classification, routing and grading, the acceptance-
band verdict fold, the token-cap stop, and the pure verdict evaluator.
db=None so the persistence mixin is a no-op; fully offline.
"""

from __future__ import annotations

import pytest

import hr.calibration_runner as cr
from hr.calibration_models import CalibrationReport, Measurement
from hr.graders.base import GradeResult, ModelResponse
from hr.items.schema import ItemType, build_envelope
from hr.stage0_selection import EST_TOKENS_PER_CALL


def make_env(item_key: str, type_: ItemType = ItemType.REASONING, tier: int = 1) -> object:
    return build_envelope(
        item_key=item_key,
        type=type_,
        payload={"question": "q"},
        grading={"grader": "constraint@1.0", "params": {"checks": []}},
        meta={"seats": ["f1"]},
        tier=tier,
    )


class _FakeAdapter:
    def __init__(self, *, raise_chat: Exception | None = None, thinking: bool = True):
        self.raise_chat = raise_chat
        self.thinking = thinking
        self.chat_calls: list[dict] = []

    def probe_capabilities(self, model_id: str):
        return type("Cap", (), {"supports_thinking": self.thinking})()

    def chat(self, model_id, messages, **kwargs) -> ModelResponse:
        if self.raise_chat is not None:
            raise self.raise_chat
        self.chat_calls.append(kwargs)
        return ModelResponse(text="ok", tokens_in=10, tokens_out=20, latency_ms=5)


class _FakeGrader:
    def __init__(self, result: GradeResult | None = None, *, raise_grade: Exception | None = None):
        self.result = result or GradeResult(score=0.95, passed=True, detail={})
        self.raise_grade = raise_grade

    def grade(self, payload, params, response):  # noqa: ARG002
        if self.raise_grade is not None:
            raise self.raise_grade
        return self.result


class _FakeRegistry:
    def __init__(self, grader: _FakeGrader, *, raise_get: Exception | None = None):
        self.grader = grader
        self.raise_get = raise_get

    def get(self, spec: str):  # noqa: ARG002
        if self.raise_get is not None:
            raise self.raise_get
        return self.grader


@pytest.fixture(autouse=True)
def _patch_deps(monkeypatch):
    monkeypatch.setattr(cr, "load_anchors", lambda: {"cheap": "anchor-a"})
    monkeypatch.setattr(
        cr,
        "load_item_repo",
        lambda repo, batteries: {"reasoning": [make_env("reasoning.001", tier=1)]},
    )


def _runner(adapter=None, **kwargs) -> cr.CalibrationRunner:  # noqa: ANN001
    adapter = adapter or _FakeAdapter()
    return cr.CalibrationRunner(
        adapter=adapter,
        item_repo="ignored",
        registry=_FakeRegistry(_FakeGrader()),
        anchors={"cheap": "anchor-a"},
        batteries=["reasoning"],
        db=None,
        **kwargs,
    )


def test_init_defaults_resolve(monkeypatch) -> None:
    runner = cr.CalibrationRunner(adapter=_FakeAdapter(), item_repo="r", anchors={"cheap": "a"})
    assert runner.registry is not None
    assert runner.anchors == {"cheap": "a"}
    assert runner.token_cap == cr.TOKEN_CAP
    assert runner.resume is False
    assert runner._recorded_pairs == set()
    assert runner._recorded_measurements == []


def test_run_happy_path() -> None:
    runner = _runner()
    report = runner.run()
    assert isinstance(report, CalibrationReport)
    assert len(report.measurements) == 1
    assert report.measurements[0].score == 0.95
    assert report.measurements[0].tokens_in == 10
    assert report.verdicts[0].status == "pass"
    assert report.verdicts[0].passed is True
    assert report.stopped_at_cap is False
    (call,) = runner.adapter.chat_calls
    assert call["thinking_budget"] == 8192


def test_run_infra_failure_records_zero(monkeypatch) -> None:
    monkeypatch.setattr(cr, "EST_TOKENS_PER_CALL", EST_TOKENS_PER_CALL)
    runner = _runner(adapter=_FakeAdapter(raise_chat=ValueError("boom")))
    report = runner.run()
    (m,) = report.measurements
    assert m.score == 0.0 and m.passed is False
    assert "ValueError: boom" in m.infra_failure
    assert m.tokens_in == 0
    assert report.total_tokens_in == 0
    # Infra-failed tier runs are inconclusive, never a silent pass.
    assert report.verdicts[0].status == "inconclusive"


def test_run_grade_failure_records_zero() -> None:
    runner = _runner(
        adapter=_FakeAdapter(),
    )
    runner.registry = _FakeRegistry(_FakeGrader(raise_grade=KeyError("x")))
    report = runner.run()
    (m,) = report.measurements
    assert m.score == 0.0
    assert "grader_error" in m.detail

def test_run_stops_at_token_cap() -> None:
    runner = _runner(token_cap=0)
    report = runner.run()
    assert report.stopped_at_cap is True
    assert report.measurements == []


def test_call_returns_error_meta_on_exception() -> None:
    runner = _runner(adapter=_FakeAdapter(raise_chat=TimeoutError("late")))
    env = make_env("reasoning.2")
    resp, meta = runner._call("anchor-a", env)
    assert resp is None
    assert meta["ok"] is False
    assert "TimeoutError" in meta["error"]


def test_grade_no_routing(monkeypatch) -> None:
    monkeypatch.setattr(cr, "_ROUTING", {})
    runner = _runner()
    gr = runner._grade(make_env("reasoning.3"), ModelResponse(text="x"))
    assert gr.score == 0.0
    assert gr.detail == {"no_routing": True}


def test_recorded_pair_bookkeeping() -> None:
    runner = _runner()
    assert runner._recorded("cheap", "k1") is False
    runner._recorded_pairs.add(("cheap", "k1"))
    assert runner._recorded("cheap", "k1") is True


def test_evaluate_fold_statuses() -> None:
    tier1_items = [make_env(f"r.{i:02d}", tier=1) for i in range(10)]
    tier6_items = [make_env(f"r.{i:02d}", tier=6) for i in range(10, 20)]
    items = {"reasoning": [*tier1_items, *tier6_items]}

    def meas(item: str, tier: int, passed: bool) -> Measurement:
        return Measurement(
            anchor="cheap", item_key=item, battery="reasoning", tier=tier,
            item_type="reasoning", score=1.0 if passed else 0.0,
            passed=passed, latency_ms=0, tokens_in=0, tokens_out=0,
        )

    passing = [meas(f"r.{i:02d}", 1, i < 9) for i in range(10)]
    passing += [meas(f"r.{i:02d}", 6, i == 11) for i in range(10, 20)]
    runner = _runner()
    (verdict,) = runner._evaluate(passing, items)
    assert verdict.status == "pass"
    assert verdict.passed is True

    failing = [meas(f"r.{i:02d}", 1, i < 2) for i in range(10)]
    failing += [meas(f"r.{i:02d}", 6, i == 11) for i in range(10, 20)]
    (verdict2,) = runner._evaluate(failing, items)
    assert verdict2.status == "fail"
    assert verdict2.failing_tiers[0].tier == 1


def test_evaluate_invalid_when_empty() -> None:
    runner = _runner()
    (verdict,) = runner._evaluate([], {"reasoning": []})
    assert verdict.status == "invalid"