from __future__ import annotations

import os
from pathlib import Path

import psycopg2
import psycopg2.errors
import psycopg2.extensions
from psycopg2 import sql

from hr.config import compose_db_password, db_dsn, db_target_fields
from hr.config_resources import db_env_file, read_db_env_file
from hr.db_schema import DDL as _DDL
from hr.db_schema import DDL_INDEXES as _DDL_INDEXES
from hr.db_schema import DDL_SCHEMA as _DDL_SCHEMA
from hr.schema_migration import (
    migrate_dialect_contract_columns,
    migrate_measurement_finish_reason,
    migrate_measurement_scorer_columns,
    migrate_run_status_columns,
    migrate_schema_namespace,
)


def _load_db_password() -> str:
    """Password chain: HR_DB_PASSWORD > AIHR db env file > HR_COMPOSE_FILE."""
    password = os.environ.get("HR_DB_PASSWORD")
    if password:
        return password
    env_file = db_env_file()
    if env_file is not None:
        file_password = read_db_env_file(env_file).get("AIHR_DB_PASSWORD")
        if file_password:
            return file_password
    compose = os.environ.get("HR_COMPOSE_FILE")
    if compose:
        password = compose_db_password(Path(compose).expanduser())
        if password:
            return password
    raise RuntimeError(
        "cannot resolve DB password: set HR_DB_PASSWORD, run `hr db-up` "
        "to provision the AIHR db env file, or set HR_COMPOSE_FILE to a "
        "docker-compose.yml whose services.db/services.wiki environment "
        "defines POSTGRES_PASSWORD/DB_PASS"
    )


_SCHEMA_HEAL_PROBE = "SELECT to_regclass('hr.run'), to_regclass('hr.model_contract')"


def _ensure_runtime(conn: psycopg2.extensions.connection) -> None:
    """Per-connection schema self-heal — product invariant: the first command
    against a database in ANY state (empty, legacy, half-migrated) works with
    no manual ``hr db-up`` and no migration patch.

    Fast path (healthy database): namespace migration + one to_regclass probe.
    Slow path (a sentinel table is missing): run the idempotent init_schema
    pipeline on this same connection. Concurrent healers racing on
    ``CREATE ... IF NOT EXISTS`` may raise duplicate-object errors — re-probe
    and accept once the sentinels have landed. A role that cannot create the
    schema gets a named error pointing at ``hr db-up`` instead of a raw
    privilege traceback.

    Maintenance escape hatch: ``HR_NO_SCHEMA_HEAL=1`` skips probe and heal so
    a broken schema can be inspected without the healer interfering.
    """
    migrate_schema_namespace(conn)
    if os.environ.get("HR_NO_SCHEMA_HEAL") == "1":
        return

    def _sentinels_present(cur) -> bool:
        cur.execute(_SCHEMA_HEAL_PROBE)
        row = cur.fetchone()
        return bool(row) and all(col is not None for col in row)

    with conn.cursor() as cur:
        if _sentinels_present(cur):
            return
    try:
        init_schema(conn, own_connection=False)
    except psycopg2.errors.InsufficientPrivilege as exc:
        conn.rollback()
        raise RuntimeError(
            "hr schema is incomplete and this role cannot create it: "
            "run `hr db-up` (or grant this role schema ownership) — "
            f"underlying error: {exc}"
        ) from exc
    except psycopg2.ProgrammingError:
        # A concurrent healer may have won the CREATE race: acceptable only
        # when the sentinels actually landed; anything else re-raises.
        conn.rollback()
        with conn.cursor() as cur:
            if _sentinels_present(cur):
                return
        raise


def connect(
    dbname: str | None = None,
    user: str | None = None,
    host: str | None = None,
    port: int | None = None,
    password: str | None = None,
) -> psycopg2.extensions.connection:
    if password is None:
        try:
            conn = psycopg2.connect(db_dsn())
        except RuntimeError:
            password = _load_db_password()
        else:
            _ensure_runtime(conn)
            return conn
    fields = db_target_fields()
    conn = psycopg2.connect(
        dbname=dbname or str(fields["dbname"]),
        user=user or str(fields["user"]),
        host=host or str(fields["host"]),
        port=port or int(fields["port"]),
        password=password,
    )
    _ensure_runtime(conn)
    return conn


