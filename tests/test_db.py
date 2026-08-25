"""Tests for db.py — verify schema DDL without hitting a live database."""
from __future__ import annotations

import pytest
from hr import db
from hr.schema_migration import migrate_schema_namespace


# Expected tables from spec §4, including the migration-added tables the
# base DDL now ships: 20 tables in DDL order (verified live against
# information_schema on a fresh scratch DB — experiment_manifest is the
# 20th, created by the base DDL, not only by a later migration).
EXPECTED_TABLES = [
    "provider",
    "model",
    "control_model",
    "seat",
    "item_pool",
    "item",
    "battery",
    "battery_item",
    "seat_battery",
    "sweep",
    "experiment_manifest",
    "run",
    "measurement",
    "infra_incident",
    "control_reading",
    "separation",
    "assignment",
    "policy_override",
    "calibration_event",
    "judge_verdict",
]


def test_ddl_creates_hr_schema():
    ddl = db.ddl()
    assert "CREATE SCHEMA hr" in ddl
    for table in EXPECTED_TABLES:
        assert f"hr.{table}" in ddl, f"missing hr.{table} in DDL"
    assert "hr2" not in ddl


def test_all_20_tables_defined():
    ddl = db.ddl()
    for table in EXPECTED_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS hr.{table}" in ddl


def test_control_model_primary_key_is_provider_fk():
    """v0.2: control_model PK = (provider_fk)."""
    ddl = db.ddl()
    # Find the control_model block
    start = ddl.find("CREATE TABLE IF NOT EXISTS hr.control_model")
    assert start != -1
    block = ddl[start:ddl.find(");", start) + 2]
    assert "provider_fk  TEXT PRIMARY KEY" in block


def test_measurement_unique_index():
    ddl = db.ddl()
    assert "UNIQUE (run_id, item_id, repetition)" in ddl


def test_measurement_index_on_item_score():
    ddl = db.ddl()
    assert "ON hr.measurement (item_id, score)" in ddl


def test_judge_verdict_unique_constraint():
    ddl = db.ddl()
    assert "UNIQUE (sweep_id, item_id, model_id, round)" in ddl


def test_calibration_event_supports_anchor_measurement_resume() -> None:
    ddl = db.ddl()
    for column in ("pool_hash", "anchor", "battery", "tier", "score"):
        assert column in ddl


def test_separation_check_model_a_ne_model_b():
    ddl = db.ddl()
    assert "model_a <> model_b" in ddl
    assert "directional" in ddl


def test_schema_guard_does_not_touch_public_tables():
    """DDL must not reference any public/v1 tables."""
    ddl = db.ddl()
    assert "CREATE TABLE public" not in ddl
    assert "public." not in ddl


@pytest.mark.db
@pytest.mark.integration
def test_init_schema_against_live_db(scratch_db: tuple[str, str]) -> None:
    """init_schema on the SHARED scratch DB (T1 fixture): idempotent + exact.

    The shared fixture already initialized the schema; re-running the full
    pipeline twice must be a no-op and must produce EXACTLY the authoritative
    EXPECTED_TABLES set (information_schema-verified; experiment_manifest is
    the 20th table, shipped by the base DDL). Offline runs skip via the
    shared fixture's admission rule (HR_TEST_PG_DSN unset).
    """
    _name, _scratch_dsn = scratch_db
    conn = db.connect()
    try:
        db.init_schema(conn, own_connection=False)
        # Re-run idempotently
        db.init_schema(conn, own_connection=False)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='hr'"
            )
            live = {row[0] for row in cur.fetchall()}
        assert len(live) == len(EXPECTED_TABLES)
        assert set(EXPECTED_TABLES) == live
    finally:
        conn.close()


def test_no_hardcoded_password_in_source():
    """Sanity: db.py contains no inline string password literal.

    We assert the source has no assignment of the form
        password = "..." or password = '...'
    so credentials cannot accidentally be committed. Passwords must come
    from the docker-compose fallback, HR_DSN env var, or similar runtime
    secrets source — never as a hard-coded literal in source.
    """
    import inspect
    import re
    src = inspect.getsource(db)
    # Generic guard: no `password = "..."` or `password = '...'` literal.
    assert re.search(r"(?i)password\s*=\s*[\"'][^\"']+[\"']", src) is None, (
        "hr.db contains an inline password literal — resolve via env/runtime"
    )


# ---------------------------------------------------------------------------
# Response-column migration
# ---------------------------------------------------------------------------
class _RecordingCursor:
    def __init__(self):
        self.executed: list[tuple] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, tuple(params or ())))

    def fetchone(self):
        return (1,)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class _RecordingConn:
    def __init__(self):
        self.cursor_ = _RecordingCursor()
        self.commits = 0

    def cursor(self):
        return self.cursor_

    def commit(self):
        self.commits += 1


def test_namespace_migration_renames_legacy_schema_in_place() -> None:
    # Given: a connection that records migration statements.
    conn = _RecordingConn()

    # When: the namespace migration runs.
    migrate_schema_namespace(conn)

    # Then: the legacy data is renamed, and ambiguous dual state is rejected.
    sql = conn.cursor_.executed[0][0]
    assert "ALTER SCHEMA hr2 RENAME TO hr" in sql
    assert "both legacy and canonical HR schemas exist" in sql
    assert conn.commits == 1


def test_migrate_add_response_columns_uses_if_not_exists():
    conn = _RecordingConn()
    db.migrate_add_response_columns(conn)
    sqls = [c[0] for c in conn.cursor_.executed]
    assert any(
        "ADD COLUMN IF NOT EXISTS response_text TEXT" in s for s in sqls
    )
    assert any(
        "ADD COLUMN IF NOT EXISTS thinking_text TEXT" in s for s in sqls
    )


def test_migrate_add_response_columns_idempotent():
    conn = _RecordingConn()
    db.migrate_add_response_columns(conn)
    n_first = len(conn.cursor_.executed)
    db.migrate_add_response_columns(conn)
    assert len(conn.cursor_.executed) == 2 * n_first
    assert conn.commits >= 2


def test_calibration_migration_adds_resumable_outcome_columns() -> None:
    # Given
    conn = _RecordingConn()

    # When
    db.migrate_add_calibration_measurement_columns(conn)

    # Then
    statements = [statement for statement, _ in conn.cursor_.executed]
    assert any("item_type TEXT" in statement for statement in statements)
    assert any("passed BOOLEAN" in statement for statement in statements)


def test_run_status_migration_adds_status_and_failure_reason() -> None:
    # Given
    conn = _RecordingConn()

    # When
    db.migrate_run_status_columns(conn)

    # Then
    statements = [statement for statement, _ in conn.cursor_.executed]
    assert any("status TEXT NOT NULL DEFAULT 'scored'" in s for s in statements)
    assert any("failure_reason TEXT" in s for s in statements)


def test_run_status_migration_is_idempotent() -> None:
    # Given
    conn = _RecordingConn()

    # When
    db.migrate_run_status_columns(conn)
    n_first = len(conn.cursor_.executed)
    db.migrate_run_status_columns(conn)

    # Then
    assert len(conn.cursor_.executed) == 2 * n_first
    assert conn.commits >= 2


def test_measurement_scorer_migration_adds_provenance_columns() -> None:
    conn = _RecordingConn()

    db.migrate_measurement_scorer_columns(conn)

    statements = [statement for statement, _ in conn.cursor_.executed]
    assert any("scorer_name TEXT NOT NULL DEFAULT 'unknown'" in s for s in statements)
    assert any("scorer_version TEXT" in s for s in statements)
