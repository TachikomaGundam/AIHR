from __future__ import annotations

import os
from pathlib import Path

import psycopg2
import psycopg2.extensions

from hr.config import compose_db_password, db_dsn
from hr.db_schema import DDL as _DDL
from hr.db_schema import DDL_INDEXES as _DDL_INDEXES
from hr.db_schema import DDL_SCHEMA as _DDL_SCHEMA
from hr.schema_migration import (
    migrate_measurement_scorer_columns,
    migrate_run_status_columns,
    migrate_schema_namespace,
)


def _load_db_password() -> str:
    password = os.environ.get("HR_DB_PASSWORD")
    if password:
        return password
    compose = os.environ.get("HR_COMPOSE_FILE")
    if compose:
        password = compose_db_password(Path(compose).expanduser())
        if password:
            return password
    raise RuntimeError(
        "cannot resolve DB password: set HR_DB_PASSWORD, or set "
        "HR_COMPOSE_FILE to a docker-compose.yml whose services.wiki "
        "environment defines DB_PASS/POSTGRES_PASSWORD"
    )


def connect(
    dbname: str = "wiki",
    user: str | None = None,
    host: str = "localhost",
    port: int = 5432,
    password: str | None = None,
) -> psycopg2.extensions.connection:
    if password is None:
        try:
            conn = psycopg2.connect(db_dsn())
        except RuntimeError:
            password = _load_db_password()
        else:
            migrate_schema_namespace(conn)
            return conn
    conn = psycopg2.connect(
        dbname=dbname,
        user=user or os.environ.get("HR_DB_USER", "wikijs"),
        host=host,
        port=port,
        password=password,
    )
    migrate_schema_namespace(conn)
    return conn


def ddl() -> str:
    return _DDL


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
