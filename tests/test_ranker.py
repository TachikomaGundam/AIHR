"""Tests for ranker.py — hard gates + separation-driven primary."""
from __future__ import annotations

import numpy as np
import pytest

from hr.assign import ranker
from hr.assign.ranker import CandidateModel, rank
from hr.health import HealthReport


def _candidate(model_id, provider="p", caps=None, scores=None, cost=1.0):
    return CandidateModel(
        model_id=model_id,
        provider_id=provider,
        capabilities=caps or {},
        ctx_p95_tokens=0,
        scores=scores or {"b1": np.array([0.5])},
        cost_per_task=cost,
    )


def test_vision_hard_gate_rejects_non_vision_models():
    candidates = [
        _candidate("m_no_vision", caps={}),
        _candidate("m_with_vision", caps={"vision": True},
                   scores={"b1": np.array([0.9, 0.85, 0.88])}),
    ]
    seat = {"seat_code": "vision_task", "required_capabilities": ["vision"], "ctx_p95": None}
    res = rank(candidates, seat, battery_weights={"b1": 1.0})
    assert res.primary == "m_with_vision"
    assert any("m_no_vision" == mid and "vision" in reason for mid, reason in res.eliminated)


def test_context_window_gate_for_hephaestus():
    candidates = [
        _candidate("short_ctx", caps={"context_window": 32000}),
        _candidate("long_ctx", caps={"context_window": 256000},
                   scores={"b1": np.array([0.7, 0.75])}),
    ]
    seat = {"seat_code": "hephaestus", "required_capabilities": [], "ctx_p95": 200000}
    res = rank(candidates, seat, battery_weights={"b1": 1.0})
    assert res.primary == "long_ctx"
    assert any(mid == "short_ctx" for mid, _ in res.eliminated)


def test_all_eliminated_raises():
    candidates = [
        _candidate("m1", caps={}),
        _candidate("m2", caps={}),
    ]
    seat = {"seat_code": "v", "required_capabilities": ["vision"], "ctx_p95": None}
    with pytest.raises(ValueError):
        rank(candidates, seat, battery_weights={"b1": 1.0})


def test_separated_primary_picks_top():
    candidates = [
        _candidate("strong", caps={}, scores={"b1": np.array([0.9, 0.92, 0.88])}),
        _candidate("weak", caps={}, scores={"b1": np.array([0.3, 0.28, 0.32])}),
    ]
    seat = {"seat_code": "s", "required_capabilities": [], "ctx_p95": None}
    sep = {("strong", "weak"): 0.98}
    res = rank(candidates, seat, battery_weights={"b1": 1.0}, separation_pairs=sep)
    assert res.primary == "strong"
    assert res.fallbacks[0][0] == "weak"
    assert res.fallbacks[0][1] == "separated"


def test_tie_breaks_to_lower_cost():
    candidates = [
        _candidate("expensive", caps={}, scores={"b1": np.array([0.5, 0.5])}, cost=10.0),
        _candidate("cheap", caps={}, scores={"b1": np.array([0.5, 0.5])}, cost=1.0),
    ]
    seat = {"seat_code": "s", "required_capabilities": [], "ctx_p95": None}
    # p=0.5 → tie (not separated)
    sep = {("expensive", "cheap"): 0.5}
    res = rank(candidates, seat, battery_weights={"b1": 1.0}, separation_pairs=sep)
    assert res.primary == "cheap"


def test_no_separation_data_picks_top_by_score():
    candidates = [
        _candidate("a", scores={"b1": np.array([0.8])}),
        _candidate("b", scores={"b1": np.array([0.5])}),
        _candidate("c", scores={"b1": np.array([0.2])}),
    ]
    seat = {"seat_code": "s", "required_capabilities": [], "ctx_p95": None}
    res = rank(candidates, seat, battery_weights={"b1": 1.0})
    assert res.primary == "a"
    assert len(res.fallbacks) == 2


