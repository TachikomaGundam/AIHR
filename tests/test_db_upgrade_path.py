"""Regression: in-place upgrade of a legacy 19-table database (W4 fix).

W4 rehearsal proved the shipped ``hr.db.init_schema()`` cannot upgrade a
pre-existing pre-migration database: the schema DDL creates the
``calibration_anchor_measurement_idx`` index (columns include pool_hash)
BEFORE the column-add migration ``migrate_add_calibration_measurement_columns``
adds pool_hash to the legacy ``calibration_event`` table, so the whole batch
aborts with ``UndefinedColumn: column "pool_hash" does not exist``. Fresh
databases never hit it because their CREATE TABLE already carries the
columns.

These tests rebuild the authoritative legacy shape (19 tables, no
``experiment_manifest``, no post-migration columns — verbatim from the
pre-migration pg_dump snapshot, see ``tests/fixtures/legacy_hr_schema.sql``),
then drive the SHIPPED ``init_schema()`` against it and require success,
20 tables, the calibration index, and preserved row data. The equivalence
test then proves fresh-vs-upgraded schemas are identical through
information_schema + pg_indexes — which also guards the whole migration set
against any further index-before-column ordering hazard.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal

import psycopg2
import psycopg2.extensions
import pytest

from hr.db import init_schema
from tests.test_db import EXPECTED_TABLES
from tests._db_contracts_helpers import scalar

_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "legacy_hr_schema.sql"
)

# Columns the migrations must add onto the legacy shape (drift lock).
_POST_CAL_COLUMNS = {
    "pool_hash", "anchor", "battery", "tier", "item_type", "score",
    "passed", "tokens_in", "tokens_out", "latency_ms", "infra_failure",
}
_LEGACY_CAL_COLUMNS = {
    "event_id", "item_id", "kind", "before_se", "after_se",
    "evidence_json", "recorded_at",
}
_EXPECTED_INDEXES = {
    "assignment_seat_idx", "calibration_item_idx", "incident_run_idx",
    "item_model_idx", "item_pool_domain_idx", "judge_sweep_idx",
    "measurement_item_score", "measurement_run_idx", "model_provider_idx",
    "run_model_idx", "run_sweep_idx", "separation_sweep_idx",
    "sweep_seat_idx", "calibration_anchor_measurement_idx",
}


def _require_admin_params() -> dict[str, str]:
    """Admission rule mirroring the shared scratch fixture (conftest)."""
    dsn = os.environ.get("HR_TEST_PG_DSN")
    if not dsn:
        if os.environ.get("HR_DSN"):
            pytest.fail(
                "HR_DSN is rejected for test-DB access: set HR_TEST_PG_DSN "
                "(admin-level DSN, dbname=postgres) for live-DB tests"
            )
        pytest.skip("DB credentials required (HR_TEST_PG_DSN) for live-DB tests")
    params = psycopg2.extensions.parse_dsn(dsn)
    if params.get("dbname", "postgres") != "postgres":
        pytest.fail(
            f"HR_TEST_PG_DSN must be an admin-level connection (dbname=postgres), "
            f"got dbname={params.get('dbname')!r}"
        )
    return params


def _admin_connect(params: dict[str, str]) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(
        dbname="postgres",
        host=params.get("host") or "localhost",
        port=int(params.get("port") or 5432),
        user=params.get("user") or "wikijs",
        password=params.get("password"),
    )
    conn.autocommit = True
    return conn


@contextmanager
def _scratch_db(params: dict[str, str]) -> Iterator[psycopg2.extensions.connection]:
    """Yield a connection to a fresh isolated scratch database (hr_test_*)."""
    name = "hr_test_" + uuid.uuid4().hex[:10]
    admin = _admin_connect(params)
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        admin.close()
    conn = psycopg2.connect(
        dbname=name,
        host=params.get("host") or "localhost",
        port=int(params.get("port") or 5432),
        user=params.get("user") or "wikijs",
        password=params.get("password"),
    )
    try:
        yield conn
    finally:
        conn.close()
        admin = _admin_connect(params)
        try:
            with admin.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            admin.close()


def _build_legacy(conn: psycopg2.extensions.connection) -> None:
    """Apply the pre-migration 19-table schema (from the pg_dump snapshot)."""
    with open(_FIXTURE, encoding="utf-8") as fh:
        ddl = fh.read()
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()


def _seed_legacy_rows(conn: psycopg2.extensions.connection) -> None:
    """Rows in the PRE-migration column shapes (FK-linked minimal chain)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hr.seat (seat_code, seat_name, domain) "
            "VALUES ('oracle', 'oracle', 'reasoning')"
        )
        cur.execute(
            "INSERT INTO hr.battery (battery_id, battery_code) "
            "VALUES ('b-reasoning', 'reasoning')"
        )
        cur.execute(
            "INSERT INTO hr.provider (provider_id, name) VALUES ('prov-a', 'prov-a')"
        )
        cur.execute(
            "INSERT INTO hr.model (model_id, provider_fk, model_name) "
            "VALUES ('prov-a/m', 'prov-a', 'm'), ('prov-a/m2', 'prov-a', 'm2')"
        )
        cur.execute(
            "INSERT INTO hr.item_pool (item_id, item_code, domain, kind) "
            "VALUES ('tool_a.calc.001', 'tool_a.calc.001', 'tool_a', 'tool_a')"
        )
        cur.execute(
            "INSERT INTO hr.sweep (sweep_id, seat_code) VALUES ('sw-a', 'oracle')"
        )
        cur.execute(
            "INSERT INTO hr.run (run_id, sweep_id, model_id, battery_id, total_tokens) "
            "VALUES ('run-legacy', 'sw-a', 'prov-a/m', 'b-reasoning', 1500)"
        )
        cur.execute(
            "INSERT INTO hr.measurement "
            "(measurement_id, run_id, item_id, repetition, score, response_text) "
            "VALUES ('meas-legacy', 'run-legacy', 'tool_a.calc.001', 1, 0.5, 'legacy')"
        )
        cur.execute(
            "INSERT INTO hr.calibration_event "
            "(event_id, item_id, kind, before_se, after_se, evidence_json) "
            "VALUES ('cal-legacy', 'tool_a.calc.001', 'anchor_measurement', "
            "0.2, 0.8, '{}'::jsonb)"
        )
        cur.execute(
            "INSERT INTO hr.separation "
            "(separation_id, sweep_id, battery_id, model_a, model_b, "
            "p_separated, p_weak, p_tie) "
            "VALUES ('sep-legacy', 'sw-a', 'b-reasoning', 'prov-a/m', "
            "'prov-a/m2', 0.8, 0.1, 0.1)"
        )
    conn.commit()