def ddl() -> str:
    return _DDL


def schema_stats(conn: psycopg2.extensions.connection) -> tuple[int, int]:
    """(hr.* table count, total row count across those tables)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'hr' ORDER BY table_name"
        )
        tables = [str(row[0]) for row in cur.fetchall()]
        total = 0
        for table in tables:
            cur.execute(
                sql.SQL("SELECT count(*) FROM {}").format(
                    sql.Identifier("hr", table)
                )
            )
            row = cur.fetchone()
            total += int(row[0]) if row else 0
    return len(tables), total


def migrate_add_response_columns(conn: psycopg2.extensions.connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE hr.measurement "
            "ADD COLUMN IF NOT EXISTS response_text TEXT"
        )
        cur.execute(
            "ALTER TABLE hr.measurement "
            "ADD COLUMN IF NOT EXISTS thinking_text TEXT"
        )
    conn.commit()


def migrate_add_measurement_cap_column(
    conn: psycopg2.extensions.connection,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE hr.measurement "
            "ADD COLUMN IF NOT EXISTS requested_max_output INTEGER"
        )
    conn.commit()


def migrate_add_calibration_measurement_columns(
    conn: psycopg2.extensions.connection,
) -> None:
    with conn.cursor() as cur:
        for column in (
            "pool_hash TEXT",
            "anchor TEXT",
            "battery TEXT",
            "tier INTEGER",
            "item_type TEXT",
            "score NUMERIC(10, 6)",
            "passed BOOLEAN",
            "tokens_in INTEGER",
            "tokens_out INTEGER",
            "latency_ms INTEGER",
            "infra_failure TEXT",
        ):
            cur.execute(
                f"ALTER TABLE hr.calibration_event ADD COLUMN IF NOT EXISTS {column}"
            )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS calibration_anchor_measurement_idx "
            "ON hr.calibration_event (pool_hash, anchor, item_id) "
            "WHERE kind = 'anchor_measurement'"
        )
    conn.commit()


def migrate_add_directional_separation(
    conn: psycopg2.extensions.connection,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE hr.separation "
            "ADD COLUMN IF NOT EXISTS directional BOOLEAN NOT NULL DEFAULT FALSE"
        )
    conn.commit()


def _run_migrations(conn: psycopg2.extensions.connection) -> None:
    migrate_schema_namespace(conn)
    migrate_add_response_columns(conn)
    migrate_add_measurement_cap_column(conn)
    migrate_add_calibration_measurement_columns(conn)
    migrate_add_directional_separation(conn)
    migrate_run_status_columns(conn)
    migrate_measurement_scorer_columns(conn)
    migrate_dialect_contract_columns(conn)
    migrate_measurement_finish_reason(conn)


def migrate() -> None:
    conn = connect()
    try:
        _run_migrations(conn)
    finally:
        conn.close()


def init_schema(
    conn: psycopg2.extensions.connection | None = None,
    own_connection: bool = True,
) -> None:
    """Create or upgrade the HR schema in place (idempotent pipeline).

    Phase order is load-bearing (W4-fix): CREATE TABLE IF NOT EXISTS first
    (no-ops on a legacy pre-migration database), then the column-add
    migrations (ALTER TABLE ... ADD COLUMN IF NOT EXISTS — no-ops on a
    fresh database), then the CREATE INDEX statements last. Legacy tables
    predate some index columns (pool_hash/anchor on calibration_event
    arrive via ``migrate_add_calibration_measurement_columns``), so indexes
    must never be created before their table's column-add migrations run;
    on a fresh database the CREATE TABLE already carries every column.
    """
    close_after = conn is None
    if conn is None:
        conn = connect()
    try:
        migrate_schema_namespace(conn)
        with conn.cursor() as cur:
            cur.execute(_DDL_SCHEMA)
        conn.commit()
        _run_migrations(conn)
        with conn.cursor() as cur:
            cur.execute(_DDL_INDEXES)
        conn.commit()
    finally:
        if own_connection and close_after:
            conn.close()