def test_up_to_three_fallbacks():
    candidates = [
        _candidate(f"m{i}", scores={"b1": np.array([1.0 - i * 0.1])}) for i in range(6)
    ]
    seat = {"seat_code": "s", "required_capabilities": [], "ctx_p95": None}
    res = rank(candidates, seat, battery_weights={"b1": 1.0})
    assert res.primary == "m0"
    assert len(res.fallbacks) == 3
    assert [fb[0] for fb in res.fallbacks] == ["m1", "m2", "m3"]


# ---------------------------------------------------------------------------
# Behavioral-health gate + health tie-break (appended per health-gate spec)
# ---------------------------------------------------------------------------


def _health_report(model_id, loop_max=0.01, trunc=0.0, unanimity=1.0):
    return HealthReport(
        model_id=model_id,
        sweep_id="s",
        n_measurements=4,
        loop_mean=loop_max * 0.5,
        loop_max=loop_max,
        truncation_rate=trunc,
        token_efficiency=1000.0,
        consistency_mean_range=0.0,
        consistency_unanimity_pct=unanimity,
        answer_completion_rate=1.0,
    )


def _candidate_with_health(model_id, health, score=0.5, cost=1.0):
    return CandidateModel(
        model_id=model_id,
        provider_id="p",
        capabilities={},
        ctx_p95_tokens=0,
        scores={"b1": np.array([score])},
        cost_per_task=cost,
        health=health,
    )


class TestHealthGateInRanker:
    def test_health_gate_eliminates_strict_violator(self):
        candidates = [
            _candidate_with_health("loopy", _health_report("loopy", loop_max=0.5), score=0.9),
            _candidate_with_health("clean", _health_report("clean", loop_max=0.01), score=0.8),
        ]
        seat = {"seat_code": "oracle", "required_capabilities": [], "ctx_p95": None}
        res = rank(candidates, seat, battery_weights={"b1": 1.0}, gate_level="strict")
        assert res.primary == "clean"
        (mid, reason) = res.eliminated[0]
        assert mid == "loopy"
        assert reason.startswith("health_gate:")

    def test_moderate_gate_passes_strict_violator(self):
        candidates = [
            _candidate_with_health("mid", _health_report("mid", loop_max=0.08), score=0.9),
            _candidate_with_health("clean", _health_report("clean", loop_max=0.01), score=0.8),
        ]
        seat = {"seat_code": "deep", "required_capabilities": [], "ctx_p95": None}
        res = rank(candidates, seat, battery_weights={"b1": 1.0}, gate_level="moderate")
        assert res.primary == "mid"
        assert res.eliminated == []

    def test_none_metrics_do_not_trigger_gate(self):
        # unanimity=None counts as "not measured": must not fail strict.
        hr = _health_report("unmeasured", loop_max=0.01, unanimity=None)
        candidates = [_candidate_with_health("unmeasured", hr, score=0.9)]
        seat = {"seat_code": "oracle", "required_capabilities": [], "ctx_p95": None}
        res = rank(candidates, seat, battery_weights={"b1": 1.0}, gate_level="strict")
        assert res.primary == "unmeasured"
        assert res.eliminated == []

    def test_all_eliminated_by_health_raises(self):
        candidates = [
            _candidate_with_health("loopy", _health_report("loopy", loop_max=0.9), score=0.9),
        ]
        seat = {"seat_code": "oracle", "required_capabilities": [], "ctx_p95": None}
        with pytest.raises(ValueError):
            rank(candidates, seat, battery_weights={"b1": 1.0}, gate_level="strict")

    def test_gate_level_none_keeps_loopy_candidate(self):
        candidates = [
            _candidate_with_health("loopy", _health_report("loopy", loop_max=0.9), score=0.9),
            _candidate_with_health("clean", _health_report("clean", loop_max=0.01), score=0.8),
        ]
        seat = {"seat_code": "oracle", "required_capabilities": [], "ctx_p95": None}
        res = rank(candidates, seat, battery_weights={"b1": 1.0})
        assert res.primary == "loopy"
        assert res.eliminated == []


