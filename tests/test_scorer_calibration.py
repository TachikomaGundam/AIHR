"""Tests for scorer calibration, drift and agreement governance (T4).

Covers:
* the mocked-manager unit surface (legacy contract shape — agreement rate,
  Cohen's kappa, drift magnitude — plus the T4 governance fields: ordinal
  alpha / ICC(2,1), bootstrap intervals, calibration status);
* live-DB round trips through the shared ``scratch_db`` fixture (db-marked):
  alignment passing the 0.80 floor, categorical disagreement blocking
  aggregation below 0.667, continuous scores in the 0.667-0.799 band marked
  inconclusive, drift triggers (>200 shared items / >7 days), reproducibility
  of bootstrap intervals across two real manager runs, and scorer
  name/version provenance never persisting ``unknown`` on the stage0 /
  stage1(shared writer) / livebench write paths.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import psycopg2
import psycopg2.extensions
import pytest

from hr.scorer_calibration import (
    CALIBRATION_POLICY,
    RESAMPLES,
    SEED,
    ScorerAgreement,
    ScorerCalibrationManager,
    ScorerDrift,
    aggregation_summary,
    classify_agreement,
    guarded_aggregate,
)
from tests._db_contracts_helpers import (
    connect,
    seed_battery,
    seed_item_pool,
    seed_measurement,
    seed_provider_models,
    seed_run,
    seed_seat,
    seed_sweep,
)


# ---------------------------------------------------------------------------
# scoped mock connection (records one query per call)
# ---------------------------------------------------------------------------
def _mock_cursor(fetchall=None, fetchone=None) -> MagicMock:
    cursor = MagicMock()
    if fetchall is not None:
        cursor.fetchall.side_effect = list(fetchall)
    if fetchone is not None:
        cursor.fetchone.side_effect = list(fetchone)
    return cursor


def _mock_conn(cursor: MagicMock) -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value = cursor
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    return conn


def _agreement_row_mocks(total: int, agree: int) -> list[tuple[tuple[float, float], ...]]:
    """Pairs matching the legacy (total, agree) contract shape."""
    agree_pairs = [(0.8, 0.8)] * agree
    disagree_pairs = [(0.8, 0.4)] * (total - agree)
    return [tuple(agree_pairs + disagree_pairs)]


# ---------------------------------------------------------------------------
# legacy contract surface (unchanged semantics, new SQL shape)
# ---------------------------------------------------------------------------
def test_scorer_agreement_is_acceptable_above_threshold() -> None:
    agreement = ScorerAgreement(
        scorer_a="rule_based",
        scorer_b="llm_judge",
        total_comparisons=100,
        agreement_count=85,
        agreement_rate=0.85,
        cohens_kappa=0.7,
        last_updated="2024-01-01",
    )
    assert agreement.is_acceptable(min_agreement=0.8) is True


def test_scorer_agreement_is_unacceptable_below_threshold() -> None:
    agreement = ScorerAgreement(
        scorer_a="rule_based",
        scorer_b="llm_judge",
        total_comparisons=100,
        agreement_count=70,
        agreement_rate=0.70,
        cohens_kappa=0.4,
        last_updated="2024-01-01",
    )
    assert agreement.is_acceptable(min_agreement=0.8) is False


def test_scorer_drift_is_significant_above_threshold() -> None:
    drift = ScorerDrift(
        scorer_name="llm_judge",
        baseline_agreement=0.9,
        current_agreement=0.75,
        drift_magnitude=0.15,
        period_start="2024-01-01",
        period_end="2024-01-31",
        sample_count=100,
    )
    assert drift.has_significant_drift(threshold=0.1) is True


def test_scorer_drift_is_not_significant_below_threshold() -> None:
    drift = ScorerDrift(
        scorer_name="llm_judge",
        baseline_agreement=0.9,
        current_agreement=0.88,
        drift_magnitude=0.02,
        period_start="2024-01-01",
        period_end="2024-01-31",
        sample_count=100,
    )
    assert drift.has_significant_drift(threshold=0.1) is False


def test_measure_agreement_with_no_data() -> None:
    manager = ScorerCalibrationManager(_mock_conn(_mock_cursor(fetchall=[()])))
    agreement = manager.measure_agreement("scorer_a", "scorer_b")

    assert agreement.total_comparisons == 0
    assert agreement.agreement_rate == 0.0
    assert agreement.cohens_kappa is None
    assert agreement.status == "block"
    assert agreement.statistic is None


def test_measure_agreement_with_data() -> None:
    manager = ScorerCalibrationManager(
        _mock_conn(_mock_cursor(fetchall=_agreement_row_mocks(100, 85)))
    )
    agreement = manager.measure_agreement("scorer_a", "scorer_b")

    assert agreement.total_comparisons == 100
    assert agreement.agreement_count == 85
    assert agreement.agreement_rate == pytest.approx(0.85)
    assert agreement.cohens_kappa == pytest.approx(0.7)
    assert agreement.resamples == RESAMPLES
    assert agreement.seed == SEED
    assert agreement.policy == CALIBRATION_POLICY


def test_measure_agreement_with_sweep_filter() -> None:
    manager = ScorerCalibrationManager(
        _mock_conn(_mock_cursor(fetchall=_agreement_row_mocks(50, 45)))
    )
    agreement = manager.measure_agreement("scorer_a", "scorer_b", sweep_id="test-sweep")

    assert agreement.total_comparisons == 50
    assert agreement.agreement_count == 45
    assert agreement.agreement_rate == pytest.approx(0.9)


def test_detect_drift_calculates_magnitude() -> None:
    baseline_pairs = tuple([(0.8, 0.8)] * 90)  # 1.0 agreement
    current_pairs = tuple([(0.8, 0.8)] * 37 + [(0.8, 0.4)] * 13)  # 0.74
    manager = ScorerCalibrationManager(
        _mock_conn(
            _mock_cursor(
                fetchall=[baseline_pairs, current_pairs],
                fetchone=[(50,), (datetime(2026, 8, 19, tzinfo=timezone.utc),)],
            )
        )
    )
    drift = manager.detect_drift("llm_judge", baseline_period_days=30, comparison_period_days=7)

    assert drift.scorer_name == "llm_judge"
    assert drift.baseline_agreement == pytest.approx(1.0)
    assert drift.current_agreement == pytest.approx(0.74)
    assert drift.drift_magnitude == pytest.approx(0.26)
    assert drift.has_significant_drift()
    assert drift.policy == CALIBRATION_POLICY


def test_get_scorer_reliability_report() -> None:
    cmd = MagicMock()
    cmd.fetchall.side_effect = [
        [("rule_based",), ("exact_match",)],  # other scorers
        tuple([(0.8, 0.8)] * 60 + [(0.82, 0.82)] * 25),  # rule_based: aligned
        tuple([(0.75, 0.75)] * 50 + [(0.74, 0.74)] * 40),  # exact_match: aligned
        tuple([(0.8, 0.8)] * 90),  # baseline pairs
        tuple([(0.8, 0.8)] * 44 + [(0.8, 0.4)] * 6),  # current pairs (0.88)
    ]
    cmd.fetchone.side_effect = [
        (50,),  # sample count
        (datetime(2026, 8, 19, tzinfo=timezone.utc),),  # last shared check
        (1000, 0.75, 0.15),  # overall stats
    ]
    manager = ScorerCalibrationManager(_mock_conn(cmd))
    report = manager.get_scorer_reliability_report("llm_judge")

    assert report["scorer_name"] == "llm_judge"
    assert report["total_scores"] == 1000
    assert len(report["agreements"]) == 2
    assert "drift" in report
    assert "overall_reliable" in report
    assert report["policy"] == CALIBRATION_POLICY
    agg = report["aggregation"]
    assert agg["status"] == "allowed"
    assert agg["interval"] is not None
    # every agreement entry carries the governance status
    for entry in report["agreements"]:
        assert entry["status"] in ("pass", "low", "block")


def test_cohens_kappa_calculation() -> None:
    manager = ScorerCalibrationManager(MagicMock())
    kappa = manager._calculate_cohens_kappa(agree=80, disagree=20)
    assert kappa is not None
    assert -1.0 <= kappa <= 1.0
    assert kappa > 0.0


def test_cohens_kappa_returns_none_for_zero_total() -> None:
    manager = ScorerCalibrationManager(MagicMock())
    assert manager._calculate_cohens_kappa(agree=0, disagree=0) is None


def test_assess_overall_reliability_with_good_metrics() -> None:
    manager = ScorerCalibrationManager(MagicMock())
    agreements = [
        {"acceptable": True, "agreement_rate": 0.85},
        {"acceptable": True, "agreement_rate": 0.90},
    ]
    drift = ScorerDrift(
        scorer_name="test",
        baseline_agreement=0.9,
        current_agreement=0.88,
        drift_magnitude=0.02,
        period_start="2024-01-01",
        period_end="2024-01-31",
        sample_count=100,
    )
    assert manager._assess_overall_reliability(agreements, drift) is True


def test_assess_overall_reliability_with_bad_agreements() -> None:
    manager = ScorerCalibrationManager(MagicMock())
    agreements = [
        {"acceptable": False, "agreement_rate": 0.60},
        {"acceptable": False, "agreement_rate": 0.65},
    ]
    drift = ScorerDrift(
        scorer_name="test",
        baseline_agreement=0.9,
        current_agreement=0.88,
        drift_magnitude=0.02,
        period_start="2024-01-01",
        period_end="2024-01-31",
        sample_count=100,
    )
    assert manager._assess_overall_reliability(agreements, drift) is False


def test_assess_overall_reliability_with_significant_drift() -> None:
    manager = ScorerCalibrationManager(MagicMock())
    agreements = [
        {"acceptable": True, "agreement_rate": 0.85},
        {"acceptable": True, "agreement_rate": 0.90},
    ]
    drift = ScorerDrift(
        scorer_name="test",
        baseline_agreement=0.9,
        current_agreement=0.75,
        drift_magnitude=0.15,
        period_start="2024-01-01",
        period_end="2024-01-31",
        sample_count=100,
    )
    assert manager._assess_overall_reliability(agreements, drift) is False


# ---------------------------------------------------------------------------
# T4 governance: statistics + gates through the manager
# ---------------------------------------------------------------------------
def test_measure_agreement_categorical_verdict_uses_ordinal_alpha() -> None:
    pairs = tuple([(0, 1), (1, 0), (0, 1), (1, 0)])
    manager = ScorerCalibrationManager(_mock_conn(_mock_cursor(fetchall=[pairs])))
    agreement = manager.measure_agreement("judge_a", "judge_b")

    from hr.scorer_calibration import krippendorff_ordinal_alpha

    assert agreement.statistic == "krippendorff_ordinal_alpha"
    # the manager's value must equal the pure function on the same pairs
    assert agreement.ord_alpha == pytest.approx(krippendorff_ordinal_alpha(list(pairs)))
    assert agreement.icc21 is None
    assert agreement.status == "block"
    assert agreement.ci_lo <= agreement.ord_alpha <= agreement.ci_hi


def test_measure_agreement_continuous_scores_use_icc21() -> None:
    pairs = tuple([(0.5, 0.58), (0.6, 0.68), (0.7, 0.75)])
    manager = ScorerCalibrationManager(_mock_conn(_mock_cursor(fetchall=[pairs])))
    agreement = manager.measure_agreement("judge_a", "judge_b")

    assert agreement.statistic == "icc21"
    assert agreement.icc21 == pytest.approx(170.0 / 221.0)
    assert agreement.ord_alpha is None
    assert agreement.status == "low"  # inside 0.667..0.799 -> inconclusive band


def test_measure_agreement_aligned_scorers_pass() -> None:
    pairs = tuple([(0.6, 0.6), (0.7, 0.7), (0.8, 0.8)])
    manager = ScorerCalibrationManager(_mock_conn(_mock_cursor(fetchall=[pairs])))
    agreement = manager.measure_agreement("judge_a", "judge_b")

    assert agreement.status == "pass"
    assert agreement.icc21 == pytest.approx(1.0)
    assert agreement.ci_lo == pytest.approx(1.0)
    assert agreement.ci_hi == pytest.approx(1.0)
    assert aggregation_summary([{"status": agreement.status}])["status"] == "allowed"


def test_measure_agreement_normalizes_hundred_scale_scores() -> None:
    # livebench 0-100 scores must normalize to 0-1 before the ICC step.
    hundred = tuple([(50.0, 58.0), (60.0, 68.0), (70.0, 75.0)])
    manager = ScorerCalibrationManager(_mock_conn(_mock_cursor(fetchall=[hundred])))
    agreement = manager.measure_agreement("judge_a", "judge_b")

    assert agreement.statistic == "icc21"
    assert agreement.icc21 == pytest.approx(170.0 / 221.0)  # same as 0-1 scale
    assert agreement.status == "low"


def test_measure_agreement_bootstrap_reproducible_two_runs() -> None:
    pairs = tuple([(0.5, 0.58), (0.6, 0.68), (0.7, 0.75)]
                  + [(0.4, 0.5), (0.9, 0.85), (0.3, 0.4)])
    manager1 = ScorerCalibrationManager(_mock_conn(_mock_cursor(fetchall=[pairs])))
    manager2 = ScorerCalibrationManager(_mock_conn(_mock_cursor(fetchall=[pairs])))
    a1 = manager1.measure_agreement("judge_a", "judge_b")
    a2 = manager2.measure_agreement("judge_a", "judge_b")
    assert (a1.icc21, a1.ci_lo, a1.ci_hi) == (a2.icc21, a2.ci_lo, a2.ci_hi)


def test_detect_drift_reports_trigger_governance() -> None:
    # 205 freshly scored shared items -> drift check due by the ITEMS rule.
    current_pairs = tuple([(0.8, 0.8)] * 205)
    manager = ScorerCalibrationManager(
        _mock_conn(
            _mock_cursor(
                fetchall=[(), current_pairs], fetchone=[(205,), None]
            )
        )
    )
    drift = manager.detect_drift("scorer-herd")

    assert drift.new_shared_items == 205
    assert drift.days_since_last_check is None  # never checked before
    assert drift.trigger_due is True


def test_detect_drift_not_due_under_both_thresholds() -> None:
    current_pairs = tuple([(0.8, 0.8)] * 5)
    last_check = datetime.now(timezone.utc) - timedelta(days=3)
    manager = ScorerCalibrationManager(
        _mock_conn(
            _mock_cursor(fetchall=[(), current_pairs], fetchone=[(5,), (last_check,)])
        )
    )
    drift = manager.detect_drift("scorer-herd")
    assert drift.trigger_due is False


def test_aggregation_summary_blocks_on_any_blocked_agreement() -> None:
    agreements = [
        {"status": "pass", "statistic": 0.91, "interval": (0.85, 0.96)},
        {"status": "block", "statistic": 0.40, "interval": (0.2, 0.6)},
    ]
    summary = aggregation_summary(agreements)
    assert summary["status"] == "blocked"


def test_aggregation_summary_inconclusive_on_low_agreement() -> None:
    agreements = [
        {"status": "pass", "statistic": 0.91, "interval": (0.85, 0.96)},
        {"status": "low", "statistic": 0.72, "interval": (0.6, 0.83)},
    ]
    summary = aggregation_summary(agreements)
    assert summary["status"] == "inconclusive"


def test_aggregation_summary_blocked_without_evidence() -> None:
    assert aggregation_summary([])["status"] == "blocked"


def test_aggregation_summary_allowed_only_when_everything_passes() -> None:
    agreements = [
        {"status": "pass", "statistic": 0.91, "interval": (0.85, 0.96)},
        {"status": "pass", "statistic": 0.88, "interval": (0.8, 0.95)},
    ]
    assert aggregation_summary(agreements)["status"] == "allowed"


# ---------------------------------------------------------------------------
# live DB: alignment, blocking, inconclusive band, drift triggers,
# reproducibility, provenance — all through the shared scratch_db
# ---------------------------------------------------------------------------
MODEL_GOV = "prov-gov/scorer-probe"


def _bulk_shared(
    conn: psycopg2.extensions.connection,
    *,
    run_id: str,
    item_prefix: str,
    n: int,
    scorer_a: str,
    scorer_b: str,
    score_a: float,
    score_b: float,
    backdate_days: int | None = None,
) -> None:
    """One run + n item_pool rows + n shared pairs (scorer_a rep1 / b rep2)."""
    seed_run(conn, run_id, "sw-gov", MODEL_GOV, "battery-reasoning")
    for i in range(1, n + 1):
        seed_item_pool(conn, f"{item_prefix}-{i:04d}", domain="reasoning", meta={"tier": 3})
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO hr.measurement (measurement_id, run_id, item_id, repetition, "
            "score, tokens_in, tokens_out, latency_ms, scorer_name, scorer_version) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            [
                (f"m-{item_prefix}-{i:04d}-a", run_id, f"{item_prefix}-{i:04d}", 1,
                 score_a, 10, 10, 10, scorer_a, "1.0")
                for i in range(1, n + 1)
            ]
            + [
                (f"m-{item_prefix}-{i:04d}-b", run_id, f"{item_prefix}-{i:04d}", 2,
                 score_b, 10, 10, 10, scorer_b, "1.0")
                for i in range(1, n + 1)
            ],
        )
        if backdate_days is not None:
            old = datetime.now(timezone.utc) - timedelta(days=backdate_days)
            cur.execute(
                "UPDATE hr.run SET finished_at = %s, started_at = %s WHERE run_id = %s",
                (old, old, run_id),
            )
            cur.execute(
                "UPDATE hr.measurement SET created_at = %s WHERE run_id = %s",
                (old, run_id),
            )
    conn.commit()


@pytest.fixture(scope="module")
def live_conn(scratch_db: tuple[str, str]) -> psycopg2.extensions.connection:
    _name, dsn = scratch_db
    conn = connect(dsn)
    seed_provider_models(conn, (MODEL_GOV,))
    seed_seat(conn, "oracle", "High-IQ consultant")
    seed_seat(conn, "_stage0_sweep", "Stage-0 sweep")
    seed_battery(conn, "reasoning")
    seed_battery(conn, "tool_a")
    seed_sweep(conn, "sw-gov")
    # item pools used by the provenance tests
    seed_item_pool(conn, "reasoning.q.001", domain="reasoning", meta={"tier": 3})
    seed_item_pool(conn, "livebench.cg.001", domain="livebench", meta={"tier": 3})
    seed_item_pool(conn, "livebench.cg.002", domain="livebench", meta={"tier": 3})
    yield conn
    conn.close()


@pytest.mark.db
@pytest.mark.integration
def test_live_aligned_scorers_pass_calibration_floor(
    live_conn: psycopg2.extensions.connection,
) -> None:
    seed_run(live_conn, "run-gov-al", "sw-gov", MODEL_GOV, "battery-reasoning")
    seed_measurement(live_conn, "m-al-1a", "run-gov-al", "reasoning.q.001",
                     repetition=1, score=0.6, scorer_name="al-a", scorer_version="1.0")
    seed_measurement(live_conn, "m-al-1b", "run-gov-al", "reasoning.q.001",
                     repetition=2, score=0.6, scorer_name="al-b", scorer_version="1.0")
    seed_measurement(live_conn, "m-al-2a", "run-gov-al", "livebench.cg.001",
                     repetition=1, score=0.7, scorer_name="al-a", scorer_version="1.0")
    seed_measurement(live_conn, "m-al-2b", "run-gov-al", "livebench.cg.001",
                     repetition=2, score=0.7, scorer_name="al-b", scorer_version="1.0")
    seed_measurement(live_conn, "m-al-3a", "run-gov-al", "livebench.cg.002",
                     repetition=1, score=0.8, scorer_name="al-a", scorer_version="1.0")
    seed_measurement(live_conn, "m-al-3b", "run-gov-al", "livebench.cg.002",
                     repetition=2, score=0.8, scorer_name="al-b", scorer_version="1.0")

    agreement = ScorerCalibrationManager(live_conn).measure_agreement("al-a", "al-b")
    assert agreement.total_comparisons == 3
    assert agreement.status == "pass"
    assert agreement.icc21 == pytest.approx(1.0)
    assert agreement.ci_lo == pytest.approx(1.0)
    assert agreement.ci_hi == pytest.approx(1.0)
    assert agreement.statistic == "icc21"


@pytest.mark.db
@pytest.mark.integration
def test_live_categorical_disagreement_blocks_aggregation(
    live_conn: psycopg2.extensions.connection,
) -> None:
    _bulk_shared(live_conn, run_id="run-gov-cat", item_prefix="cat", n=4,
                 scorer_a="cat-a", scorer_b="cat-b", score_a=0.0, score_b=1.0)
    # flip two of the four pairs so exactly half disagree
    with live_conn.cursor() as cur:
        cur.execute(
            "UPDATE hr.measurement SET score = 0.0 WHERE measurement_id = %s",
            ("m-cat-0002-b",),
        )
        cur.execute(
            "UPDATE hr.measurement SET score = 0.0 WHERE measurement_id = %s",
            ("m-cat-0004-b",),
        )
    live_conn.commit()

    manager = ScorerCalibrationManager(live_conn)
    agreement = manager.measure_agreement("cat-a", "cat-b")
    assert agreement.total_comparisons == 4
    assert agreement.statistic == "krippendorff_ordinal_alpha"
    assert agreement.ord_alpha is not None and agreement.ord_alpha < 0.667
    assert agreement.status == "block"

    report = manager.get_scorer_reliability_report("cat-a")
    entry = next(a for a in report["agreements"] if a["other_scorer"] == "cat-b")
    assert entry["status"] == "block"
    assert report["aggregation"]["status"] == "blocked"


@pytest.mark.db
@pytest.mark.integration
def test_live_continuous_low_agreement_is_inconclusive_never_plain_score(
    live_conn: psycopg2.extensions.connection,
) -> None:
    _bulk_shared(live_conn, run_id="run-gov-low", item_prefix="low", n=3,
                 scorer_a="low-a", scorer_b="low-b", score_a=0.5, score_b=0.58)
    with live_conn.cursor() as cur:
        for i, (sa, sb) in enumerate(((0.6, 0.68), (0.7, 0.75)), start=2):
            cur.execute(
                "UPDATE hr.measurement SET score = %s WHERE measurement_id = %s",
                (sa, f"m-low-{i:04d}-a"),
            )
            cur.execute(
                "UPDATE hr.measurement SET score = %s WHERE measurement_id = %s",
                (sb, f"m-low-{i:04d}-b"),
            )
    live_conn.commit()

    agreement = ScorerCalibrationManager(live_conn).measure_agreement("low-a", "low-b")
    assert agreement.statistic == "icc21"
    assert agreement.icc21 == pytest.approx(170.0 / 221.0)
    assert agreement.status == "low"

    # downstream consumption: inconclusive, NEVER a plain score
    verdict = guarded_aggregate(
        status=agreement.status,
        statistic=agreement.icc21,
        interval=(agreement.ci_lo, agreement.ci_hi),
        values=[0.5, 0.6, 0.7],
    )
    assert verdict.status == "inconclusive"
    assert verdict.value is None

    report = ScorerCalibrationManager(live_conn).get_scorer_reliability_report("low-a")
    assert report["aggregation"]["status"] == "inconclusive"


@pytest.mark.db
@pytest.mark.integration
def test_live_drift_due_after_200_shared_items(
    live_conn: psycopg2.extensions.connection,
) -> None:
    seed_battery(live_conn, "driftb")
    seed_run(live_conn, "run-gov-d1", "sw-gov", MODEL_GOV, "battery-driftb")
    _bulk_shared(live_conn, run_id="run-gov-d1", item_prefix="d1", n=205,
                 scorer_a="d1-a", scorer_b="d1-b", score_a=0.8, score_b=0.8)

    drift = ScorerCalibrationManager(live_conn).detect_drift("d1-a")
    assert drift.new_shared_items == 205
    assert drift.trigger_due is True


@pytest.mark.db
@pytest.mark.integration
def test_live_drift_due_after_seven_days(
    live_conn: psycopg2.extensions.connection,
) -> None:
    seed_run(live_conn, "run-gov-d2", "sw-gov", MODEL_GOV, "battery-reasoning")
    _bulk_shared(live_conn, run_id="run-gov-d2", item_prefix="d2", n=5,
                 scorer_a="d2-a", scorer_b="d2-b", score_a=0.8, score_b=0.8,
                 backdate_days=10)

    drift = ScorerCalibrationManager(live_conn).detect_drift("d2-a")
    assert drift.new_shared_items == 0  # outside the 7-day comparison window
    assert drift.days_since_last_check is not None
    assert drift.days_since_last_check >= 10.0
    assert drift.trigger_due is True


@pytest.mark.db
@pytest.mark.integration
def test_live_measure_agreement_reproducible_across_runs(
    live_conn: psycopg2.extensions.connection,
) -> None:
    agreement1 = ScorerCalibrationManager(live_conn).measure_agreement("low-a", "low-b")
    agreement2 = ScorerCalibrationManager(live_conn).measure_agreement("low-a", "low-b")
    assert (
        agreement1.icc21,
        agreement1.ci_lo,
        agreement1.ci_hi,
    ) == (
        agreement2.icc21,
        agreement2.ci_lo,
        agreement2.ci_hi,
    )
    assert agreement1.ci_lo <= agreement1.icc21 <= agreement1.ci_hi


@pytest.mark.db
@pytest.mark.integration
def test_live_provenance_stage0_and_stage1_writer_never_unknown(
    live_conn: psycopg2.extensions.connection,
) -> None:
    import hr.stage0_storage as storage

    # stage1_loop is tracked/off-limits; it writes through this SAME shared
    # writer without scorer args, so exercising the writer default IS the
    # stage1 provenance path.
    seed_run(live_conn, "run-gov-prov", "sw-gov", MODEL_GOV, "battery-reasoning")
    storage._insert_measurement(
        live_conn, "m-prov-1", "run-gov-prov", "reasoning.q.001", 1,
        0.9, 10, 10, 10,
    )
    with live_conn.cursor() as cur:
        cur.execute(
            "SELECT scorer_name, scorer_version FROM hr.measurement "
            "WHERE measurement_id = %s",
            ("m-prov-1",),
        )
        row = cur.fetchone()
    # kind 'reasoning' resolves through _ROUTING -> constraint@1.0
    assert row == ("constraint", "1.0")

    # nothing written through the stage0/stage1 writer may be 'unknown'
    with live_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM hr.measurement WHERE scorer_name = 'unknown'"
        )
        unknown_count = cur.fetchone()[0]
    # the db-level DEFAULT 'unknown' only applies to legacy backfills; the
    # governed writer paths must never produce it
    assert unknown_count == 0


@pytest.mark.db
@pytest.mark.integration
def test_live_provenance_livebench_engine(
    live_conn: psycopg2.extensions.connection,
) -> None:
    from hr.bench.engine_results import BenchOutcome, ItemResult
    from hr.bench.engine_storage import EngineStorageMixin
    from hr.bench.livebench import LIVEBENCH_BATTERIES, battery_code
    from hr.graders.base import GRADER_VERSION

    seed_battery(live_conn, battery_code(LIVEBENCH_BATTERIES[0]))

    outcome = BenchOutcome(
        battery=LIVEBENCH_BATTERIES[0],
        model_id="prov-gov/livebench-model",
        score=100.0,
        passed=True,
        latency_ms=12,
        tokens_in=30,
        tokens_out=9,
        response_text="ok",
        thinking_text=None,
        requested_max_output=2048,
        items=[
            ItemResult(label="cg-1", item_id="livebench.cg.001", score=92.5, passed=True),
            ItemResult(label="cg-2", item_id="livebench.cg.002", score=100.0, passed=True),
        ],
    )
    EngineStorageMixin().store(live_conn, "sw-gov-bench", "prov-gov/livebench-model",
                               LIVEBENCH_BATTERIES[0], outcome)

    with live_conn.cursor() as cur:
        cur.execute(
            "SELECT scorer_name, scorer_version FROM hr.measurement "
            "WHERE measurement_id LIKE 'meas-%' "
            "AND item_id IN ('livebench.cg.001', 'livebench.cg.002') "
            "ORDER BY item_id"
        )
        rows = cur.fetchall()
    expected = f"livebench:{battery_code(LIVEBENCH_BATTERIES[0])}"
    assert rows == [(expected, GRADER_VERSION), (expected, GRADER_VERSION)]
    assert all(r[0] != "unknown" for r in rows)


@pytest.mark.db
@pytest.mark.integration
def test_live_hundred_scale_scores_normalized_before_icc(
    live_conn: psycopg2.extensions.connection,
) -> None:
    _bulk_shared(live_conn, run_id="run-gov-hun", item_prefix="hun", n=3,
                 scorer_a="hun-a", scorer_b="hun-b", score_a=50.0, score_b=58.0)
    with live_conn.cursor() as cur:
        cur.execute(
            "UPDATE hr.measurement SET score = 60.0 WHERE measurement_id = 'm-hun-0002-a'"
        )
        cur.execute(
            "UPDATE hr.measurement SET score = 68.0 WHERE measurement_id = 'm-hun-0002-b'"
        )
        cur.execute(
            "UPDATE hr.measurement SET score = 70.0 WHERE measurement_id = 'm-hun-0003-a'"
        )
        cur.execute(
            "UPDATE hr.measurement SET score = 75.0 WHERE measurement_id = 'm-hun-0003-b'"
        )
    live_conn.commit()

    agreement = ScorerCalibrationManager(live_conn).measure_agreement("hun-a", "hun-b")
    assert agreement.statistic == "icc21"
    # 0-100 scale normalized to 0-1 before ICC -> same value as the 0-1 twin
    assert agreement.icc21 == pytest.approx(170.0 / 221.0)
    assert agreement.status == "low"