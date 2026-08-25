"""Live-schema contract tests for the HR SQL *readers* (hr-evolution T2).

One module-scoped ``scratch_db`` database is seeded ONCE at module setup
with the full linked chain (provider -> models -> seat -> sweeps -> runs ->
measurements, plus bank items and scorer-provenance rows), then every test
asserts the production reader output for the slice it owns. The empty-table
failure paths live in ``tests/test_db_contracts_empty.py`` on a fresh
scratch DB.

Coverage targets: ``hr.health`` (compute_health + sweep_health),
``hr.decision`` (latest_sweep_id, measurement_count, capability_means,
battery_codes, seat_rows, model_capabilities, separation_probabilities,
seat_assignments), ``hr.recommend`` (RecommendationEngine round trip),
``hr.benchmark_banks`` (exposure/version/safe-items), ``hr.scorer_calibration``
(agreement, drift, reliability), and an explicit catalog check that every
column referenced by the covered SQL exists on the live schema.
"""
from __future__ import annotations

import json

import psycopg2
import psycopg2.extensions
import pytest

from hr import decision
from hr import health
from hr import recommend as recommend_mod
from hr.benchmark_banks import BenchmarkBankManager
from hr.recommend import RecommendationEngine
from hr.scorer_calibration import ScorerCalibrationManager
from tests._db_contracts_helpers import (
    columns,
    connect,
    scalar,
    seed_battery,
    seed_battery_item,
    seed_measurement,
    seed_provider_models,
    seed_run,
    seed_seat,
    seed_sweep,
)
from tests._db_contracts_helpers import seed_item_pool

pytestmark = [pytest.mark.db, pytest.mark.integration]

MODEL_A = "prov-a/model-a"
MODEL_B = "prov-a/model-b"
SCORER_PROBE = "prov-a/scorer-probe"


def _insert_item_pool_bank(conn: psycopg2.extensions.connection) -> None:
    for item_id, meta in {
        "b1": {"difficulty": "hard"},
        "b2": {"difficulty": "easy"},
        "b3": {"difficulty": "easy"},
        "b4": {"difficulty": "hard"},
        "b5": {"difficulty": "easy", "holdout": True},
    }.items():
        seed_item_pool(conn, item_id, domain="general", meta=meta)


