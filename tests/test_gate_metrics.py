"""Task 10 — gate + metric correctness: seat-aware gate display, >=2-rep
unanimity, per-request truncation cap.

Three regression fixtures, one per fix:

  (a) ``_verdict_gates`` must pass ``seat_code`` into ``evaluate_gate`` so
      TIER-1 per-role hard vetoes surface in the displayed gate status
      (matching the ranker, which already passes seat_code).
  (b) ``_self_consistency`` unanimity counts ONLY items with >= 2 reps — a
      single-rep item carries no consistency information and must not count
      as "unanimous" (previously every single-rep item counted, inflating the
      metric to a vacuous 100% and letting the strict gate pass).
  (c) truncation is judged against the ACTUAL ``requested_max_output``
      recorded on the measurement row (openai-compat default is 8192), not
      the fixed 16000 proxy — the proxy either misses real 8192-cap
      truncations or misjudges rows that recorded their own cap.
"""

from __future__ import annotations

import pytest

from hr.cli import _verdict_gates
from hr.health import (
    HealthReport,
    _self_consistency,
    _truncation_rate_rows,
    compute_health,
)
from hr.seats.health_gates import evaluate_gate


# ---------------------------------------------------------------------------
# Shared fixture fakes (mirror tests/test_health.py, but the cursor's column
# count follows the row shape so a 5th ``requested_max_output`` column works).
# ---------------------------------------------------------------------------
class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.description = (
            [("item_id",), ("score",), ("tokens_out",), ("response_text",),
             ("requested_max_output",)][: len(rows[0])]
            if rows
            else []
        )

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return FakeCursor(self._rows)


def _looping_text() -> str:
    """Response text that scores loop_max >= 0.9 (see test_health.py)."""
    block = "The quick brown fox jumps over the lazy dog. " * 2
    return (block + "\n") * 6


# ---------------------------------------------------------------------------
# (a) TIER-1 hard vetoes appear in the displayed gate status
# ---------------------------------------------------------------------------
class TestVerdictGateDisplay:
    def test_clean_model_shows_pass(self):
        reports = {
            "m_clean": HealthReport(
                model_id="m_clean", sweep_id="s", n_measurements=2,
                loop_mean=0.01, loop_max=0.01, truncation_rate=0.0,
                consistency_unanimity_pct=1.0, answer_completion_rate=1.0,
            )
        }
        rows = _verdict_gates(reports)
        assert "m_clean: PASS" in "".join(cells for _, cells in rows)

    def test_looping_model_shows_tier1_hard_veto(self):
        # Same report builder path as production: compute_health → report.
        rows = [
            ("i1", 0.9, 100, _looping_text()),
            ("i2", 0.9, 100, _looping_text()),
        ]
        conn = FakeConn(rows)
        reports = {"m_loop": compute_health("m_loop", "s1", conn)}
        cells = "".join(c for _, c in _verdict_gates(reports))
        assert "m_loop: FAIL" in cells
        assert "hard veto" in cells


# ---------------------------------------------------------------------------
# (b) unanimity counts only items with >= 2 reps (no vacuous 100%)
# ---------------------------------------------------------------------------
class TestUnanimityReps:
    def test_single_rep_items_not_unanimous(self):
        rows = [
            {"item_id": "i1", "score": 0.9},
            {"item_id": "i2", "score": 0.8},
            {"item_id": "i3", "score": 0.7},
        ]
        mean_range, unanimity_pct = _self_consistency(rows)
        assert mean_range is None
        assert unanimity_pct is None

    def test_multi_rep_items_still_count(self):
        rows = [
            {"item_id": "i1", "score": 0.9},
            {"item_id": "i1", "score": 0.9},
            {"item_id": "i2", "score": 0.8},
            {"item_id": "i2", "score": 0.8},
        ]
        mean_range, unanimity_pct = _self_consistency(rows)
        assert mean_range == pytest.approx(0.0)
        assert unanimity_pct == pytest.approx(1.0)

    def test_mixed_single_and_multi_reps(self):
        # i1=i2 single-rep items must NOT dilute/enrich the unanimity
        # denominator: only the 2-rep item is judged.
        rows = [
            {"item_id": "single_a", "score": 0.9},
            {"item_id": "single_b", "score": 0.9},
            {"item_id": "two", "score": 0.3},
            {"item_id": "two", "score": 0.9},
        ]
        mean_range, unanimity_pct = _self_consistency(rows)
        assert mean_range == pytest.approx(0.6)
        assert unanimity_pct == pytest.approx(0.0)

    def test_strict_gate_not_vacuously_passed_on_single_reps(self):
        rows = [
            ("i1", 0.9, 100, "结论: 42."),
            ("i2", 0.8, 100, "The answer is 42."),
            ("i3", 0.7, 100, "42"),
        ]
        hr = compute_health("m", "s1", FakeConn(rows))
        assert hr.consistency_unanimity_pct is None
        passed, notes = evaluate_gate(hr, "strict", seat_code="oracle")
        assert any("consistency unanimity not measured" in n for n in notes)
        # The 100%-unanimity illusion is gone: the threshold check cannot
        # pass on a metric that was never measured.
        assert passed is True  # only other, honestly-measured checks ran


# ---------------------------------------------------------------------------
# (c) truncation judged against the measurement's own requested cap
# ---------------------------------------------------------------------------
class TestTruncationRequestedCap:
    def test_at_cap_flags_truncation(self):
        rows = [
            {"item_id": "i1", "score": 0.5, "tokens_out": 8192,
             "requested_max_output": 8192},
        ]
        assert _truncation_rate_rows(rows) == pytest.approx(1.0)

    def test_same_token_count_under_larger_cap_not_truncated(self):
        rows = [
            {"item_id": "i1", "score": 0.5, "tokens_out": 8192,
             "requested_max_output": 16384},
        ]
        assert _truncation_rate_rows(rows) == pytest.approx(0.0)

    def test_rows_without_recorded_cap_fall_back_to_proxy(self):
        rows = [
            {"item_id": "i1", "score": 0.5, "tokens_out": 17000},
        ]
        assert _truncation_rate_rows(rows) == pytest.approx(1.0)

    def test_compute_health_respects_per_row_caps(self):
        rows = [
            ("i1", 0.5, 8192, "The answer is 42.", 8192),
            ("i2", 0.5, 8192, "The answer is 42.", 16384),
            ("i3", 0.5, 1000, "The answer is 42.", None),
        ]
        hr = compute_health("m", "s1", FakeConn(rows))
        # i1 truncated (at its 8192 cap), i2 not (cap 16384), i3 not.
        assert hr.truncation_rate == pytest.approx(1 / 3)
        # completion: the truncated i1 response does not count as complete.
        assert hr.answer_completion_rate == pytest.approx(2 / 3)