"""Scorer agreement calibration and drift detection.

This module provides tools for:
- Measuring agreement between different scorers (e.g., rule-based vs LLM judge)
- Calibrating scorers against reference answers
- Detecting drift in scorer behavior over time
- Providing evidence of grader reliability

Scorer reliability is critical for trustworthy evaluation results. This module
tracks agreement metrics and alerts when scorers diverge from expected behavior.

Agreement governance (T4):
- Categorical verdicts are compared with Krippendorff's ordinal alpha;
  continuous scores with ICC(2,1) (McGraw & Wong 1996, two-way random,
  single measures, absolute agreement). Both are bootstrapped (percentile
  CI, fixed seed) so reliability reports are reproducible.
- Aggregation is gated: below 0.667 it is hard-blocked, 0.667-0.799 is
  explicitly low-agreement/inconclusive (never a plain score), 0.80+ passes.
- Drift checks are due when 200 newly scored shared items OR 7 elapsed days
  accumulate — whichever comes first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence

import numpy as np

from hr.stats.sequential import normalize_bounded_score

# ---------------------------------------------------------------------------
# CALIBRATION POLICY — revisable defaults (rationale in CALIBRATION_POLICY)
#
# These constants gate two things: WHETHER two scorers agree enough for their
# measurements to be aggregated/downstreamed, and WHEN a scorer's agreement
# baseline must be re-checked for drift.
#
# * CALIBRATION_FLOOR (0.80): two scorers whose agreement statistic clears
#   0.80 are considered calibrated and their scores may be merged freely.
#   0.80 is the conventional inter-rater reliability floor (Landis & Koch
#   1977 call 0.81+ "almost perfect"; psychometric pipelines refuse to merge
#   raters below 0.80).
# * BLOCK_AGREEMENT_BELOW (0.667): below this, agreement sits at or below
#   chance and aggregation is HARD-BLOCKED. 0.667 is the widely used lower
#   bound of the "substantial" agreement band.
# * LOW_AGREEMENT_CEILING (0.799): the band [0.667, 0.799] still carries
#   signal, but merging must surface an explicit inconclusive/low-agreement
#   status — never a plain score.
# * RESAMPLES (1000) / SEED (42): bootstrap CI size and the fixed RNG seed
#   (matching hr/stats/sequential.py) so every reliability report is
#   bit-for-bit reproducible.
# * DRIFT_ITEMS_OR_DAYS (200, 7): a drift check is due when 200 NEW shared
#   items were scored (a large enough batch to trust the bootstrap) OR 7
#   days have passed since the last check — whichever occurs first; 7 days
#   matches the production sweep cadence.
#
# Treat these as POLICY: change them in ONE place with peer review of the
# rationale, never as per-call magic numbers.
# ---------------------------------------------------------------------------
CALIBRATION_FLOOR = 0.80
BLOCK_AGREEMENT_BELOW = 0.667
LOW_AGREEMENT_CEILING = 0.799
RESAMPLES = 1000
SEED = 42
DRIFT_ITEMS_OR_DAYS = (200, 7)

CALIBRATION_POLICY: dict[str, Any] = {
    "calibration_floor": CALIBRATION_FLOOR,
    "block_below": BLOCK_AGREEMENT_BELOW,
    "low_ceiling": LOW_AGREEMENT_CEILING,
    "resamples": RESAMPLES,
    "seed": SEED,
    "drift_items_or_days": DRIFT_ITEMS_OR_DAYS,
    "rationale": {
        "calibration_floor": "inter-rater floor: merge scores only above 0.80",
        "block_below": "below 0.667 agreement is at chance; hard-block aggregation",
        "low_ceiling": "0.667-0.799 merges only as explicitly inconclusive",
        "resamples": "1000 bootstrap resamples for stable percentile CIs",
        "seed": "fixed RNG seed 42 (matching hr/stats/sequential.py) for reproducible reports",
        "drift_items_or_days": "drift check due at 200 new shared items OR 7 days, whichever occurs first",
    },
}


# ---------------------------------------------------------------------------
# Reliability statistics (pure numpy — no external stats dependency)
# ---------------------------------------------------------------------------
def krippendorff_ordinal_alpha(
    pairs: Sequence[tuple[float, float]],
) -> float | None:
    """Krippendorff's alpha for two raters with the ordinal metric.

    Categories are the observed values; the difference function is the
    squared difference of each value's MIDRANK inside the pooled judgment
    distribution (the ordinal metric of Krippendorff 2011), so ties share a
    midpoint and distant categories are punished more than adjacent ones.

    Coincidence-matrix formulation (equivalent to the unit-level definition):
    each unit contributes o[v1][v2] += 1 and o[v2][v1] += 1; alpha =
    1 - Do/De with Do = sum(o_ck * w_ck)/n and De = sum(n_c * n_k * w_ck) /
    (n*(n-1)); n = total coincidence entries = 2 * len(pairs).
    """
    if pairs is None or np.asarray(pairs).size == 0:
        return None
    arr = np.asarray(pairs, dtype=float)
    a, b = arr[:, 0], arr[:, 1]
    values = np.concatenate([a, b])
    n = values.size
    if n < 2:
        return None
    if np.all(values == values[0]):
        return 1.0
    categories, counts = np.unique(values, return_counts=True)
    below = np.cumsum(counts) - counts
    midranks = below + (counts + 1) / 2.0
    index = {float(v): i for i, v in enumerate(categories)}
    ia = np.fromiter((index[float(v)] for v in a), dtype=int, count=len(a))
    ib = np.fromiter((index[float(v)] for v in b), dtype=int, count=len(b))

    k = len(categories)
    coincidence = np.zeros((k, k))
    for i in range(len(pairs)):
        coincidence[ia[i], ib[i]] += 1
        coincidence[ib[i], ia[i]] += 1
    marginals = coincidence.sum(axis=1)
    weights = (midranks[:, None] - midranks[None, :]) ** 2

    observed = (coincidence * weights).sum() / n
    expected = (marginals[:, None] * marginals[None, :] * weights).sum() / (
        n * (n - 1)
    )
    if expected == 0.0:
        return 1.0
    return float(1.0 - observed / expected)


def icc21(pairs: Sequence[tuple[float, float]]) -> float | None:
    """ICC(2,1) — McGraw & Wong 1996, two-way random effects, single
    measures, absolute agreement, exactly two coders.

    ICC = (MS_R - MS_E) / (MS_R + (k-1)*MS_E + (k/n)*(MS_C - MS_E))
    with k = 2 coders, n units, MS_R/MS_C/MS_E from a two-way ANOVA
    (rows = units, columns = coders). Absolute agreement (not consistency)
    means systematic coder offsets reduce the coefficient.
    """
    if pairs is None or np.asarray(pairs).size == 0 or len(pairs) < 2:
        return None
    arr = np.asarray(pairs, dtype=float)
    a, b = arr[:, 0], arr[:, 1]
    k, n = 2, len(pairs)
    grand = float(np.mean(np.concatenate([a, b])))
    unit_means = (a + b) / 2.0
    msr = k * np.sum((unit_means - grand) ** 2) / (n - 1)
    coder_means = np.array([a.mean(), b.mean()])
    msc = n * np.sum((coder_means - grand) ** 2) / (k - 1)
    sst = np.sum((np.concatenate([a, b]) - grand) ** 2)
    sse = sst - msr * (n - 1) - msc * (k - 1)
    mse = sse / ((n - 1) * (k - 1))
    denominator = msr + (k - 1) * mse + (k / n) * (msc - mse)
    if denominator == 0.0:
        # zero total variance: every judgment identical -> perfect agreement
        return 1.0 if msr == 0.0 else None
    return float((msr - mse) / denominator)


def _bootstrap_distribution(
    stat_fn: Callable[[Sequence[tuple[float, float]]], float | None],
    pairs: Sequence[tuple[float, float]],
    resamples: int,
    seed: int,
) -> np.ndarray:
    """Resample UNITS (not judgments) with replacement; one statistic each."""
    arr = np.asarray(pairs, dtype=float)
    n = len(arr)
    rng = np.random.default_rng(seed)
    indices = rng.integers(0, n, size=(resamples, n))
    out = np.empty(resamples)
    for r in range(resamples):
        out[r] = stat_fn(arr[indices[r]])
    return out


def bootstrap_ci(
    stat_fn: Callable[[Sequence[tuple[float, float]]], float | None],
    pairs: Sequence[tuple[float, float]],
    resamples: int = RESAMPLES,
    seed: int = SEED,
) -> tuple[float | None, float | None]:
    """Percentile bootstrap CI (2.5%, 97.5%) over the given statistic.

    Deterministic: the RNG is freshly seeded with ``seed`` per call, so any
    two runs over identical data yield bit-identical intervals.
    """
    point = stat_fn(pairs)
    if point is None:
        return (None, None)
    distribution = _bootstrap_distribution(stat_fn, pairs, resamples, seed)
    lo, hi = np.percentile(distribution, [2.5, 97.5])
    return (float(lo), float(hi))


# ---------------------------------------------------------------------------
# Calibration gates (policy-driven; see the policy block above)
# ---------------------------------------------------------------------------
def classify_agreement(value: float | None) -> str:
    """Map an agreement statistic to 'pass' | 'low' | 'block'."""
    if value is None or value < BLOCK_AGREEMENT_BELOW:
        return "block"
    if value >= CALIBRATION_FLOOR:
        return "pass"
    return "low"


def aggregation_gate(status: str) -> str:
    """Machine-readable aggregation verdict: allowed | inconclusive | blocked."""
    return {"pass": "allowed", "low": "inconclusive", "block": "blocked"}.get(
        status, "blocked"
    )


def aggregation_allowed(status: str) -> bool:
    """Only a passing agreement may be merged into downstream output."""
    return status == "pass"


def drift_check_due(
    new_shared_items: int, days_since_last_check: float | None
) -> bool:
    """Drift check is due at 200 newly scored shared items OR 7 elapsed days
    since the last check — whichever occurs first. A never-checked scorer is
    always due.
    """
    item_threshold, day_threshold = DRIFT_ITEMS_OR_DAYS
    if new_shared_items >= item_threshold:
        return True
    if days_since_last_check is None:
        return True
    return days_since_last_check >= day_threshold


def aggregation_summary(agreements: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Worst-case aggregation status across a scorer's peer agreements.

    Any blocked peer hard-blocks aggregation; otherwise any low peer forces
    an inconclusive result; with no peers there is no evidence, which also
    blocks. The binding statistic/interval come from the worst-status entry
    with the lowest statistic.
    """
    if not agreements:
        return {
            "status": "blocked",
            "statistic": None,
            "interval": None,
            "policy": dict(CALIBRATION_POLICY),
        }
    ranks = {"pass": 0, "low": 1, "block": 2}
    worst_rank = max(ranks.get(a.get("status", "block"), 2) for a in agreements)
    worst_status = {0: "pass", 1: "low", 2: "block"}[worst_rank]
    candidates = [
        a for a in agreements if ranks.get(a.get("status", "block"), 2) == worst_rank
    ]
    keyed = [
        a for a in candidates if a.get("statistic") is not None
    ]
    binding = min(keyed, key=lambda a: a["statistic"]) if keyed else {}
    return {
        "status": aggregation_gate(worst_status),
        "statistic": binding.get("statistic"),
        "interval": binding.get("interval"),
        "policy": dict(CALIBRATION_POLICY),
    }


