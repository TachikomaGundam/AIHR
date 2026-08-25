"""Live-schema contract tests for the HR SQL *writers* (hr-evolution T2).

Every test runs against the shared module-scoped ``scratch_db`` fixture (a
fresh ``hr_test_*`` Postgres database initialized with :func:`hr.db.
init_schema`). Coverage targets:

* ``hr.stage0_storage`` — every production writer round-trips on the live
  schema (happy path), and conflict/FK/invalid-value paths document the
  real error contract (no-op upserts, ForeignKeyViolation on bad FKs, no
  DB-level CHECK on ``run.status``).
* ``hr.schema_migration`` + ``hr.db._run_migrations`` — idempotent re-runs
  are a no-op; the migration correctly adds columns to PRE-column tables
  (simulated by dropping the migrated columns and re-running).
* ``hr.calibration_persistence`` — ``_persist``/``_load_recorded_pairs``
  round-trip on the live schema, including the corrupt-provenance path.
* The live schema table set matches ``EXPECTED_TABLES`` exactly (the
  authoritative information_schema list, drift-locked).

Offline runs skip via the shared fixture; nothing here weakens existing
unit tests.
"""
from __future__ import annotations

import json
import hashlib

import psycopg2
import psycopg2.extensions
import pytest

import hr.stage0_storage as storage
from hr.calibration_models import CalibrationReport, Measurement
from hr.calibration_persistence import CalibrationPersistenceMixin
from hr.db import _run_migrations
from hr.db import migrate_add_calibration_measurement_columns
from hr.db import migrate_add_directional_separation
from hr.db import migrate_add_measurement_cap_column
from hr.db import migrate_add_response_columns
from hr.schema_migration import migrate_measurement_scorer_columns
from hr.schema_migration import migrate_run_status_columns
from tests._db_contracts_helpers import (
    columns,
    connect,
    scalar,
    seed_battery,
    seed_battery_item,
    seed_control_model,
    seed_envelope_item,
    seed_measurement,
    seed_provider_models,
    seed_run,
    seed_seat,
    seed_sweep,
)
from tests.test_db import EXPECTED_TABLES

pytestmark = [pytest.mark.db, pytest.mark.integration]

MODEL_A = "prov-a/model-a"
MODEL_B = "prov-a/model-b"


@pytest.fixture
def db_conn(scratch_db: tuple[str, str]) -> psycopg2.extensions.connection:
    _name, dsn = scratch_db
    conn = connect(dsn)
    yield conn
    conn.close()


def _seed_chain(
    conn: psycopg2.extensions.connection,
    *,
    sweep_id: str = "sw-a",
    models: tuple[str, ...] = (MODEL_A, MODEL_B),
) -> None:
    """The linked provider -> model -> seat -> sweep -> run -> measurement chain."""
    seed_provider_models(conn, models)
    seed_control_model(conn, "prov-a", MODEL_A)
    seed_seat(conn, "oracle", "High-IQ consultant")
    seed_seat(conn, "_stage0_sweep", "stage0 sweep")
    seed_envelope_item(conn, "tool_a.calc.001")
    battery = seed_battery(conn, "reasoning")
    seed_battery_item(conn, battery, "tool_a.calc.001", 0)
    seed_sweep(conn, sweep_id)
    seed_run(conn, "run-1", sweep_id, MODEL_A, battery, status="failed", failure_reason="boom")
    seed_measurement(conn, "meas-1", "run-1", "tool_a.calc.001", score=0.75)


# ---------------------------------------------------------------------------
# hr.stage0_storage — writers round trip
# ---------------------------------------------------------------------------