def _table_columns(conn: psycopg2.extensions.connection) -> dict[str, set[str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'hr'"
        )
        out: dict[str, set[str]] = {}
        for table, column in cur.fetchall():
            out.setdefault(str(table), set()).add(str(column))
    return out


def _schema_snapshot(conn: psycopg2.extensions.connection) -> tuple[object, ...]:
    """(tables, columns, indexes) — the equivalence fingerprint."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'hr'"
        )
        tables = frozenset(row[0] for row in cur.fetchall())
        cur.execute(
            "SELECT table_name, column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema = 'hr'"
        )
        columns = frozenset(tuple(row) for row in cur.fetchall())
        cur.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'hr'"
        )
        indexes = frozenset(tuple(row) for row in cur.fetchall())
    return tables, columns, indexes


@pytest.mark.db
@pytest.mark.integration
def test_legacy_fixture_shapes_pre_migration_starting_state() -> None:
    """The fixture must reproduce 19 tables WITHOUT any post-migration column."""
    params = _require_admin_params()
    with _scratch_db(params) as conn:
        _build_legacy(conn)
        schema = _table_columns(conn)
        assert set(EXPECTED_TABLES) - {"experiment_manifest"} == set(schema)
        assert len(schema) == 19
        assert _LEGACY_CAL_COLUMNS == schema["calibration_event"]
        assert "pool_hash" not in schema["calibration_event"]
        assert not ({"status", "failure_reason"} & set(schema["run"]))
        assert "directional" not in schema["separation"]
        assert "scorer_name" not in schema["measurement"]
        # the snapshot's measurement already carried the response columns
        assert {"response_text", "thinking_text", "requested_max_output"} <= schema["measurement"]


@pytest.mark.db
@pytest.mark.integration
def test_init_schema_upgrades_legacy_db_in_place() -> None:
    """Shipped init_schema() on a legacy 19-table DB: success + 20 tables + index + data."""
    params = _require_admin_params()
    with _scratch_db(params) as conn:
        _build_legacy(conn)
        _seed_legacy_rows(conn)
        init_schema(conn, own_connection=False)

        schema = _table_columns(conn)
        assert set(EXPECTED_TABLES) == set(schema)
        assert len(schema) == 20
        assert _POST_CAL_COLUMNS <= schema["calibration_event"]
        assert {"status", "failure_reason"} <= schema["run"]
        assert "directional" in schema["separation"]
        assert {"scorer_name", "scorer_version"} <= schema["measurement"]

        assert (
            scalar(
                conn,
                "SELECT 1 FROM pg_indexes WHERE schemaname = 'hr' "
                "AND indexname = 'calibration_anchor_measurement_idx'",
            )
            is not None
        )

        # row data survived the in-place upgrade
        assert scalar(conn, "SELECT count(*) FROM hr.measurement") == 1
        assert scalar(conn, "SELECT count(*) FROM hr.calibration_event") == 1
        assert scalar(
            conn, "SELECT score FROM hr.measurement WHERE measurement_id = 'meas-legacy'"
        ) == 0.5
        assert scalar(
            conn,
            "SELECT response_text FROM hr.measurement WHERE measurement_id = 'meas-legacy'",
        ) == "legacy"
        assert scalar(
            conn,
            "SELECT before_se FROM hr.calibration_event WHERE event_id = 'cal-legacy'",
        ) == Decimal("0.2")
        # freshly added NOT NULL DEFAULT columns backfilled on legacy rows
        assert scalar(
            conn, "SELECT status FROM hr.run WHERE run_id = 'run-legacy'"
        ) == "scored"
        assert scalar(
            conn, "SELECT count(*) FROM hr.experiment_manifest"
        ) == 0


@pytest.mark.db
@pytest.mark.integration
def test_fresh_and_upgraded_schemas_are_equivalent() -> None:
    """Fresh init_schema vs legacy-then-init_schema: identical final schema.

    Compares information_schema tables + columns (name/type/nullability/
    default) and pg_indexes (name/definition). Any residual
    index-before-column hazard anywhere in the migration set would make the
    upgraded side fail or diverge from the fresh side.
    """
    params = _require_admin_params()
    with _scratch_db(params) as fresh:
        init_schema(fresh, own_connection=False)
        fresh_snapshot = _schema_snapshot(fresh)
    with _scratch_db(params) as upgraded:
        _build_legacy(upgraded)
        _seed_legacy_rows(upgraded)
        init_schema(upgraded, own_connection=False)
        upgraded_snapshot = _schema_snapshot(upgraded)

    tables_f, columns_f, indexes_f = fresh_snapshot
    tables_u, columns_u, indexes_u = upgraded_snapshot
    assert tables_f == tables_u == frozenset(EXPECTED_TABLES)
    assert columns_f == columns_u
    assert indexes_f == indexes_u
    # the full index set includes PK/UNIQUE constraint indexes; the 14
    # explicit CREATE INDEX statements must all be present on both paths
    assert _EXPECTED_INDEXES <= {name for name, _def in indexes_u}