class AgreementBlockedError(RuntimeError):
    """Raised when downstream aggregation is attempted below the 0.667 gate."""


@dataclass(frozen=True)
class AggregationVerdict:
    """Machine-readable downstream result with the policy attached.

    ``status`` is 'allowed' | 'inconclusive' | 'blocked'; ``value`` is set
    ONLY for 'allowed' — a low-agreement merge never surfaces as a plain
    score.
    """

    status: str
    value: float | None
    interval: tuple[float | None, float | None] | None
    policy: dict[str, Any] = field(default_factory=lambda: dict(CALIBRATION_POLICY))


def guarded_aggregate(
    *,
    status: str,
    statistic: float | None,
    interval: tuple[float | None, float | None] | None,
    values: Sequence[float],
) -> AggregationVerdict:
    """Aggregate scores under the reliability gate.

    - 'pass': aggregation allowed; the plain aggregate is returned.
    - 'low' : inconclusive — the result carries NO value, only status+CI.
    - 'block': raises AgreementBlockedError (aggregation is refused).
    """
    if status == "block":
        raise AgreementBlockedError(
            f"aggregation blocked: agreement {statistic} is below "
            f"{BLOCK_AGREEMENT_BELOW}"
        )
    if status == "low":
        return AggregationVerdict(
            status="inconclusive", value=None, interval=interval
        )
    mean = float(np.mean(values)) if len(values) else None
    return AggregationVerdict(status="allowed", value=mean, interval=interval)


