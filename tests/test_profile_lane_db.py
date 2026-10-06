"""0.4.7 decision plane: profile readers = latest scored run per
(model, battery) across sweeps — a newer partial lane updates its own
batteries only and can never dethrone the rest (191 hijack incident)."""
from __future__ import annotations

from datetime import datetime, timezone

import psycopg2
import pytest

pytestmark = pytest.mark.usefixtures()  # scratch_db fixture drives DB gating


def test_profile_readers_prefer_latest_run_per_battery(scratch_db) -> None:
    from hr.decision import capability_means_profile, measurement_count_profile
    from tests._db_contracts_helpers import (
        seed_battery,
        seed_item_pool,
        seed_measurement,
        seed_provider_models,
        seed_run,
        seed_seat,
        seed_sweep,
    )

    _name, dsn = scratch_db
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        seed_provider_models(conn, ("m/prof",))
        seed_seat(conn, "oracle", "High-IQ consultant")
        seed_seat(conn, "_stage0_sweep", "stage0 sweep")
        b_reas = seed_battery(conn, "reasoning")
        b_speed = seed_battery(conn, "speed")
        seed_item_pool(conn, "p-i1", domain="reasoning")
        seed_sweep(conn, "sw-full")
        seed_sweep(conn, "sw-partial")
        # OLD full profile: reasoning 0.40, speed 0.90
        seed_run(conn, "run-f-r", "sw-full", "m/prof", b_reas)
        seed_measurement(conn, "mf-r1", "run-f-r", "p-i1", score=40.0)
        seed_run(conn, "run-f-s", "sw-full", "m/prof", b_speed)
        seed_measurement(conn, "mf-s1", "run-f-s", "p-i1", score=90.0)
        # NEWER partial lane: reasoning re-measured 100.0 only
        seed_run(conn, "run-p-r", "sw-partial", "m/prof", b_reas)
        seed_measurement(conn, "mp-r1", "run-p-r", "p-i1", score=100.0)
        with conn.cursor() as cur:
            t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
            t1 = datetime(2026, 6, 1, tzinfo=timezone.utc)
            cur.execute("UPDATE hr.run SET started_at = %s WHERE run_id LIKE 'run-f%%'", (t0,))
            cur.execute("UPDATE hr.run SET started_at = %s WHERE run_id = 'run-p-r'", (t1,))

        means = capability_means_profile(conn)["m/prof"]
        codes = set(means)
        by_code = {
            bc: means[bc] for bc in codes
        }
        # reasoning: latest lane wins; speed: kept from the full sweep (no hijack)
        assert by_code["reasoning"] == 100.0
        assert by_code["speed"] == 90.0
        assert measurement_count_profile(conn) == 2  # one row per latest run
    finally:
        conn.close()


def test_inconclusive_run_lands_incident_row(scratch_db) -> None:
    from hr.bench.engine_results import BenchOutcome, ItemResult
    from hr.bench.engine_storage import EngineStorageMixin
    from hr.models import BenchmarkCategory
    from tests._db_contracts_helpers import (
        seed_battery,
        seed_provider_models,
        seed_seat,
        seed_sweep,
    )

    _name, dsn = scratch_db
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        seed_provider_models(conn, ("m/inc",))
        seed_seat(conn, "oracle", "High-IQ consultant")
        seed_seat(conn, "_stage0_sweep", "stage0 sweep")
        from hr.bench.livebench import battery_code as _bc
        seed_battery(conn, _bc(BenchmarkCategory.tool_use))
        seed_sweep(conn, "sw-inc")
        outcome = BenchOutcome(
            battery=BenchmarkCategory.tool_use,
            model_id="m/inc",
            score=0.0,
            passed=False,
            status="inconclusive",
            items=[ItemResult(item_id="tool_use.001", label="tool_use",
                              passed=False, score=0.0)],
            latency_ms=29000,
            tokens_in=3589,
            tokens_out=1321,
            raw_output="INCONCLUSIVE: empty final answer after 1321 generated tokens (loop turns=6)",
        )
        EngineStorageMixin().store(conn, "sw-inc", "m/inc",
                                   BenchmarkCategory.tool_use, outcome)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT kind, details_json->>'note' FROM hr.infra_incident "
                "ORDER BY recorded_at DESC LIMIT 1"
            )
            row = cur.fetchone()
        # established convention: any inconclusive run = machine fault kind
        # adapter_setup_failure, incident row written, NO measurement injected
        assert row is not None and row[0] == "adapter_setup_failure"
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM hr.measurement m "
                        "JOIN hr.run r USING(run_id) WHERE r.sweep_id='sw-inc'")
            assert cur.fetchone()[0] == 0  # machine fault leaves no fake score
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM hr.run ORDER BY started_at DESC LIMIT 1")
            assert cur.fetchone()[0] == "inconclusive"
    finally:
        conn.close()