def test_stage0_storage_writers_round_trip_on_live_schema(
    db_conn: psycopg2.extensions.connection,
) -> None:
    _seed_chain(db_conn)

    # provider + models (composite "provider/model_id" PK convention)
    assert scalar(db_conn, "SELECT name FROM hr.provider WHERE provider_id = 'prov-a'") == "prov-a"
    assert scalar(
        db_conn, "SELECT provider_fk FROM hr.model WHERE model_id = %s", (MODEL_A,)
    ) == "prov-a"
    assert scalar(
        db_conn, "SELECT model_name FROM hr.model WHERE model_id = %s", (MODEL_A,)
    ) == "model-a"
    # capabilities default to '{}'::jsonb — parsed back as a dict
    caps = scalar(db_conn, "SELECT capabilities FROM hr.model WHERE model_id = %s", (MODEL_A,))
    assert caps == {}

    # control_model row (PK = provider_fk, model FK resolved)
    assert scalar(
        db_conn,
        "SELECT model_id FROM hr.control_model WHERE provider_fk = 'prov-a'",
    ) == MODEL_A

    # yaml seat row carries the full typed shape from seats.yaml
    seat = scalar(
        db_conn,
        "SELECT seat_name FROM hr.seat WHERE seat_code = 'oracle'",
    )
    assert isinstance(seat, str) and seat  # "High-IQ consultant (…)" from seats.yaml

    # envelope item_pool row: domain derived from the dotted item key
    pool = scalar(
        db_conn,
        "SELECT domain FROM hr.item_pool WHERE item_id = 'tool_a.calc.001'",
    )
    assert pool == "tool_a"

    # battery / battery_item / sweep / run / measurement / incident / separation
    assert scalar(db_conn, "SELECT battery_code FROM hr.battery WHERE battery_id = 'battery-reasoning'") == "reasoning"
    assert scalar(
        db_conn,
        "SELECT position FROM hr.battery_item WHERE battery_id = 'battery-reasoning' AND item_id = 'tool_a.calc.001'",
    ) == 0
    assert scalar(db_conn, "SELECT seat_code FROM hr.sweep WHERE sweep_id = 'sw-a'") == "oracle"
    assert scalar(db_conn, "SELECT purpose FROM hr.sweep WHERE sweep_id = 'sw-a'") == "primary"
    with db_conn.cursor() as cur:
        cur.execute("SELECT status, failure_reason FROM hr.run WHERE run_id = 'run-1'")
        assert cur.fetchone() == ("failed", "boom")
    assert scalar(db_conn, "SELECT score FROM hr.measurement WHERE measurement_id = 'meas-1'") == 0.75
    # infra incident with JSONB details round-trips
    storage._insert_infra_incident(db_conn, "run-1", "timeout", {"code": 408})
    assert scalar(
        db_conn,
        "SELECT details_json->>'code' FROM hr.infra_incident WHERE run_id = 'run-1'",
    ) == "408"
    # separation row: CHECK (model_a <> model_b) satisfied, directional default TRUE
    storage._insert_separation(
        db_conn, "sep-1", "sw-a", "battery-reasoning", MODEL_A, MODEL_B,
        p_separated=0.9, p_weak=0.05, p_tie=0.05,
    )
    row = scalar(
        db_conn,
        "SELECT directional FROM hr.separation WHERE separation_id = 'sep-1'",
    )
    assert row is True


def test_stage0_storage_upsert_conflicts_are_noops(
    db_conn: psycopg2.extensions.connection,
) -> None:
    _seed_chain(db_conn)
    # Re-upsert with different values: ON CONFLICT DO NOTHING preserves rows.
    storage._upsert_provider(db_conn, "prov-a", "Renamed")
    assert scalar(db_conn, "SELECT name FROM hr.provider WHERE provider_id = 'prov-a'") == "prov-a"
    storage._upsert_model(db_conn, MODEL_A, "prov-a", "renamed-model")
    assert scalar(db_conn, "SELECT model_name FROM hr.model WHERE model_id = %s", (MODEL_A,)) == "model-a"
    storage._insert_sweep(db_conn, "sw-a", "oracle", "changed")
    assert scalar(db_conn, "SELECT purpose FROM hr.sweep WHERE sweep_id = 'sw-a'") == "primary"
    # duplicate measurement (same run/item/repetition) is a no-op
    seed_measurement(db_conn, "meas-dup", "run-1", "tool_a.calc.001", score=0.1)
    assert scalar(
        db_conn, "SELECT COUNT(*) FROM hr.measurement WHERE run_id = 'run-1' AND item_id = 'tool_a.calc.001'"
    ) == 1
    # pseudo-seat fallback path is also ON CONFLICT DO NOTHING
    storage._upsert_seat(db_conn, "_stage0_sweep", "changed name")
    assert scalar(db_conn, "SELECT seat_name FROM hr.seat WHERE seat_code = '_stage0_sweep'") == "stage0 sweep"