def _seed_all(conn: psycopg2.extensions.connection) -> None:
    """One deterministic seed for the whole module: every linked row."""
    seed_provider_models(conn, (MODEL_A, MODEL_B, SCORER_PROBE))
    seed_seat(conn, "oracle", "High-IQ consultant")
    batteries = {
        code: seed_battery(conn, code)
        for code in ("reasoning", "livebench_speed", "code_gen", "instruction_follow", "tool_a")
    }
    seed_sweep(conn, "sw-a")
    seed_sweep(conn, "sw-b")
    seed_sweep(conn, "sw-cal")
    seed_sweep(conn, "sw-bank")
    seed_sweep(conn, "sw-seat")

    # -- sw-a: model-a across 5 batteries, model-b on reasoning only --------
    for item_id in ("h1", "h2"):
        seed_item_pool(conn, item_id, domain="reasoning", meta={"tier": 3})
    for item_id in ("c1", "c2"):
        seed_item_pool(conn, item_id, domain="tool_a", meta={"tier": 2})
    seed_run(conn, "run-a1", "sw-a", MODEL_A, batteries["reasoning"])
    for rep, item_id in ((1, "h1"), (2, "h1"), (1, "h2"), (2, "h2")):
        seed_measurement(conn, f"meas-a-{item_id}-{rep}", "run-a1", item_id, repetition=rep)
    # scorer-b re-scores the same c-items on the same run (repetition 2)
    seed_item_pool(conn, "h3", domain="livebench", meta={"tier": 3})
    seed_run(conn, "run-a2", "sw-a", MODEL_A, batteries["livebench_speed"])
    seed_measurement(conn, "meas-h3", "run-a2", "h3", score=0.9, tokens_out=600)
    seed_run(conn, "run-a3", "sw-a", MODEL_A, batteries["code_gen"])
    for item_id in ("i1", "i2"):
        seed_item_pool(conn, item_id, domain="code_gen", meta={"tier": 2})
        seed_measurement(conn, f"meas-{item_id}", "run-a3", item_id, score=0.75, tokens_out=500)
    seed_run(conn, "run-a4", "sw-a", MODEL_A, batteries["instruction_follow"])
    for item_id in ("j1", "j2"):
        seed_item_pool(conn, item_id, domain="instruction_follow", meta={"tier": 2})
        seed_measurement(conn, f"meas-{item_id}", "run-a4", item_id, score=0.7, tokens_out=400)
    seed_run(conn, "run-a5", "sw-a", MODEL_A, batteries["tool_a"])
    seed_item_pool(conn, "k1", domain="tool_a", meta={"tier": 2})
    seed_measurement(conn, "meas-k1", "run-a5", "k1", score=0.95, tokens_out=300)
    # model-b on reasoning with a failed run (success-rate path)
    seed_run(conn, "run-b1", "sw-a", MODEL_B, batteries["reasoning"], status="failed", failure_reason="timeout")
    for item_id in ("m1", "m2"):
        seed_item_pool(conn, item_id, domain="reasoning", meta={"tier": 3})
        seed_measurement(conn, f"meas-{item_id}", "run-b1", item_id, score=0.5, tokens_out=400)

    # -- sw-b: a smaller sweep (latest_sweep_id ordering) -------------------
    seed_run(conn, "run-b2", "sw-b", MODEL_B, batteries["reasoning"])
    seed_measurement(conn, "meas-n1", "run-b2", "m1", repetition=2, score=0.6)

    # -- sw-cal: scorer provenance rows on their OWN model ------------------
    seed_run(conn, "run-sc", "sw-cal", SCORER_PROBE, batteries["reasoning"])
    seed_measurement(
        conn, "meas-sc1a", "run-sc", "c1", repetition=1, score=0.8,
        scorer_name="scorer-a", scorer_version="1.0",
    )
    seed_measurement(
        conn, "meas-sc1b", "run-sc", "c1", repetition=2, score=0.8,
        scorer_name="scorer-b", scorer_version="1.0",
    )
    seed_measurement(
        conn, "meas-sc2a", "run-sc", "c2", repetition=1, score=0.8,
        scorer_name="scorer-a", scorer_version="1.0",
    )
    seed_measurement(
        conn, "meas-sc2b", "run-sc", "c2", repetition=2, score=0.4,
        scorer_name="scorer-b", scorer_version="1.0",
    )

    # -- sw-bank: exposure-tracked bank items ------------------------------
    bank_battery = seed_battery(conn, "bankcode")
    _insert_item_pool_bank(conn)
    for position, item_id in enumerate(("b1", "b2", "b3", "b4", "b5")):
        seed_battery_item(conn, bank_battery, item_id, position)
    seed_run(conn, "run-bk1", "sw-bank", MODEL_A, bank_battery)
    seed_run(conn, "run-bk2", "sw-bank", MODEL_B, bank_battery)
    seed_measurement(conn, "meas-b1a", "run-bk1", "b1", repetition=1, score=0.8, response_text="The answer is 1.")
    seed_measurement(conn, "meas-b1b", "run-bk1", "b1", repetition=2, score=0.7, response_text="The answer is 2.")
    seed_measurement(conn, "meas-b1c", "run-bk2", "b1", repetition=1, score=0.8, response_text="The answer is 3.")
    seed_measurement(conn, "meas-b2", "run-bk1", "b2", repetition=1, score=0.5, response_text="The answer is 4.")

    # -- sw-seat: health-clean rows for the seat-assignment verdict ---------
    seed_run(conn, "run-st1", "sw-seat", MODEL_A, batteries["reasoning"])
    seed_run(conn, "run-st2", "sw-seat", MODEL_A, batteries["livebench_speed"])
    seed_run(conn, "run-st3", "sw-seat", MODEL_A, batteries["tool_a"])
    for rep, item_id in ((1, "s1"), (2, "s1"), (1, "s2"), (2, "s2")):
        seed_item_pool(conn, item_id, domain="reasoning", meta={"tier": 3})
        seed_measurement(conn, f"meas-st-{item_id}-{rep}", "run-st1", item_id, repetition=rep, score=0.8)
    seed_item_pool(conn, "s3", domain="livebench", meta={"tier": 3})
    seed_measurement(conn, "meas-st3", "run-st2", "s3", score=0.9)
    seed_item_pool(conn, "s4", domain="tool_a", meta={"tier": 2})
    seed_measurement(conn, "meas-st4", "run-st3", "s4", score=0.95)
    seed_run(conn, "run-st5", "sw-seat", MODEL_B, batteries["livebench_speed"])
    seed_run(conn, "run-st6", "sw-seat", MODEL_B, batteries["tool_a"])
    seed_item_pool(conn, "s5", domain="livebench", meta={"tier": 3})
    seed_item_pool(conn, "s6", domain="tool_a", meta={"tier": 2})
    seed_measurement(conn, "meas-st5", "run-st5", "s5", score=0.6)
    seed_measurement(conn, "meas-st6", "run-st6", "s6", score=0.55)

    # context-window capabilities: model-a passes the oracle ctx gate,
    # model-b does not (hard-gate elimination evidence).
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE hr.model SET capabilities = %s::jsonb WHERE model_id = %s",
            (json.dumps({"context_window": 300000}), MODEL_A),
        )
        cur.execute(
            "UPDATE hr.model SET capabilities = %s::jsonb WHERE model_id = %s",
            (json.dumps({"context_window": 50000}), MODEL_B),
        )
    conn.commit()