# ---------------------------------------------------------------------------
# Agreement / drift records
# ---------------------------------------------------------------------------
@dataclass
class ScorerAgreement:
    """Agreement metrics between two scorers."""

    scorer_a: str
    scorer_b: str
    total_comparisons: int
    agreement_count: int
    agreement_rate: float  # 0.0 to 1.0
    cohens_kappa: float | None  # legacy inter-rater reliability metric
    last_updated: str
    # T4 governance fields (defaulted: legacy constructions keep working):
    # exactly one of ord_alpha / icc21 is populated, named by ``statistic``.
    ord_alpha: float | None = None  # Krippendorff ordinal alpha (categorical)
    icc21: float | None = None  # ICC(2,1) (continuous scores)
    statistic: str | None = None  # 'krippendorff_ordinal_alpha' | 'icc21'
    ci_lo: float | None = None  # percentile-bootstrap CI (2.5%)
    ci_hi: float | None = None  # percentile-bootstrap CI (97.5%)
    status: str | None = None  # 'pass' | 'low' | 'block'
    resamples: int = RESAMPLES
    seed: int = SEED
    policy: dict[str, Any] = field(
        default_factory=lambda: dict(CALIBRATION_POLICY)
    )

    def is_acceptable(self, min_agreement: float = 0.8) -> bool:
        """Check if agreement rate meets minimum threshold."""
        return self.agreement_rate >= min_agreement