def test_stage0_storage_invalid_fk_and_status_contract(
    db_conn: psycopg2.extensions.connection,
) -> None:
    _seed_chain(db_conn)
    # Invalid FK: psycopg2 ForeignKeyViolation is the documented writer contract.
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        storage._insert_measurement(
            db_conn, "meas-bad", "run-does-not-exist", "tool_a.calc.001", 1, 0.5,
            tokens_in=1, tokens_out=1, latency_ms=1,
        )
    db_conn.rollback()
    # run.status is a plain TEXT column: the SCHEMA does not constrain values
    # (validation lives at call sites). Assert the live contract explicitly.
    storage._insert_run(
        db_conn, "run-bogus", "sw-a", MODEL_A, "battery-reasoning",
        round_num=1, total_tokens=1, total_cost_cny=0.0, infra_ok=True,
        status="bogus-status",
    )
    assert scalar(db_conn, "SELECT status FROM hr.run WHERE run_id = 'run-bogus'") == "bogus-status"


def test_ensure_provider_model_records_splits_provider_slug(
    db_conn: psycopg2.extensions.connection,
) -> None:
    mapping = storage._ensure_provider_model_records(
        db_conn, ("prov-a/model-a", "bare-model")
    )
    assert mapping == {"prov-a/model-a": "prov-a", "bare-model": "unknown"}
    assert scalar(db_conn, "SELECT model_name FROM hr.model WHERE model_id = 'bare-model'") == "bare-model"
    assert scalar(db_conn, "SELECT provider_id FROM hr.provider WHERE provider_id = 'unknown'") == "unknown"


def test_stage0_storage_sanitizes_control_bytes_in_response_text(
    db_conn: psycopg2.extensions.connection,
) -> None:
    _seed_chain(db_conn)
    seed_measurement(
        db_conn, "meas-nul", "run-1", "tool_a.calc.001",
        repetition=2, score=0.5, response_text="a\x00b\tc",
    )
    stored = scalar(
        db_conn, "SELECT response_text FROM hr.measurement WHERE measurement_id = 'meas-nul'"
    )
    assert stored == "ab\tc"


def test_live_schema_table_set_matches_expected_tables(
    db_conn: psycopg2.extensions.connection,
) -> None:
    """The live schema is EXACTLY the authoritative EXPECTED_TABLES set.

    This is the drift lock for the 20-vs-19 fix: init_schema() must produce
    precisely the tables the catalog expects (''experiment_manifest'' is the
    20th, created by the base DDL, not only by a later migration).
    """
    live = set(columns(db_conn))
    assert set(EXPECTED_TABLES) == live
    assert len(EXPECTED_TABLES) == 20


def test_experiment_manifest_round_trip(
    db_conn: psycopg2.extensions.connection,
) -> None:
    """The 20th table carries the evaluation-configuration snapshot."""
    _seed_chain(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hr.experiment_manifest (sweep_id, manifest_json, digest) "
            "VALUES (%s, %s::jsonb, %s)",
            ("sw-a", json.dumps({"adapter": "openai-compat", "model": MODEL_A}), "sha256:abc"),
        )
    db_conn.commit()
    assert scalar(
        db_conn, "SELECT digest FROM hr.experiment_manifest WHERE sweep_id = 'sw-a'"
    ) == "sha256:abc"
    assert scalar(
        db_conn, "SELECT manifest_json->>'adapter' FROM hr.experiment_manifest WHERE sweep_id = 'sw-a'"
    ) == "openai-compat"


# ---------------------------------------------------------------------------
# hr.schema_migration / hr.db migration pipeline
# ---------------------------------------------------------------------------


def test_migration_rerun_is_a_noop(
    db_conn: psycopg2.extensions.connection,
) -> None:
    """init_schema already ran; re-running the migration pipeline changes nothing."""
    before = columns(db_conn)
    _run_migrations(db_conn)
    assert columns(db_conn) == before
    # the post-migration columns exist on the live schema
    run_cols = before["run"]
    assert {"status", "failure_reason"} <= run_cols
    meas_cols = before["measurement"]
    assert {
        "response_text",
        "thinking_text",
        "requested_max_output",
        "scorer_name",
        "scorer_version",
    } <= meas_cols
    assert {
        "pool_hash",
        "anchor",
        "battery",
        "tier",
        "item_type",
        "score",
        "passed",
        "tokens_in",
        "tokens_out",
        "latency_ms",
        "infra_failure",
    } <= before["calibration_event"]
    assert "directional" in before["separation"]