@pytest.fixture(scope="module")
def db_conn(scratch_db: tuple[str, str]) -> psycopg2.extensions.connection:
    _name, dsn = scratch_db
    conn = connect(dsn)
    _seed_all(conn)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# hr.decision — canonical measurement queries
# ---------------------------------------------------------------------------


def test_latest_sweep_id_returns_sweep_with_most_measurements(
    db_conn: psycopg2.extensions.connection,
) -> None:
    assert decision.latest_sweep_id(db_conn) == "sw-a"
    assert decision.measurement_count(db_conn, "sw-a") == 12
    assert decision.measurement_count(db_conn, "sw-b") == 1
    assert decision.measurement_count(db_conn, "sw-none") == 0


def test_capability_means_and_battery_codes(
    db_conn: psycopg2.extensions.connection,
) -> None:
    means = decision.capability_means(db_conn, "sw-a")
    assert set(means) == {MODEL_A, MODEL_B}
    assert means[MODEL_A]["reasoning"] == pytest.approx(0.8)
    assert means[MODEL_A]["livebench_speed"] == pytest.approx(0.9)
    assert means[MODEL_A]["code_gen"] == pytest.approx(0.75)
    assert means[MODEL_A]["instruction_follow"] == pytest.approx(0.7)
    assert means[MODEL_A]["tool_a"] == pytest.approx(0.95)
    assert means[MODEL_B]["reasoning"] == pytest.approx(0.5)
    codes = decision.battery_codes(db_conn)
    assert codes == sorted(codes) and "reasoning" in codes and "bankcode" in codes


def test_seat_rows_and_model_capabilities(
    db_conn: psycopg2.extensions.connection,
) -> None:
    seats = decision.seat_rows(db_conn)
    assert seats["oracle"]["seat_code"] == "oracle"
    assert seats["oracle"]["ctx_p95"] == 262144
    caps = decision.model_capabilities(db_conn)
    assert caps[MODEL_A]["context_window"] == 300000
    assert caps[MODEL_B]["context_window"] == 50000


