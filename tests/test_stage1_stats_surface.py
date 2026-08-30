"""Stage-1 statistics contract tests (committed surface).

Covers hr.stage1_stats: aligned 2-D array building for paired
comparisons, and the per-battery pair-decision matrix over the
anytime-valid sequences. Offline and deterministic.
"""

from __future__ import annotations

import numpy as np
import pytest

from hr.stage1_stats import _bootstrap_separation_from_stage1, build_aligned_2d


def _decision(ma: str, mb: str, *, status: str, winner: str | None, alpha: float = 0.05):
    return {
        "model_a": ma,
        "model_b": mb,
        "status": status,
        "winner": winner,
        "effect": 0.1,
        "alpha": alpha,
    }


class _FakeSeq:
    def __init__(self, maker):
        self._maker = maker

    def decide(self, *, model_a: str, model_b: str):
        data = self._maker(model_a, model_b)
        return type("FakeDecision", (), {
            "status": data["status"],
            "winner": data["winner"],
            "alpha": data["alpha"],
            "to_dict": lambda *args: dict(data),
        })()


class _FakeState:
    def __init__(self, pair_stoppers: dict):
        self.pair_stoppers = pair_stoppers


def test_build_aligned_2d_pads_with_nan() -> None:
    arr, keys = build_aligned_2d({"i2": [1.0, 2.0], "i1": [3.0]})
    assert keys == ["i1", "i2"]
    assert arr.shape == (2, 2)
    assert arr[0, 0] == 3.0
    assert np.isnan(arr[0, 1])
    assert arr[1, 0] == 1.0 and arr[1, 1] == 2.0


def test_build_aligned_2d_empty_and_zero_reps() -> None:
    arr, keys = build_aligned_2d({})
    assert arr.shape == (0, 0) and keys == []
    arr2, keys2 = build_aligned_2d({"i1": [], "i2": []})
    assert arr2.shape == (2, 0)
    assert keys2 == ["i1", "i2"]


def test_bootstrap_separation_decided_winner_orientation() -> None:
    def decided_b_wins(ma: str, mb: str) -> dict:
        return _decision(ma, mb, status="decided", winner=mb)

    def decided_a_wins(ma: str, mb: str) -> dict:
        return _decision(ma, mb, status="decided", winner=ma)

    state = _FakeState(
        {
            "a|b|reasoning": _FakeSeq(decided_b_wins),
            "c|d|tool_a": _FakeSeq(decided_a_wins),
            "e|f|vision": _FakeSeq(lambda ma, mb: _decision(ma, mb, status="unresolvable", winner=None)),
            "badenough": _FakeSeq(decided_a_wins),
        }
    )
    result = _bootstrap_separation_from_stage1(state)
    assert set(result.keys()) == {"reasoning", "tool_a", "vision"}
    (ab,) = result["reasoning"]
    assert ab["model_a"] == "b" and ab["model_b"] == "a"
    assert ab["p_separated"] == pytest.approx(0.95)
    assert ab["p_weak"] == pytest.approx(0.05)
    assert ab["p_tie"] == 0.0
    (cd,) = result["tool_a"]
    assert cd["model_a"] == "c" and cd["model_b"] == "d"
    assert cd["p_separated"] == pytest.approx(0.95)
    (ef,) = result["vision"]
    assert ef["p_separated"] == 0.0 and ef["p_weak"] == 0.0 and ef["p_tie"] == 1.0
    assert result["reasoning"][0]["status"] == "decided"


def test_bootstrap_separation_sorts_rows() -> None:
    state = _FakeState(
        {
            "z|a|reasoning": _FakeSeq(lambda ma, mb: _decision(ma, mb, status="decided", winner=ma)),
            "a|b|reasoning": _FakeSeq(lambda ma, mb: _decision(ma, mb, status="decided", winner=ma)),
        }
    )
    result = _bootstrap_separation_from_stage1(state)
    keys = [(r["model_a"], r["model_b"]) for r in result["reasoning"]]
    assert keys == sorted(keys)