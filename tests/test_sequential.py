"""Tests for sequential.py — SequentialStopper."""
from __future__ import annotations

import numpy as np
import pytest

from hr.stats.sequential import SequentialStopper, SequentialConfig


def _make_cfg(**overrides):
    base = dict(
        thresholds={"reasoning": 2.0, "hallucination": 2.0, "tool_a": 3.0, "vision": 3.0},
        n_initial=3,
        n_max=10,
    )
    base.update(overrides)
    return SequentialConfig(**base)


def test_stops_early_on_low_variance():
    """Low variance → tight CI quickly → stop at n_initial or soon after."""
    cfg = _make_cfg(thresholds={"low": 1.0})
    stopper = SequentialStopper(battery_code="low", config=cfg)
    rng = np.random.default_rng(0)
    stopped_at = None
    for rnd in range(1, 11):
        # Very low-variance scores around 0.9 ± 0.001
        stopper.add_round(rng.normal(0.9, 0.001, size=5))
        if stopper.should_stop(confidence=0.95, B=500):
            stopped_at = rnd
            break
    assert stopped_at is not None
    # Should stop at n_initial=3 since variance drops fast
    assert stopped_at <= cfg.n_max
    assert stopped_at == cfg.n_initial


def test_hits_n_max_on_high_variance():
    """High variance + tight threshold → never meets threshold, only hits n_max."""
    cfg = _make_cfg(thresholds={"high": 0.001})  # impossibly tight
    stopper = SequentialStopper(battery_code="high", config=cfg)
    rng = np.random.default_rng(1)
    stopped_at = None
    for rnd in range(1, 30):  # allow to go past n_max to verify stopper stops at cap
        stopper.add_round(rng.normal(0.5, 2.0, size=5))
        if stopper.should_stop(confidence=0.95, B=500):
            stopped_at = rnd
            break
    # Must hit n_max (or later due to threshold never met)
    assert stopped_at is not None
    assert stopped_at == cfg.n_max


def test_respects_pilot_phase():
    """During pilot (n < n_initial), should_stop is False regardless."""
    cfg = _make_cfg(thresholds={"x": 100.0})  # generous threshold
    stopper = SequentialStopper(battery_code="x", config=cfg)
    stopper.add_round([0.5])
    assert stopper.should_stop() is False  # n_rounds=1 < n_initial=3
    stopper.add_round([0.5])
    assert stopper.should_stop() is False  # n_rounds=2 < 3
    stopper.add_round([0.5])
    # n_rounds=3 == n_initial, threshold very lax → should stop
    assert stopper.should_stop() is True


def test_missing_threshold_never_stops():
    """Battery without configured threshold → always False."""
    cfg = _make_cfg(thresholds={"known": 0.5})
    stopper = SequentialStopper(battery_code="unknown_battery", config=cfg)
    for _ in range(20):
        stopper.add_round([0.5])
    assert stopper.should_stop() is False


def test_status_fields():
    cfg = _make_cfg(thresholds={"s": 0.1})
    stopper = SequentialStopper(battery_code="s", config=cfg)
    stopper.add_round([0.1, 0.2, 0.3])
    status = stopper.status()
    assert status["battery_code"] == "s"
    assert status["n_rounds"] == 1
    assert status["n_scores"] == 3
    assert "half_width" in status
    assert status["threshold"] == 0.1
    assert "should_stop" in status


def test_from_yaml_accepts_full_battery_set(tmp_path):
    """Stage-0 set (incl. tool_b) present in half_width → loads fine."""
    p = tmp_path / "thresholds.yaml"
    p.write_text(
        "half_width:\n"
        "  reasoning: 2.0\n"
        "  hallucination: 2.0\n"
        "  tool_a: 3.0\n"
        "  tool_b: 5.0\n"
        "  vision: 3.0\n"
        "n_initial: 3\n"
        "n_max: 10\n",
        encoding="utf-8",
    )
    cfg = SequentialConfig.from_yaml(
        str(p), required_batteries=["reasoning", "hallucination", "tool_a", "tool_b", "vision"]
    )
    assert cfg.thresholds["tool_b"] == 5.0


def test_from_yaml_missing_battery_raises(tmp_path):
    """tool_b removed from the temp config → explicit validation error."""
    p = tmp_path / "thresholds.yaml"
    p.write_text(
        "half_width:\n"
        "  reasoning: 2.0\n"
        "  hallucination: 2.0\n"
        "  tool_a: 3.0\n"
        "  vision: 3.0\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="tool_b"):
        SequentialConfig.from_yaml(
            str(p), required_batteries=["reasoning", "hallucination", "tool_a", "tool_b", "vision"]
        )