def test_separation_probabilities_filters_directional(
    db_conn: psycopg2.extensions.connection,
) -> None:
    from hr.stage0_storage import _insert_separation

    _insert_separation(
        db_conn, "sep-dir", "sw-a", "battery-reasoning", MODEL_A, MODEL_B,
        p_separated=0.9, p_weak=0.05, p_tie=0.05, directional=True,
    )
    _insert_separation(
        db_conn, "sep-flat", "sw-b", "battery-reasoning", MODEL_A, MODEL_B,
        p_separated=0.5, p_weak=0.4, p_tie=0.1, directional=False,
    )
    probs = decision.separation_probabilities(db_conn, "sw-a")
    assert probs == {"reasoning": {(MODEL_A, MODEL_B): pytest.approx(0.9)}}
    assert decision.separation_probabilities(db_conn, "sw-b") == {}


def test_seat_assignments_primary_and_elimination(
    db_conn: psycopg2.extensions.connection,
) -> None:
    means = decision.capability_means(db_conn, "sw-seat")
    assert set(means) == {MODEL_A, MODEL_B}
    for battery in ("reasoning", "livebench_speed", "tool_a"):
        assert means[MODEL_A][battery] == pytest.approx(
            {"reasoning": 0.8, "livebench_speed": 0.9, "tool_a": 0.95}[battery]
        )
    assert means[MODEL_B]["livebench_speed"] == pytest.approx(0.6)
    # reports={} defuses the health tie-break, which is pinned separately in
    # test_health_rank_score_with_seat_multiplies_token_efficiency (live rows
    # carry Decimal token_efficiency; health_gates.py:190 divides by float).
    assignments = decision.seat_assignments(
        pool={MODEL_A, MODEL_B},
        means=means,
        reports={},
        seat_db=decision.seat_rows(db_conn),
        caps_db=decision.model_capabilities(db_conn),
        codes=decision.battery_codes(db_conn),
        retired_set=set(),
        include_retired=False,
        separations={},
    )
    by_seat = {assignment["seat_code"]: assignment for assignment in assignments}
    oracle = by_seat["oracle"]
    assert oracle["primary"] == MODEL_A
    assert (MODEL_B, "context_window(50000) < seat p95 (262144)") in oracle["eliminated"]
    assert len(assignments) == 18  # every SEAT_CODES seat gets a row


@pytest.mark.xfail(
    strict=True,
    reason=(
        "hr/seats/health_gates.py:190 divides Decimal token_efficiency by "
        "1000.0 — live NUMERIC/INTEGER rows produce Decimal efficiency while "
        "unit fakes use floats, so the seat-weighted tie-break crashes only "
        "against real PostgreSQL"
    ),
)
def test_health_rank_score_with_seat_multiplies_token_efficiency(
    db_conn: psycopg2.extensions.connection,
) -> None:
    from hr.seats.health_gates import health_rank_score

    report = health.compute_health(MODEL_A, "sw-a", db_conn)
    assert report.token_efficiency is not None
    health_rank_score(report, "oracle")


# ---------------------------------------------------------------------------
# hr.health — compute_health + sweep_health
# ---------------------------------------------------------------------------


def test_compute_health_metrics_from_seeded_measurements(
    db_conn: psycopg2.extensions.connection,
) -> None:
    report = health.compute_health(MODEL_A, "sw-a", db_conn)
    assert report.n_measurements == 10
    assert report.loop_mean == pytest.approx(0.0)
    assert report.truncation_rate == pytest.approx(0.0)
    assert float(report.token_efficiency) == pytest.approx(4700 / 7.95)
    # Consistency is UNMEASURED on live rows (prod-bug pin): NUMERIC scores
    # arrive as Decimal, which _self_consistency rejects via
    # isinstance(score, (int, float)) at hr/health_metrics.py:131-132 —
    # reported to the orchestrator; fix breaks this pin deliberately.
    assert report.consistency_mean_range is None
    assert report.consistency_unanimity_pct is None
    assert report.answer_completion_rate == pytest.approx(1.0)
    assert "no measurements" not in report.notes