@dataclass
class ScorerDrift:
    """Drift detection for a scorer over time."""

    scorer_name: str
    baseline_agreement: float
    current_agreement: float
    drift_magnitude: float  # Absolute change from baseline
    period_start: str
    period_end: str
    sample_count: int
    # T4 governance fields:
    trigger_due: bool = False
    new_shared_items: int = 0
    days_since_last_check: float | None = None
    policy: dict[str, Any] = field(
        default_factory=lambda: dict(CALIBRATION_POLICY)
    )

    def has_significant_drift(self, threshold: float = 0.1) -> bool:
        """Check if drift exceeds significance threshold."""
        return self.drift_magnitude > threshold


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------
_PAIR_SQL = """
SELECT m1.score, m2.score
FROM hr.measurement m1
JOIN hr.measurement m2
    ON m1.item_id = m2.item_id
    AND m1.run_id = m2.run_id
WHERE m1.scorer_name = %s
  AND m2.scorer_name = %s
"""

_PAIR_SQL_SWEEP = """
SELECT m1.score, m2.score
FROM hr.measurement m1
JOIN hr.measurement m2
    ON m1.item_id = m2.item_id
    AND m1.run_id = m2.run_id
JOIN hr.run r ON m1.run_id = r.run_id
WHERE m1.scorer_name = %s
  AND m2.scorer_name = %s
  AND r.sweep_id = %s
"""