def test_migration_adds_columns_to_pre_column_tables(
    db_conn: psycopg2.extensions.connection,
) -> None:
    """Migrations work on tables created WITHOUT the post-migration columns.

    Simulates a pre-migration database: the columns are dropped, legacy rows
    inserted with the OLD shape, then each migration is re-run — columns
    appear and legacy rows are backfilled with the documented defaults.
    """
    _seed_chain(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE hr.run DROP COLUMN IF EXISTS status, "
            "DROP COLUMN IF EXISTS failure_reason"
        )
        cur.execute(
            "ALTER TABLE hr.measurement DROP COLUMN IF EXISTS scorer_name, "
            "DROP COLUMN IF EXISTS scorer_version, DROP COLUMN IF EXISTS response_text, "
            "DROP COLUMN IF EXISTS thinking_text, DROP COLUMN IF EXISTS requested_max_output"
        )
        cur.execute(
            "ALTER TABLE hr.calibration_event DROP COLUMN IF EXISTS pool_hash, "
            "DROP COLUMN IF EXISTS anchor, DROP COLUMN IF EXISTS battery, "
            "DROP COLUMN IF EXISTS tier, DROP COLUMN IF EXISTS item_type, "
            "DROP COLUMN IF EXISTS score, DROP COLUMN IF EXISTS passed, "
            "DROP COLUMN IF EXISTS tokens_in, DROP COLUMN IF EXISTS tokens_out, "
            "DROP COLUMN IF EXISTS latency_ms, DROP COLUMN IF EXISTS infra_failure"
        )
        cur.execute("ALTER TABLE hr.separation DROP COLUMN IF EXISTS directional")
        # legacy rows in the PRE-column shape
        cur.execute(
            "INSERT INTO hr.run (run_id, sweep_id, model_id, battery_id) "
            "VALUES ('run-legacy', 'sw-a', %s, 'battery-reasoning')",
            (MODEL_A,),
        )
        cur.execute(
            "INSERT INTO hr.measurement (measurement_id, run_id, item_id, repetition, score) "
            "VALUES ('meas-legacy', 'run-legacy', 'tool_a.calc.001', 1, 0.5)"
        )
        cur.execute(
            "INSERT INTO hr.calibration_event (event_id, item_id, kind) "
            "VALUES ('cal-legacy', 'tool_a.calc.001', 'anchor_measurement')"
        )
        cur.execute(
            "INSERT INTO hr.separation (separation_id, sweep_id, battery_id, model_a, model_b, "
            "p_separated, p_weak, p_tie) "
            "VALUES ('sep-legacy', 'sw-a', 'battery-reasoning', %s, %s, 0.8, 0.1, 0.1)",
            (MODEL_A, MODEL_B),
        )
    db_conn.commit()

    migrate_run_status_columns(db_conn)
    migrate_measurement_scorer_columns(db_conn)
    migrate_add_response_columns(db_conn)
    migrate_add_measurement_cap_column(db_conn)
    migrate_add_calibration_measurement_columns(db_conn)
    migrate_add_directional_separation(db_conn)

    schema = columns(db_conn)
    assert {"status", "failure_reason"} <= schema["run"]
    assert {
        "scorer_name",
        "scorer_version",
        "response_text",
        "thinking_text",
        "requested_max_output",
    } <= schema["measurement"]
    assert {
        "pool_hash",
        "anchor",
        "battery",
        "tier",
        "item_type",
        "score",
        "passed",
        "tokens_in",
        "tokens_out",
        "latency_ms",
        "infra_failure",
    } <= schema["calibration_event"]
    assert "directional" in schema["separation"]
    # legacy rows are backfilled with the documented defaults
    assert scalar(
        db_conn, "SELECT status FROM hr.run WHERE run_id = 'run-legacy'"
    ) == "scored"
    assert scalar(
        db_conn, "SELECT failure_reason FROM hr.run WHERE run_id = 'run-legacy'"
    ) is None
    assert scalar(
        db_conn, "SELECT scorer_name FROM hr.measurement WHERE measurement_id = 'meas-legacy'"
    ) == "unknown"
    assert scalar(
        db_conn, "SELECT directional FROM hr.separation WHERE separation_id = 'sep-legacy'"
    ) is False
    # the recreated unique index is functional (duplicate anchor measurement refused)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hr.calibration_event (event_id, item_id, kind, pool_hash, anchor) "
            "VALUES ('cal-a1', 'tool_a.calc.001', 'anchor_measurement', 'pool-x', 'anchor-x')"
        )
    db_conn.commit()
    with pytest.raises(psycopg2.errors.UniqueViolation):
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO hr.calibration_event (event_id, item_id, kind, pool_hash, anchor) "
                "VALUES ('cal-a2', 'tool_a.calc.001', 'anchor_measurement', 'pool-x', 'anchor-x')"
            )
    db_conn.rollback()