class TestHealthTieBreak:
    def test_tied_healthier_wins(self):
        candidates = [
            _candidate_with_health("sick", _health_report("sick", loop_max=0.20, trunc=0.1), score=0.5, cost=1.0),
            _candidate_with_health("fit", _health_report("fit", loop_max=0.01, trunc=0.0), score=0.5, cost=10.0),
        ]
        seat = {"seat_code": "s", "required_capabilities": [], "ctx_p95": None}
        # p=0.5 → tie: health must beat the cost difference.
        sep = {("fit", "sick"): 0.5}
        res = rank(candidates, seat, battery_weights={"b1": 1.0}, separation_pairs=sep)
        assert res.primary == "fit"

    def test_tied_no_health_sorts_after_healthy(self):
        healthy = _candidate_with_health("healthy", _health_report("healthy", loop_max=0.01), score=0.5, cost=100.0)
        no_data = _candidate("nodata", caps={}, scores={"b1": np.array([0.5])}, cost=1.0)
        candidates = [healthy, no_data]
        seat = {"seat_code": "s", "required_capabilities": [], "ctx_p95": None}
        sep = {("healthy", "nodata"): 0.5}
        res = rank(candidates, seat, battery_weights={"b1": 1.0}, separation_pairs=sep)
        assert res.primary == "healthy"

    def test_tied_equal_health_falls_back_to_cost(self):
        candidates = [
            _candidate_with_health("fit_expensive", _health_report("fit_expensive"), score=0.5, cost=10.0),
            _candidate_with_health("fit_cheap", _health_report("fit_cheap"), score=0.5, cost=1.0),
        ]
        seat = {"seat_code": "s", "required_capabilities": [], "ctx_p95": None}
        sep = {("fit_expensive", "fit_cheap"): 0.5}
        res = rank(candidates, seat, battery_weights={"b1": 1.0}, separation_pairs=sep)
        assert res.primary == "fit_cheap"

    def test_separated_leader_not_displaced_by_healthier_runner_up(self):
        strong_sick = _candidate_with_health(
            "strong_sick", _health_report("strong_sick", loop_max=0.5), score=0.9
        )
        weak_fit = _candidate_with_health(
            "weak_fit", _health_report("weak_fit", loop_max=0.01), score=0.3
        )
        candidates = [strong_sick, weak_fit]
        seat = {"seat_code": "s", "required_capabilities": [], "ctx_p95": None}
        # p=0.98 → separated; the clear capability leader must stay primary
        # even though its health is far worse (gate_level=None: health is
        # tie-break only, never a displacement mechanism).
        sep = {("strong_sick", "weak_fit"): 0.98}
        res = rank(candidates, seat, battery_weights={"b1": 1.0}, separation_pairs=sep)
        assert res.primary == "strong_sick"
        assert res.fallbacks[0][0] == "weak_fit"

    def test_legacy_tie_behavior_without_health_data(self):
        # Candidates without health, no gate_level: exact pre-health behavior —
        # the tie is resolved by cost_per_task, exactly as before.
        candidates = [
            _candidate("pricey", caps={}, scores={"b1": np.array([0.5])}, cost=10.0),
            _candidate("cheap", caps={}, scores={"b1": np.array([0.5])}, cost=1.0),
        ]
        seat = {"seat_code": "s", "required_capabilities": [], "ctx_p95": None}
        sep = {("pricey", "cheap"): 0.5}
        res = rank(candidates, seat, battery_weights={"b1": 1.0}, separation_pairs=sep)
        assert res.primary == "cheap"

    def test_strict_gate_without_health_data_is_noop(self):
        # A strict gate_level with no health reports on any candidate must not
        # eliminate anyone — gate applies only when health is present.
        candidates = [
            _candidate("a", scores={"b1": np.array([0.8])}),
            _candidate("b", scores={"b1": np.array([0.5])}),
        ]
        seat = {"seat_code": "oracle", "required_capabilities": [], "ctx_p95": None}
        res = rank(candidates, seat, battery_weights={"b1": 1.0}, gate_level="strict")
        assert res.primary == "a"
        assert res.eliminated == []