_PAIR_SQL_PERIOD = """
SELECT m1.score, m2.score
FROM hr.measurement m1
JOIN hr.measurement m2
    ON m1.item_id = m2.item_id
    AND m1.run_id = m2.run_id
JOIN hr.run r ON m1.run_id = r.run_id
WHERE m1.scorer_name = %s
  AND m2.scorer_name != %s
  AND r.finished_at >= %s
  AND r.finished_at < %s
"""

_LAST_SHARED_CHECK_SQL = """
SELECT MAX(m1.created_at)
FROM hr.measurement m1
JOIN hr.measurement m2
    ON m1.item_id = m2.item_id
    AND m1.run_id = m2.run_id
WHERE m1.scorer_name = %s
  AND m2.scorer_name != %s
"""


class ScorerCalibrationManager:
    """Manage scorer calibration and drift detection."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def measure_agreement(
        self,
        scorer_a: str,
        scorer_b: str,
        sweep_id: str | None = None,
    ) -> ScorerAgreement:
        """Measure agreement between two scorers on overlapping evaluations.

        Pairs are fetched as (m1.score, m2.score) rows; the legacy
        count/kappa are derived from them, then the governance statistic is
        computed: Krippendorff ordinal alpha for {0,1} verdicts, ICC(2,1)
        for continuous scores (0-100 bench scores are normalized to 0-1
        first), both with a fixed-seed percentile bootstrap CI and a
        calibration status.
        """
        if sweep_id:
            sql, params = _PAIR_SQL_SWEEP, (scorer_a, scorer_b, sweep_id)
        else:
            sql, params = _PAIR_SQL, (scorer_a, scorer_b)
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        pairs = [(float(x), float(y)) for x, y in rows]
        total = len(pairs)
        agree = sum(1 for x, y in pairs if x == y)
        if total == 0:
            agreement_rate = 0.0
            kappa = None
        else:
            agreement_rate = agree / total
            kappa = self._calculate_cohens_kappa(agree, total - agree)

        normalized = self._normalize_pairs(pairs)
        if total < 2:
            statistic: str | None = None
            point: float | None = None
        else:
            statistic, stat_fn = self._select_statistic(normalized)
            point = stat_fn(normalized)
        if statistic is not None and point is not None:
            lo, hi = bootstrap_ci(
                stat_fn, normalized, resamples=RESAMPLES, seed=SEED
            )
        else:
            lo = hi = None

        return ScorerAgreement(
            scorer_a=scorer_a,
            scorer_b=scorer_b,
            total_comparisons=total,
            agreement_count=agree,
            agreement_rate=agreement_rate,
            cohens_kappa=kappa,
            last_updated=datetime.now(timezone.utc).isoformat(),
            ord_alpha=point if statistic == "krippendorff_ordinal_alpha" else None,
            icc21=point if statistic == "icc21" else None,
            statistic=statistic,
            ci_lo=lo,
            ci_hi=hi,
            status=classify_agreement(point),
            resamples=RESAMPLES,
            seed=SEED,
            policy=dict(CALIBRATION_POLICY),
        )

    def detect_drift(
        self,
        scorer_name: str,
        baseline_period_days: int = 30,
        comparison_period_days: int = 7,
    ) -> ScorerDrift:
        """Detect if a scorer's behavior has drifted from baseline.

        ``new_shared_items`` counts the shared item pairs scored inside the
        comparison window; ``days_since_last_check`` is the time since the
        scorer's most recent shared measurement (None if never checked);
        ``trigger_due`` applies the (200 items OR 7 days) policy.
        """
        now = datetime.now(timezone.utc)
        baseline_end = now - timedelta(days=comparison_period_days)
        baseline_start = baseline_end - timedelta(days=baseline_period_days)
        comparison_start = baseline_end

        baseline_pairs = self._fetch_pairs(scorer_name, baseline_start, baseline_end)
        current_pairs = self._fetch_pairs(scorer_name, comparison_start, now)
        sample_count = self._get_sample_count(scorer_name, comparison_start, now)

        baseline_agreement = _rate_from_pairs(baseline_pairs)
        current_agreement = _rate_from_pairs(current_pairs)
        drift = abs(current_agreement - baseline_agreement)

        last_check = self._last_shared_check(scorer_name)
        if last_check is None:
            days_since: float | None = None
        else:
            days_since = (now - last_check).total_seconds() / 86400.0
        new_shared_items = len(current_pairs)
        trigger_due = drift_check_due(new_shared_items, days_since)

        return ScorerDrift(
            scorer_name=scorer_name,
            baseline_agreement=baseline_agreement,
            current_agreement=current_agreement,
            drift_magnitude=drift,
            period_start=baseline_start.isoformat(),
            period_end=now.isoformat(),
            sample_count=sample_count,
            trigger_due=trigger_due,
            new_shared_items=new_shared_items,
            days_since_last_check=days_since,
            policy=dict(CALIBRATION_POLICY),
        )

    def get_scorer_reliability_report(self, scorer_name: str) -> dict[str, Any]:
        """Generate comprehensive reliability report for a scorer."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT scorer_name
                FROM hr.measurement
                WHERE scorer_name != %s
                LIMIT 5
                """,
                (scorer_name,),
            )
            other_scorers = [row[0] for row in cur.fetchall()]

        agreements = []
        for other in other_scorers:
            agreement = self.measure_agreement(scorer_name, other)
            agreements.append(
                {
                    "other_scorer": other,
                    "agreement_rate": agreement.agreement_rate,
                    "comparisons": agreement.total_comparisons,
                    "acceptable": agreement.is_acceptable(),
                    "status": agreement.status,
                    "statistic": agreement.statistic,
                    "interval": (
                        [agreement.ci_lo, agreement.ci_hi]
                        if agreement.ci_lo is not None
                        else None
                    ),
                }
            )

        drift = self.detect_drift(scorer_name)

        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) as total_scores,
                    AVG(score) as mean_score,
                    STDDEV(score) as std_score
                FROM hr.measurement
                WHERE scorer_name = %s
                """,
                (scorer_name,),
            )
            stats_row = cur.fetchone()

        total_scores = stats_row[0] if stats_row else 0
        mean_score = stats_row[1] if stats_row else 0.0
        std_score = stats_row[2] if stats_row else 0.0

        # peers that never shared an item carry no agreement evidence: they
        # stay visible in ``agreements`` (status stays 'block') but must NOT
        # gate aggregation of the scorer's own scores.
        evidence = [entry for entry in agreements if entry["comparisons"] > 0]

        return {
            "scorer_name": scorer_name,
            "total_scores": total_scores,
            "mean_score": float(mean_score) if mean_score else 0.0,
            "std_score": float(std_score) if std_score else 0.0,
            "agreements": agreements,
            "drift": {
                "baseline_agreement": drift.baseline_agreement,
                "current_agreement": drift.current_agreement,
                "drift_magnitude": drift.drift_magnitude,
                "significant": drift.has_significant_drift(),
                "trigger_due": drift.trigger_due,
                "new_shared_items": drift.new_shared_items,
                "days_since_last_check": drift.days_since_last_check,
            },
            "overall_reliable": self._assess_overall_reliability(
                agreements, drift
            ),
            "aggregation": aggregation_summary(evidence),
            "policy": dict(CALIBRATION_POLICY),
        }

    def _calculate_cohens_kappa(self, agree: int, disagree: int) -> float | None:
        """Calculate Cohen's kappa for inter-rater reliability."""
        total = agree + disagree
        if total == 0:
            return None

        # Simplified kappa for binary agreement
        # In practice, this would need the full confusion matrix
        observed_agreement = agree / total
        # Assume chance agreement of 0.5 for binary case
        chance_agreement = 0.5
        if observed_agreement == chance_agreement:
            return 0.0

        kappa = (observed_agreement - chance_agreement) / (1.0 - chance_agreement)
        return max(-1.0, min(1.0, kappa))

    def _fetch_pairs(
        self, scorer_name: str, start: datetime, end: datetime
    ) -> list[tuple[float, float]]:
        """Shared (score_a, score_b) pairs for a scorer in a time window."""
        with self._conn.cursor() as cur:
            cur.execute(
                _PAIR_SQL_PERIOD, (scorer_name, scorer_name, start, end)
            )
            rows = cur.fetchall()
        return [(float(a), float(b)) for a, b in rows]

    def _last_shared_check(self, scorer_name: str) -> datetime | None:
        """Most recent shared-pair measurement time for the scorer."""
        with self._conn.cursor() as cur:
            cur.execute(_LAST_SHARED_CHECK_SQL, (scorer_name, scorer_name))
            row = cur.fetchone()
        if not row or row[0] is None:
            return None
        return row[0]

    def _get_agreement_rate(
        self, scorer_name: str, start: datetime, end: datetime
    ) -> float:
        """Get agreement rate for a scorer in a time period."""
        return _rate_from_pairs(self._fetch_pairs(scorer_name, start, end))

    def _get_sample_count(
        self, scorer_name: str, start: datetime, end: datetime
    ) -> int:
        """Get sample count for a scorer in a time period."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM hr.measurement m
                JOIN hr.run r ON m.run_id = r.run_id
                WHERE m.scorer_name = %s
                  AND r.finished_at >= %s
                  AND r.finished_at < %s
                """,
                (scorer_name, start, end),
            )
            row = cur.fetchone()

        return row[0] if row else 0

    def _assess_overall_reliability(
        self, agreements: list[dict[str, Any]], drift: ScorerDrift
    ) -> bool:
        """Assess if a scorer is overall reliable based on agreements and drift."""
        if not agreements:
            return False

        acceptable_count = sum(1 for a in agreements if a["acceptable"])
        if acceptable_count < len(agreements) * 0.7:
            return False

        if drift.has_significant_drift():
            return False

        return True

    @staticmethod
    def _normalize_pairs(
        pairs: list[tuple[float, float]],
    ) -> list[tuple[float, float]]:
        """Map bench 0-100 scores to 0-1 before the statistic step."""
        if not pairs:
            return pairs
        max_seen = max(v for p in pairs for v in p)
        max_score = 100.0 if max_seen > 1.0 else 1.0
        return [
            (
                normalize_bounded_score(a, max_score=max_score),
                normalize_bounded_score(b, max_score=max_score),
            )
            for a, b in pairs
        ]

    @staticmethod
    def _select_statistic(
        pairs: list[tuple[float, float]],
    ) -> tuple[str, Callable[[Sequence[tuple[float, float]]], float | None]]:
        """Pick ordinal alpha for {0,1} verdicts, ICC(2,1) for continuous."""
        values = np.unique(np.asarray(pairs, dtype=float))
        if np.all(np.isin(values, [0.0, 1.0])):
            return "krippendorff_ordinal_alpha", krippendorff_ordinal_alpha
        return "icc21", icc21


def _rate_from_pairs(pairs: Sequence[tuple[float, float]]) -> float:
    if not pairs:
        return 0.0
    agree = sum(1 for x, y in pairs if x == y)
    return agree / len(pairs)


__all__ = [
    "CALIBRATION_FLOOR",
    "BLOCK_AGREEMENT_BELOW",
    "LOW_AGREEMENT_CEILING",
    "RESAMPLES",
    "SEED",
    "DRIFT_ITEMS_OR_DAYS",
    "CALIBRATION_POLICY",
    "krippendorff_ordinal_alpha",
    "icc21",
    "bootstrap_ci",
    "classify_agreement",
    "aggregation_gate",
    "aggregation_allowed",
    "drift_check_due",
    "aggregation_summary",
    "AgreementBlockedError",
    "AggregationVerdict",
    "guarded_aggregate",
    "ScorerAgreement",
    "ScorerDrift",
    "ScorerCalibrationManager",
]