# ---------------------------------------------------------------------------
# hr.calibration_persistence — scorer provenance round trip
# ---------------------------------------------------------------------------


class _Probe(CalibrationPersistenceMixin):
    """Minimal mixin consumer: a live-DB ``db`` with a .connect() method."""

    def __init__(self, db: object, pool_hash: str, resume: bool = True) -> None:
        self.resume = resume
        self.db = db
        self.pool_hash = pool_hash
        self._recorded_pairs: set[tuple[str, str]] = set()
        self._recorded_measurements: list[Measurement] = []


class _FakeDb:
    def __init__(self, conn: psycopg2.extensions.connection) -> None:
        self._conn = conn

    def connect(self) -> psycopg2.extensions.connection:
        return self._conn


def _report(pool_hash: str = "pool-abc") -> CalibrationReport:
    return CalibrationReport(
        pool_hash=pool_hash,
        measurements=[
            Measurement(
                anchor="cheap",
                item_key="tool_a.calc.001",
                battery="vision",
                tier=2,
                item_type="tool_a",
                score=0.75,
                passed=True,
                tokens_in=10,
                tokens_out=20,
                latency_ms=120,
                detail={"schema_valid": True},
            ),
            Measurement(
                anchor="costly",
                item_key="tool_a.calc.002",
                battery="vision",
                tier=3,
                item_type="tool_a",
                score=0.4,
                passed=False,
                tokens_in=11,
                tokens_out=21,
                latency_ms=130,
            ),
        ],
        verdicts=[],
        total_tokens_in=21,
        total_tokens_out=41,
    )


def test_calibration_persistence_persist_round_trip(
    db_conn: psycopg2.extensions.connection,
) -> None:
    seed_envelope_item(db_conn, "tool_a.calc.001")
    seed_envelope_item(db_conn, "tool_a.calc.002")
    report = _report()
    probe = _Probe(_FakeDb(db_conn), report.pool_hash)

    probe._persist(report)

    assert scalar(
        db_conn, "SELECT COUNT(*) FROM hr.calibration_event WHERE pool_hash = 'pool-abc'"
    ) == 2
    # deterministic event ids from (pool_hash, anchor, item_key)
    for measurement in report.measurements:
        event_key = "|".join(
            (report.pool_hash, measurement.anchor, measurement.item_key)
        ).encode("utf-8")
        event_id = f"cal-{hashlib.sha256(event_key).hexdigest()}"
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT score, passed, item_type FROM hr.calibration_event "
                "WHERE event_id = %s",
                (event_id,),
            )
            row = cur.fetchone()
        assert (float(row[0]), bool(row[1]), row[2]) == (
            measurement.score,
            measurement.passed,
            measurement.item_type,
        )
    # ON CONFLICT DO UPDATE: re-persist with a changed score updates in place
    changed = _report()
    changed.measurements[0].score = 0.9
    probe._persist(changed)
    assert scalar(
        db_conn, "SELECT COUNT(*) FROM hr.calibration_event WHERE pool_hash = 'pool-abc'"
    ) == 2
    # NUMERIC(10,6) round-trips as Decimal — compare numerically (0.9 has no
    # exact binary float representation, so Decimal == float is False).
    assert float(
        scalar(
            db_conn,
            "SELECT score FROM hr.calibration_event WHERE item_id = 'tool_a.calc.001' AND pool_hash = 'pool-abc'",
        )
    ) == pytest.approx(0.9)


