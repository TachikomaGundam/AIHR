"""Tests for health gate levels, thresholds, and ranking penalties."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hr.health import HealthReport
from hr.seats.health_gates import (
    GATES,
    ROLE_HARD_VETOS,
    ROLE_HEALTH_WEIGHTS,
    SEAT_HEALTH_GATE,
    HealthThresholds,
    evaluate_gate,
    health_rank_score,
)
from hr.seats.rolespec import SEAT_CODES


def _report(**kwargs) -> HealthReport:
    base = dict(
        model_id="m",
        sweep_id="s",
        n_measurements=10,
        loop_mean=0.01,
        loop_max=0.01,
        truncation_rate=0.01,
        consistency_unanimity_pct=0.99,
        answer_completion_rate=1.0,
    )
    base.update(kwargs)
    return HealthReport(**base)


class TestThresholds:
    def test_strict_level_values(self):
        t = GATES["strict"]
        assert t.loop_mean_max == pytest.approx(0.10)
        assert t.truncation_max == pytest.approx(0.05)
        assert t.unanimity_min == pytest.approx(0.90)
        assert t.completion_min == pytest.approx(0.80)

    def test_moderate_level_values(self):
        t = GATES["moderate"]
        assert t.loop_mean_max == pytest.approx(0.15)
        assert t.truncation_max == pytest.approx(0.08)
        assert t.unanimity_min is None
        assert t.completion_min == pytest.approx(0.70)

    def test_lenient_level_values(self):
        t = GATES["lenient"]
        assert t.loop_mean_max == pytest.approx(0.20)
        assert t.truncation_max == pytest.approx(0.10)
        assert t.unanimity_min is None
        assert t.completion_min is None

    def test_all_fields_optional(self):
        assert HealthThresholds() == HealthThresholds(
            loop_mean_max=None, truncation_max=None,
            unanimity_min=None, completion_min=None,
        )

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            HealthThresholds(loop_mean_max=0.1, typo_threshold=0.2)

    def test_old_loop_max_field_name_rejected(self):
        # Regression: the threshold is loop_mean_max; the old loop_max name
        # must be rejected by extra="forbid" rather than silently ignored.
        with pytest.raises(ValidationError):
            HealthThresholds(loop_max=0.1)


class TestEvaluateGate:
    def test_clean_report_passes_all_levels(self):
        for level in ("strict", "moderate", "lenient"):
            passed, notes = evaluate_gate(_report(), level)
            assert passed is True
            assert notes == []

    def test_strict_enforces_loop_mean_and_truncation(self):
        passed, notes = evaluate_gate(
            _report(loop_mean=0.30, truncation_rate=0.10), "strict"
        )
        assert passed is False
        assert any("loop repetition" in n for n in notes)
        assert any("truncation rate" in n for n in notes)

    def test_strict_enforces_unanimity(self):
        passed, notes = evaluate_gate(
            _report(consistency_unanimity_pct=0.50), "strict"
        )
        assert passed is False
        assert any("consistency unanimity" in n for n in notes)

    def test_moderate_allows_what_strict_rejects(self):
        report = _report(loop_mean=0.12, truncation_rate=0.07)
        strict_passed, _ = evaluate_gate(report, "strict")
        moderate_passed, moderate_notes = evaluate_gate(report, "moderate")
        assert strict_passed is False
        assert moderate_passed is True
        assert moderate_notes == []

    def test_lenient_accepts_moderate_violations(self):
        report = _report(loop_mean=0.08, truncation_rate=0.055)
        passed, notes = evaluate_gate(report, "lenient")
        assert passed is True
        assert notes == []

    def test_one_bad_response_does_not_fail_gate(self):
        # loop_mean=0.02 (healthy pool) with a single degenerate response
        # (loop_max=1.0) must PASS strict — loop_max never gates; it is only
        # surfaced as an informational note.
        report = _report(loop_mean=0.02, loop_max=1.0)
        passed, notes = evaluate_gate(report, "strict")
        assert passed is True
        assert not any("exceeds" in n for n in notes)
        assert any("informational" in n and "worst-response" in n for n in notes)

    def test_loop_mean_012_fails_strict_passes_moderate(self):
        # With the relaxed strict threshold (0.10), loop_mean=0.08 now
        # passes strict — use 0.12 to exercise strict-fail + moderate-pass.
        report = _report(loop_mean=0.12, loop_max=1.0, truncation_rate=0.01)
        strict_passed, strict_notes = evaluate_gate(report, "strict")
        assert strict_passed is False
        assert any("loop repetition (mean) 0.120 exceeds 0.100" in n for n in strict_notes)
        moderate_passed, moderate_notes = evaluate_gate(report, "moderate")
        assert moderate_passed is True
        assert not any("exceeds" in n for n in moderate_notes)

    def test_none_metric_never_fails_with_note(self):
        report = _report(loop_mean=None, truncation_rate=None)
        passed, notes = evaluate_gate(report, "strict")
        assert passed is True
        assert any("loop repetition (mean) not measured" in n for n in notes)
        assert any("truncation rate not measured" in n for n in notes)

    def test_unenforced_unanimity_ignored_even_if_low(self):
        # unanimity is not enforced at moderate/lenient: a low measured value
        # must not fail those levels.
        report = _report(consistency_unanimity_pct=0.10)
        passed, _ = evaluate_gate(report, "moderate")
        assert passed is True

    def test_measured_violation_beats_missing_other_metric(self):
        report = _report(loop_mean=0.90, truncation_rate=None)
        passed, notes = evaluate_gate(report, "strict")
        assert passed is False
        assert any("exceeds" in n for n in notes)


class TestSeatGateCoverage:
    def test_every_seat_code_has_a_level(self):
        assert set(SEAT_HEALTH_GATE) == set(SEAT_CODES)

    def test_levels_are_valid(self):
        for seat, level in SEAT_HEALTH_GATE.items():
            assert level in ("strict", "moderate", "lenient")
            assert seat in SEAT_CODES

    def test_expected_levels(self):
        assert SEAT_HEALTH_GATE["oracle"] == "strict"
        assert SEAT_HEALTH_GATE["writing"] == "strict"
        assert SEAT_HEALTH_GATE["librarian"] == "strict"
        assert SEAT_HEALTH_GATE["explore"] == "lenient"
        assert SEAT_HEALTH_GATE["quick"] == "lenient"
        assert SEAT_HEALTH_GATE["hephaestus"] == "moderate"
        assert SEAT_HEALTH_GATE["sisyphus_junior"] == "moderate"


class TestHealthRankScore:
    def test_lower_is_healthier(self):
        healthy = _report(loop_max=0.01, truncation_rate=0.01)
        unhealthy = _report(loop_max=0.30, truncation_rate=0.20)
        assert health_rank_score(healthy) < health_rank_score(unhealthy)

    def test_additive_of_loop_mean_and_truncation(self):
        report = _report()
        report.loop_mean = 0.10
        report.truncation_rate = 0.05
        assert health_rank_score(report) == pytest.approx(0.15)

    def test_none_metrics_default_to_zero(self):
        report = _report(loop_mean=None, loop_max=None, truncation_rate=None)
        assert health_rank_score(report) == pytest.approx(0.0)

    def test_ordering_between_reports(self):
        a = _report()
        a.loop_mean = 0.05
        a.truncation_rate = 0.05
        b = _report()
        b.loop_mean = 0.0
        b.truncation_rate = 0.02
        assert health_rank_score(b) < health_rank_score(a)


class TestCompletionGate:
    def test_strict_enforces_completion_min(self):
        report = _report(answer_completion_rate=0.75)
        passed, notes = evaluate_gate(report, "strict")
        assert passed is False
        assert any("answer completion" in n for n in notes)

    def test_completion_between_levels(self):
        report = _report(answer_completion_rate=0.75)
        strict_passed, _ = evaluate_gate(report, "strict")
        moderate_passed, moderate_notes = evaluate_gate(report, "moderate")
        assert strict_passed is False
        assert moderate_passed is True
        assert moderate_notes == []

    def test_none_completion_never_fails(self):
        report = _report(answer_completion_rate=None)
        passed, notes = evaluate_gate(report, "strict")
        assert passed is True
        assert any("answer completion not measured" in n for n in notes)


class TestSeatAwareScoring:
    """TIER 1 seat hard vetoes + TIER 3 seat-weighted rank scoring."""

    def test_weights_cover_all_seat_codes(self):
        assert set(ROLE_HEALTH_WEIGHTS) == set(SEAT_CODES)
        for seat, weights in ROLE_HEALTH_WEIGHTS.items():
            assert set(weights) == {"loop", "truncation", "efficiency", "completion"}

    def test_hard_vetos_cover_all_seat_codes(self):
        assert set(ROLE_HARD_VETOS) == set(SEAT_CODES)

    def test_hard_veto_loop_max_fails_even_when_level_gate_passes(self):
        # deep (moderate level): level gate holds loop_mean=0.02 <= 0.15,
        # but the per-role loop_max veto (0.15) must eliminate the model on
        # the worst response alone — TIER 1 overrides TIER 2.
        report = _report(loop_mean=0.02, loop_max=0.30, truncation_rate=0.01)
        passed, notes = evaluate_gate(report, "moderate", seat_code="deep")
        assert passed is False
        assert any("hard veto" in n and "loop repetition" in n for n in notes)

    def test_hard_veto_truncation(self):
        # librarian vetoes truncation > 0.20: 0.25 must fail with a veto note.
        report = _report(loop_mean=0.02, loop_max=0.03, truncation_rate=0.25)
        passed, notes = evaluate_gate(report, "strict", seat_code="librarian")
        assert passed is False
        assert any("hard veto" in n and "truncation rate" in n for n in notes)

    def test_no_veto_means_clean_report_passes_with_seat_code(self):
        passed, notes = evaluate_gate(_report(), "strict", seat_code="oracle")
        assert passed is True
        assert notes == []

    def test_unknown_seat_code_gate_falls_back_to_level(self):
        report = _report(loop_mean=0.30)
        passed_none, notes_none = evaluate_gate(report, "strict")
        passed_unknown, notes_unknown = evaluate_gate(
            report, "strict", seat_code="no_such_seat"
        )
        assert passed_none == passed_unknown
        assert notes_none == notes_unknown

    def test_gate_without_seat_code_is_backward_compat(self):
        report = _report(loop_mean=0.30)
        passed, notes = evaluate_gate(report, "strict")
        assert passed is False
        assert any("loop repetition (mean) 0.300 exceeds 0.100" in n for n in notes)

    def test_weighted_score_differs_from_base(self):
        report = _report(
            loop_mean=0.10,
            truncation_rate=0.05,
            token_efficiency=2000.0,
            answer_completion_rate=0.90,
        )
        assert health_rank_score(report, seat_code="explore") != pytest.approx(
            health_rank_score(report)
        )

    def test_weighted_score_calculation(self):
        # sisyphus_junior weights: loop 0.20 / truncation 0.25 /
        # efficiency 0.15 / completion 0.15.
        report = _report(
            loop_mean=0.10,
            truncation_rate=0.04,
            token_efficiency=2000.0,
            answer_completion_rate=0.90,
        )
        expected = (
            0.20 * 0.10
            + 0.25 * 0.04
            + 0.15 * (2000.0 / 1000.0 - 1.0)
            + 0.15 * (1.0 - 0.90)
        )
        assert health_rank_score(
            report, seat_code="sisyphus_junior"
        ) == pytest.approx(expected)

    def test_efficiency_below_baseline_not_penalized(self):
        # token_efficiency <= 1000 contributes zero: max(0, ...) floors it.
        report = _report(loop_mean=0.0, truncation_rate=0.0, token_efficiency=500.0)
        assert health_rank_score(report, seat_code="explore") == pytest.approx(0.0)

    def test_score_without_seat_is_loop_plus_truncation(self):
        report = _report(
            loop_mean=0.10,
            truncation_rate=0.05,
            token_efficiency=5000.0,
            answer_completion_rate=0.50,
        )
        assert health_rank_score(report) == pytest.approx(0.15)

    def test_unknown_seat_score_falls_back_to_base(self):
        report = _report(loop_mean=0.10, truncation_rate=0.05, token_efficiency=5000.0)
        assert health_rank_score(report, seat_code="ad_hoc") == pytest.approx(
            health_rank_score(report)
        )
