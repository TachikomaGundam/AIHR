"""Offline governance tests: reliability statistics + calibration gates (T4).

These tests pin the PURE statistical core of scorer governance — no DB, no
network:

* Krippendorff's ordinal alpha (two raters, midrank ordinal metric),
  cross-checked against an independent unit-level formulation of the same
  definition (Krippendorff derives the coincidence-matrix form FROM the
  unit-level form, so agreement between the two is a real cross-check);
* ICC(2,1) (McGraw & Wong 1996, two-way random, single measures, absolute
  agreement) on hand-computed examples;
* bootstrap reproducibility with the documented seed (np.random.default_rng
  with ``SEED = 42``, matching hr/stats/sequential.py) — the determinism test
  runs the bootstrap twice for real and compares exact values;
* the calibration-policy gates (0.80 floor / 0.667 / 0.799 / (200, 7d) drift
  window) as revisable machine-readable defaults;
* downstream consumption: low-agreement/inconclusive states are NEVER reduced
  to a plain score; blocked states refuse aggregation outright.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from hr.scorer_calibration import (
    CALIBRATION_POLICY,
    AgreementBlockedError,
    AggregationVerdict,
    RESAMPLES,
    SEED,
    aggregation_allowed,
    aggregation_gate,
    bootstrap_ci,
    classify_agreement,
    drift_check_due,
    guarded_aggregate,
    icc21,
    krippendorff_ordinal_alpha,
)
from hr.stats.sequential import normalize_bounded_score


# ---------------------------------------------------------------------------
# Independent oracle: UNIT-LEVEL Krippendorff ordinal alpha
#
# Krippendorff (2011) defines alpha = 1 - Do/De where
#   Do = (1/N) * sum over units of observed disagreement across coder pairs
#   De = (1/(n(n-1))) * sum over ALL ordered pairs of distinct judgments
# of the same difference function.  The production coincidence-matrix form is
# algebraically equivalent; this oracle re-derives alpha from the definition
# so a coincidence-matrix bug cannot hide.
# ---------------------------------------------------------------------------
def _unit_level_ordinal_alpha(pairs: list[tuple[float, float]]) -> float | None:
    if not pairs:
        return None
    a = np.asarray([p[0] for p in pairs], dtype=float)
    b = np.asarray([p[1] for p in pairs], dtype=float)
    pooled = np.concatenate([a, b])
    n = pooled.size
    if n < 2:
        return None
    # midrank of a value inside the sorted pool (ties share the midpoint)
    def midrank(value: float) -> float:
        below = np.sum(pooled < value)
        equal = np.sum(pooled == value)
        return below + (equal + 1) / 2.0

    def delta(x: float, y: float) -> float:
        return (midrank(x) - midrank(y)) ** 2

    do = sum(delta(ai, bi) for ai, bi in zip(a, b)) / len(pairs)
    de = 0.0
    for i in range(n):
        for j in range(n):
            if i != j:
                de += delta(float(pooled[i]), float(pooled[j]))
    de = de / (n * (n - 1))
    if de == 0.0:
        # zero expected disagreement -> indistinguishable judgments
        return 1.0
    return 1.0 - do / de


# ---------------------------------------------------------------------------
# Krippendorff ordinal alpha
# ---------------------------------------------------------------------------
def test_alpha_perfect_agreement_is_one() -> None:
    pairs = [(0, 0), (0, 0), (1, 1), (1, 1)]
    assert krippendorff_ordinal_alpha(pairs) == pytest.approx(1.0)


def test_alpha_all_same_value_is_one() -> None:
    # A single category is trivially in perfect agreement.
    assert krippendorff_ordinal_alpha([(0.8, 0.8), (0.8, 0.8)]) == pytest.approx(1.0)


def test_alpha_hand_computed_balanced_swap() -> None:
    # 2 units, values {0,1}: (0,1),(1,0).  o01=o10=2, n0=n1=2, n=4,
    # midranks r0=1.5, r1=3.5, w=4  ->  Do=4, De=8/3, alpha = -0.5.
    pairs = [(0, 1), (1, 0)]
    assert krippendorff_ordinal_alpha(pairs) == pytest.approx(-0.5)


def test_alpha_hand_computed_imbalanced_zero() -> None:
    # 3 units: (1,1),(1,1),(1,2).  counts n1=5, n2=1 -> midranks r1=3, r2=6,
    # w=9; Do=3, De=3 -> alpha == 0.0 exactly.
    pairs = [(1, 1), (1, 1), (1, 2)]
    assert krippendorff_ordinal_alpha(pairs) == pytest.approx(0.0)


def test_alpha_empty_or_single_observation_returns_none() -> None:
    assert krippendorff_ordinal_alpha([]) is None


def test_alpha_matches_unit_level_definition_on_random_data() -> None:
    rng = np.random.default_rng(7)
    for _ in range(5):
        a = rng.integers(1, 6, size=9).astype(float)
        b = rng.integers(1, 6, size=9).astype(float)
        pairs = list(zip(a.tolist(), b.tolist()))
        got = krippendorff_ordinal_alpha(pairs)
        if got is None:
            continue
        assert got == pytest.approx(_unit_level_ordinal_alpha(pairs))
    # a second shape: skewed values incl. ties
    a = [1.0, 1.0, 2.0, 2.0, 3.0, 4.0, 4.0]
    b = [2.0, 1.0, 3.0, 2.0, 4.0, 4.0, 5.0]
    pairs = list(zip(a, b))
    assert krippendorff_ordinal_alpha(pairs) == pytest.approx(
        _unit_level_ordinal_alpha(pairs)
    )


def test_alpha_ordinal_punishes_distance_more_than_nominal() -> None:
    # (0,1) and (0,4) both disagree but the ordinal metric weights the
    # 0->4 jump more: alpha must be LOWER than if the jump were adjacent.
    near = krippendorff_ordinal_alpha([(0, 1), (1, 0), (1, 1), (0, 0)])
    far = krippendorff_ordinal_alpha([(0, 4), (4, 0), (1, 1), (0, 0)])
    assert far < near


# ---------------------------------------------------------------------------
# ICC(2,1) — McGraw & Wong 1996
# ---------------------------------------------------------------------------
def test_icc21_perfect_agreement_is_one() -> None:
    assert icc21([(0.5, 0.5), (0.7, 0.7), (0.9, 0.9)]) == pytest.approx(1.0)


def test_icc21_no_variance_at_all_is_one() -> None:
    assert icc21([(0.8, 0.8), (0.8, 0.8)]) == pytest.approx(1.0)


def test_icc21_constant_coder_offset_is_two_thirds() -> None:
    # a=(0.5,0.6,0.7), b=(0.6,0.7,0.8): pure coder effect, zero residual.
    # SS_R=0.04, MS_R=0.02; SS_C=0.015, MS_C=0.015; SS_E=0.
    # ICC = 0.02/(0.02 + 0 + (2/3)*0.015) = 2/3 exactly.
    pairs = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8)]
    assert icc21(pairs) == pytest.approx(2.0 / 3.0)


def test_icc21_hand_computed_mid_band() -> None:
    # a=(0.5,0.6,0.7), b=(0.58,0.68,0.75): small residual + coder bias.
    # MS_R=0.01715, MS_C=0.00735, MS_E=0.00015 ->
    # ICC = 0.017/0.0221 = 170/221 ~ 0.76923  (inside the 0.667-0.799 band).
    pairs = [(0.5, 0.58), (0.6, 0.68), (0.7, 0.75)]
    assert icc21(pairs) == pytest.approx(170.0 / 221.0)


def test_icc21_requires_two_units() -> None:
    assert icc21([]) is None
    assert icc21([(0.5, 0.5)]) is None


def test_icc21_wild_disagreement_below_gate() -> None:
    pairs = [(0.5, 0.1), (0.5, 0.9), (0.5, 0.1), (0.5, 0.9), (0.5, 0.1)]
    value = icc21(pairs)
    assert value is not None and value < 0.667


# ---------------------------------------------------------------------------
# Score normalization: both scales must map into 0-1 for ICC aggregation
# ---------------------------------------------------------------------------
def test_normalize_bounded_score_handles_both_scales() -> None:
    # 0-100 bench scale
    assert normalize_bounded_score(92.5, max_score=100) == pytest.approx(0.925)
    assert normalize_bounded_score(0, max_score=100) == pytest.approx(0.0)
    assert normalize_bounded_score(130, max_score=100) == pytest.approx(1.0)
    # 0-1 scale is already bounded
    assert normalize_bounded_score(0.4, max_score=1) == pytest.approx(0.4)


def test_icc21_invariant_under_scale_conversion() -> None:
    # The SAME observations on a 0-100 scale must yield the same ICC as the
    # 0-1 normalization (the manager normalizes before aggregating).
    unit_scale = [(0.5, 0.58), (0.6, 0.68), (0.7, 0.75)]
    hundred_scale = [
        (normalize_bounded_score(a * 100, max_score=100),
         normalize_bounded_score(b * 100, max_score=100))
        for (a, b) in unit_scale
    ]
    assert icc21(hundred_scale) == pytest.approx(icc21(unit_scale))


# ---------------------------------------------------------------------------
# Bootstrap reproducibility (seed 42, >= 1000 resamples) — REAL double run
# ---------------------------------------------------------------------------
def test_bootstrap_interval_is_reproducible_with_seed_42() -> None:
    rng = np.random.default_rng(3)
    a = rng.uniform(0.3, 0.9, size=12)
    b = np.clip(a + rng.normal(0.0, 0.08, size=12), 0.1, 1.0)
    pairs = list(zip(a.tolist(), b.tolist()))

    lo1, hi1 = bootstrap_ci(icc21, pairs, resamples=RESAMPLES, seed=SEED)
    lo2, hi2 = bootstrap_ci(icc21, pairs, resamples=RESAMPLES, seed=SEED)

    # two REAL identical runs -> bit-identical intervals
    assert lo1 == lo2 and hi1 == hi2
    point = icc21(pairs)
    assert point is not None
    assert lo1 <= point <= hi1
    assert lo1 < hi1


def test_bootstrap_reproducible_for_ordinal_alpha() -> None:
    pairs = [(0, 0), (0, 1), (1, 1), (1, 0), (0, 0), (1, 1), (0, 1), (1, 0)]
    lo1, hi1 = bootstrap_ci(krippendorff_ordinal_alpha, pairs, seed=SEED)
    lo2, hi2 = bootstrap_ci(krippendorff_ordinal_alpha, pairs, seed=SEED)
    assert (lo1, hi1) == (lo2, hi2)
    assert lo1 <= krippendorff_ordinal_alpha(pairs) <= hi1


def test_bootstrap_uses_at_least_1000_resamples() -> None:
    from hr.scorer_calibration import _bootstrap_distribution

    pairs = [(0.4, 0.4), (0.6, 0.6), (0.8, 0.8), (0.5, 0.9)]
    dist = _bootstrap_distribution(icc21, pairs, resamples=RESAMPLES, seed=SEED)
    assert dist.shape == (RESAMPLES,)
    assert RESAMPLES >= 1000
    assert np.all(np.isfinite(dist))


def test_bootstrap_perfect_agreement_collapses_to_one() -> None:
    pairs = [(0.6, 0.6), (0.7, 0.7)]
    lo, hi = bootstrap_ci(icc21, pairs, resamples=1000, seed=SEED)
    assert (lo, hi) == (1.0, 1.0)


# ---------------------------------------------------------------------------
# Calibration-policy gates (revisable defaults, machine-readable)
# ---------------------------------------------------------------------------
def test_calibration_policy_constants_are_documented_defaults() -> None:
    assert CALIBRATION_POLICY["calibration_floor"] == 0.80
    assert CALIBRATION_POLICY["block_below"] == 0.667
    assert CALIBRATION_POLICY["low_ceiling"] == 0.799
    assert CALIBRATION_POLICY["resamples"] == 1000
    assert CALIBRATION_POLICY["seed"] == 42
    assert CALIBRATION_POLICY["drift_items_or_days"] == (200, 7)
    # everything exposed in the machine-readable policy must have a rationale
    assert "rationale" in CALIBRATION_POLICY


def test_classify_agreement_boundaries() -> None:
    assert classify_agreement(0.80) == "pass"
    assert classify_agreement(0.95) == "pass"
    assert classify_agreement(0.7999) == "low"
    assert classify_agreement(0.667) == "low"
    assert classify_agreement(0.6669) == "block"
    assert classify_agreement(None) == "block"


def test_aggregation_gates() -> None:
    assert aggregation_gate("pass") == "allowed"
    assert aggregation_gate("low") == "inconclusive"
    assert aggregation_gate("block") == "blocked"
    assert aggregation_allowed("pass") is True
    assert aggregation_allowed("low") is False
    assert aggregation_allowed("block") is False


def test_drift_check_due_whichever_comes_first() -> None:
    # 200 newly scored shared items trigger immediately (day 0)
    assert drift_check_due(200, 0.0) is True
    # >200 with 0 days elapsed: the ITEMS trigger wins
    assert drift_check_due(201, 0.0) is True
    # 7 days elapsed with zero items triggers too
    assert drift_check_due(0, 7.0) is True
    assert drift_check_due(5, 7.0) is True
    # never checked -> due (a drift check must eventually happen)
    assert drift_check_due(0, None) is True
    # neither threshold reached -> not due
    assert drift_check_due(199, 6.9) is False
    assert drift_check_due(0, 0.0) is False
    # boundary: exactly below both
    assert drift_check_due(199, 6.999) is False


# ---------------------------------------------------------------------------
# Downstream consumers: low-agreement/inconclusive handling is mandatory
# ---------------------------------------------------------------------------
def test_guarded_aggregate_passes_plain_score_when_allowed() -> None:
    verdict = guarded_aggregate(
        status="pass",
        statistic=0.95,
        interval=(0.91, 0.98),
        values=[0.8, 0.9, 0.7],
    )
    assert verdict.status == "allowed"
    assert verdict.value == pytest.approx(0.8)  # mean of the values
    assert verdict.interval == (0.91, 0.98)


def test_guarded_aggregate_low_agreement_never_returns_plain_score() -> None:
    # A low-agreement gate must surface as inconclusive, NOT as a score.
    verdict = guarded_aggregate(
        status="low", statistic=0.72, interval=(0.60, 0.83), values=[0.8, 0.9, 0.7]
    )
    assert verdict.status == "inconclusive"
    assert verdict.value is None
    assert verdict.interval == (0.60, 0.83)


def test_guarded_aggregate_blocked_raises() -> None:
    with pytest.raises(AgreementBlockedError):
        guarded_aggregate(
            status="block", statistic=0.4, interval=(0.2, 0.6), values=[0.8, 0.9]
        )


def test_guarded_aggregate_blocked_without_statistic() -> None:
    # zero shared observations -> no statistic, still a hard block
    with pytest.raises(AgreementBlockedError):
        guarded_aggregate(status="block", statistic=None, interval=None, values=[])


def test_aggregation_verdict_carries_policy() -> None:
    verdict = guarded_aggregate(status="pass", statistic=0.9, interval=(0.8, 0.95), values=[1.0])
    assert verdict.policy == CALIBRATION_POLICY