def test_calibration_persistence_resume_loads_recorded_pairs(
    db_conn: psycopg2.extensions.connection,
) -> None:
    seed_envelope_item(db_conn, "tool_a.calc.001")
    seed_envelope_item(db_conn, "tool_a.calc.002")
    report = _report()
    _Probe(_FakeDb(db_conn), report.pool_hash)._persist(report)

    resumed = _Probe(_FakeDb(db_conn), report.pool_hash, resume=True)
    resumed._load_recorded_pairs()

    assert resumed._recorded_pairs == {
        ("cheap", "tool_a.calc.001"),
        ("costly", "tool_a.calc.002"),
    }
    by_item = {m.item_key: m for m in resumed._recorded_measurements}
    assert set(by_item) == {"tool_a.calc.001", "tool_a.calc.002"}
    first = by_item["tool_a.calc.001"]
    assert (first.anchor, first.battery, first.tier, first.item_type) == (
        "cheap", "vision", 2, "tool_a",
    )
    assert (first.score, first.passed, first.tokens_in, first.tokens_out) == (0.75, True, 10, 20)
    assert first.detail == {"schema_valid": True}
    # unknown pool: resume finds nothing (empty result, not an error)
    empty = _Probe(_FakeDb(db_conn), "pool-none", resume=True)
    empty._load_recorded_pairs()
    assert empty._recorded_measurements == []
    assert empty._recorded_pairs == set()


def test_calibration_persistence_corrupt_provenance_failure_paths(
    db_conn: psycopg2.extensions.connection,
) -> None:
    """Provenance JSON edge cases against the live JSONB column.

    * truly corrupt input never reaches the column (JSONB rejects it);
    * a nested-JSON *string* value (``'"{\\"nested\\": 1}"'``) is the
      intended str-branch: the reader recovers the dict via json.loads;
    * a plain-string detail round-trips verbatim: psycopg2 auto-parses the
      JSON string to ``plain string``, and the reader keeps non-JSON text
      as-is (no double-parse crash);
    * an anchor row with a NULL score/tier is SKIPPED by the reader (no
      reconstructible measurement) instead of crashing on int/float(None).
    """
    seed_envelope_item(db_conn, "tool_a.calc.001")
    with pytest.raises(psycopg2.errors.InvalidTextRepresentation):
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO hr.calibration_event (event_id, item_id, kind, pool_hash, anchor, "
                "item_type, passed, evidence_json) "
                "VALUES ('cal-bad', 'tool_a.calc.001', 'anchor_measurement', 'pool-x', 'anchor-x', "
                "'tool_a', TRUE, 'not-json'::jsonb)"
            )
    db_conn.rollback()

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hr.calibration_event (event_id, item_id, kind, pool_hash, anchor, "
            "tier, item_type, passed, score, evidence_json) "
            "VALUES ('cal-nested', 'tool_a.calc.001', 'anchor_measurement', 'pool-nested', "
            "'anchor-x', 2, 'tool_a', TRUE, 0.7, '\"{\\\"nested\\\": 1}\"'::jsonb)"
        )
        cur.execute(
            "INSERT INTO hr.calibration_event (event_id, item_id, kind, pool_hash, anchor, "
            "tier, item_type, passed, score, evidence_json) "
            "VALUES ('cal-str', 'tool_a.calc.001', 'anchor_measurement', 'pool-str', "
            "'anchor-x', 2, 'tool_a', TRUE, 0.6, '\"plain string\"'::jsonb)"
        )
    db_conn.commit()

    nested = _Probe(_FakeDb(db_conn), "pool-nested", resume=True)
    nested._load_recorded_pairs()
    assert nested._recorded_measurements[0].detail == {"nested": 1}

    plain = _Probe(_FakeDb(db_conn), "pool-str", resume=True)
    plain._load_recorded_pairs()
    assert plain._recorded_measurements[0].detail == "plain string"

    # NULL score with item_type+passed set: the reader SKIPS the row (no
    # reconstructible measurement) instead of crashing on int/float(None).
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hr.calibration_event (event_id, item_id, kind, pool_hash, anchor, "
            "tier, item_type, passed) "
            "VALUES ('cal-nullscore', 'tool_a.calc.001', 'anchor_measurement', 'pool-nullscore', "
            "'anchor-x', 2, 'tool_a', TRUE)"
        )
    db_conn.commit()
    nullscore = _Probe(_FakeDb(db_conn), "pool-nullscore", resume=True)
    nullscore._load_recorded_pairs()
    assert nullscore._recorded_measurements == []
    assert nullscore._recorded_pairs == set()