def test_sweep_health_full_pool_with_battery_breakdown(
    db_conn: psycopg2.extensions.connection,
) -> None:
    reports = health.sweep_health(db_conn, "sw-a")
    assert set(reports) == {MODEL_A, MODEL_B}
    assert reports[MODEL_B].n_measurements == 2
    breakdown = reports[MODEL_A].battery_breakdown
    assert breakdown is not None
    assert {row["battery_code"] for row in breakdown} == {
        "reasoning", "livebench_speed", "code_gen", "instruction_follow", "tool_a",
    }
    reasoning = next(row for row in breakdown if row["battery_code"] == "reasoning")
    # the breakdown is sweep-wide (both models): 4 reps from model-a (h1/h2
    # x2) + 2 from model-b (m1/m2) -> mean (0.8*4 + 0.5*2) / 6 = 0.7
    assert reasoning["n_measurements"] == 6
    assert reasoning["mean_score"] == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# hr.recommend — RecommendationEngine round trip
# ---------------------------------------------------------------------------


def test_recommend_engine_round_trip(
    db_conn: psycopg2.extensions.connection,
) -> None:
    engine = RecommendationEngine()
    assert engine._sweep_id == "sw-a"
    recommendations = engine.recommend_for_task(
        "write a function and debug the math logic"
    )
    assert recommendations and recommendations[0][0] == MODEL_A
    score = recommendations[0][1]
    assert score == pytest.approx(0.75)  # mean of reasoning/code_gen/instruction_follow
    assert engine._get_sweep_created_at() is not None
    assert engine._get_success_rates() == {MODEL_A: 1.0, MODEL_B: 0.0}
    assert engine._get_latency_stats()[MODEL_A]["p50"] == pytest.approx(250.0)
    assert engine._get_separation_probabilities() == {MODEL_A: 0.9, MODEL_B: 0.9}


def test_recommend_with_constraints_defaults_pass(
    db_conn: psycopg2.extensions.connection,
) -> None:
    from hr.recommendation_constraints import RecommendationConstraints

    engine = RecommendationEngine()
    results = engine.recommend_with_constraints(
        "write a function and debug the math logic", RecommendationConstraints()
    )
    assert results and results[0][0] == MODEL_A
    assert results[0][2] == []  # no constraint failures with default policy


# ---------------------------------------------------------------------------
# hr.benchmark_banks — exposure, versions, safe-item selection
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "hr/benchmark_banks.py:86 unpacks the 4-column exposure SELECT "
        "(item_id, total, unique, last) into 3 variables — get_item_exposure "
        "raises ValueError against any non-empty measurement set; production "
        "bug exposed by the live scratch-DB seed, reported to the orchestrator"
    ),
)
def test_bank_item_exposure_counts_measurements(
    db_conn: psycopg2.extensions.connection,
) -> None:
    manager = BenchmarkBankManager(db_conn)
    exposure = manager.get_item_exposure("b1")
    assert exposure.total_exposures == 3
    assert exposure.unique_models_exposed == 2
    assert exposure.last_exposed_at is not None
    assert exposure.contamination_risk == pytest.approx(0.03)
    assert exposure.is_safe_for_evaluation()
    assert not exposure.is_safe_for_evaluation(max_exposures=3)
    assert manager.get_item_exposure("b3").total_exposures == 0


def test_bank_version_and_stratification(
    db_conn: psycopg2.extensions.connection,
) -> None:
    manager = BenchmarkBankManager(db_conn)
    version = manager.get_bank_version("bankcode")
    assert version is not None
    assert (version.bank_code, version.version) == ("bankcode", "v1")
    assert version.item_count == 5
    assert version.difficulty_distribution == {"hard": 2, "easy": 3}
    assert version.holdout_count == 1
    assert version.created_at  # timestamp round-trips as text
    assert manager.get_bank_version("bankcode", "v1") == version
    assert manager.get_bank_version("bankcode", "v9") is None
    assert manager.get_bank_version("nope") is None


def test_bank_safe_items_exclude_overexposed_and_holdout(
    db_conn: psycopg2.extensions.connection,
) -> None:
    manager = BenchmarkBankManager(db_conn)
    items = manager.get_safe_items_for_evaluation("bankcode", 3, max_exposures=2)
    assert set(items) == {"b2", "b3", "b4"}  # b1 overexposed, b5 holdout


