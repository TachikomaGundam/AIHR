"""Empty-table contract tests for the HR SQL readers (hr-evolution T2).

A fresh module-scoped ``scratch_db`` with ZERO seeded rows: every reader
must return an empty result (or its documented error) instead of crashing.
The single documented exception is ``hr.decision.latest_sweep_id``, which
raises ``ValueError`` when ``hr.sweep`` has no rows.
"""
from __future__ import annotations

import psycopg2
import psycopg2.extensions
import pytest

from hr import decision
from hr import health
from hr.benchmark_banks import BenchmarkBankManager
from hr.recommend import RecommendationEngine
from hr.scorer_calibration import ScorerCalibrationManager
from tests._db_contracts_helpers import connect

pytestmark = [pytest.mark.db, pytest.mark.integration]


@pytest.fixture
def db_conn(scratch_db: tuple[str, str]) -> psycopg2.extensions.connection:
    _name, dsn = scratch_db
    conn = connect(dsn)
    yield conn
    conn.close()


def test_latest_sweep_id_on_empty_database_raises_documented_error(
    db_conn: psycopg2.extensions.connection,
) -> None:
    with pytest.raises(ValueError, match="no sweeps found in hr.sweep"):
        decision.latest_sweep_id(db_conn)


def test_decision_readers_on_empty_tables(
    db_conn: psycopg2.extensions.connection,
) -> None:
    assert decision.measurement_count(db_conn, "sw-none") == 0
    assert decision.capability_means(db_conn, "sw-none") == {}
    assert decision.battery_codes(db_conn) == []
    assert decision.seat_rows(db_conn) == {}
    assert decision.model_capabilities(db_conn) == {}
    assert decision.separation_probabilities(db_conn, "sw-none") == {}


def test_health_on_empty_sweep(
    db_conn: psycopg2.extensions.connection,
) -> None:
    report = health.compute_health("model-x", "sw-none", db_conn)
    assert report.n_measurements == 0
    assert "no measurements" in report.notes
    assert health.sweep_health(db_conn, "sw-none") == {}


def test_recommend_engine_on_empty_database(
    db_conn: psycopg2.extensions.connection,
) -> None:
    from hr.recommendation_constraints import RecommendationConstraints

    engine = RecommendationEngine()
    assert engine._sweep_id is None
    assert engine.recommend_for_task("anything") == []
    assert engine.recommend_with_constraints("anything", RecommendationConstraints()) == []


def test_benchmark_banks_on_empty_tables(
    db_conn: psycopg2.extensions.connection,
) -> None:
    manager = BenchmarkBankManager(db_conn)
    exposure = manager.get_item_exposure("nope")
    assert exposure.total_exposures == 0
    assert exposure.unique_models_exposed == 0
    assert exposure.last_exposed_at is None
    assert exposure.contamination_risk == 0.0
    assert exposure.is_safe_for_evaluation()
    assert manager.get_bank_version("nope") is None
    assert manager.get_safe_items_for_evaluation("nope", 5) == []


def test_scorer_calibration_on_empty_tables(
    db_conn: psycopg2.extensions.connection,
) -> None:
    manager = ScorerCalibrationManager(db_conn)
    agreement = manager.measure_agreement("scorer-a", "scorer-b")
    assert agreement.total_comparisons == 0
    assert agreement.agreement_rate == 0.0
    assert agreement.cohens_kappa is None
    drift = manager.detect_drift("scorer-a")
    assert drift.baseline_agreement == 0.0
    assert drift.current_agreement == 0.0
    assert drift.sample_count == 0
    assert not drift.has_significant_drift()
    report = manager.get_scorer_reliability_report("scorer-a")
    assert report["total_scores"] == 0
    assert report["agreements"] == []
    assert report["overall_reliable"] is False