# ---------------------------------------------------------------------------
# hr.scorer_calibration — agreement, drift, reliability
# ---------------------------------------------------------------------------


def test_scorer_agreement_live_round_trip(
    db_conn: psycopg2.extensions.connection,
) -> None:
    manager = ScorerCalibrationManager(db_conn)
    agreement = manager.measure_agreement("scorer-a", "scorer-b", "sw-cal")
    assert agreement.total_comparisons == 2
    assert agreement.agreement_count == 1
    assert agreement.agreement_rate == pytest.approx(0.5)
    assert agreement.cohens_kappa == pytest.approx(0.0)
    assert not agreement.is_acceptable()
    # sweep-less variant sees the same pair rows
    assert manager.measure_agreement("scorer-a", "scorer-b").total_comparisons == 2
    # missing scorer: empty result contract (0 comparisons, None kappa)
    ghost = manager.measure_agreement("scorer-a", "ghost")
    assert ghost.total_comparisons == 0
    assert ghost.agreement_rate == 0.0
    assert ghost.cohens_kappa is None


def test_scorer_drift_and_reliability_report(
    db_conn: psycopg2.extensions.connection,
) -> None:
    manager = ScorerCalibrationManager(db_conn)
    drift = manager.detect_drift("scorer-a")
    # comparison window (last 7d) holds the seeded rows; baseline is empty
    assert drift.current_agreement == pytest.approx(0.5)
    assert drift.baseline_agreement == 0.0
    assert drift.drift_magnitude == pytest.approx(0.5)
    assert drift.sample_count == 2
    assert drift.has_significant_drift()

    report = manager.get_scorer_reliability_report("scorer-a")
    assert report["total_scores"] == 2
    assert report["mean_score"] == pytest.approx(0.8)
    assert report["drift"]["significant"] is True
    assert report["overall_reliable"] is False
    agreements = {item["other_scorer"]: item for item in report["agreements"]}
    assert "scorer-b" in agreements
    assert agreements["scorer-b"]["agreement_rate"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Catalog consistency: every referenced column exists on the live schema
# ---------------------------------------------------------------------------


# The columns each covered module's SQL reads (audited from the SQL strings).
REFERENCED_COLUMNS: dict[str, set[str]] = {
    "provider": {"provider_id", "name"},
    "model": {"model_id", "provider_fk", "model_name", "capabilities"},
    "control_model": {"provider_fk", "model_id"},
    "seat": {"seat_code", "required_capabilities", "ctx_p95_tokens"},
    "item_pool": {"item_id", "domain", "json_meta"},
    "battery": {"battery_id", "battery_code", "version", "created_at"},
    "battery_item": {"battery_id", "item_id"},
    "sweep": {"sweep_id", "seat_code", "purpose", "created_at"},
    "run": {"run_id", "sweep_id", "model_id", "battery_id", "status", "failure_reason", "finished_at"},
    "measurement": {
        "measurement_id", "run_id", "item_id", "repetition", "score", "tokens_in",
        "tokens_out", "latency_ms", "response_text", "requested_max_output",
        "scorer_name", "scorer_version", "created_at",
    },
    "infra_incident": {"incident_id", "run_id", "kind", "details_json"},
    "separation": {
        "separation_id", "sweep_id", "battery_id", "model_a", "model_b",
        "p_separated", "p_weak", "p_tie", "directional",
    },
    "calibration_event": {
        "event_id", "item_id", "kind", "pool_hash", "anchor", "battery", "tier",
        "item_type", "score", "passed", "tokens_in", "tokens_out", "latency_ms",
        "infra_failure", "evidence_json",
    },
    "experiment_manifest": {"sweep_id", "manifest_json", "digest"},
}


def test_referenced_columns_exist_in_live_schema(
    db_conn: psycopg2.extensions.connection,
) -> None:
    live = columns(db_conn)
    for table, referenced in REFERENCED_COLUMNS.items():
        assert table in live, f"table hr.{table} missing from live schema"
        missing = referenced - live[table]
        assert not missing, f"hr.{table} missing columns referenced by SQL: {sorted(missing)}"