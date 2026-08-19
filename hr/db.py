"""Database schema and connection management for hr2.

PostgreSQL schema ``hr2`` lives inside the shared ``wiki`` database
but NEVER touches ``public`` or v1 tables.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extensions

from hr.config import compose_db_password, db_dsn

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

_DDL_PATH = Path(__file__).with_name("schema.sql")


def _load_db_password() -> str:
    """Resolve the running DB password.

    Resolution order:
      1. ``HR2_DB_PASSWORD`` env var (preferred; see also HR_DSN)
      2. Config layer's docker-compose fallback — active only when the
         ``HR_COMPOSE_FILE`` env var is set (reads ``DB_PASS`` /
         ``POSTGRES_PASSWORD`` from ``services.wiki.environment`` in that
         compose yaml)
    """
    env = os.environ.get("HR2_DB_PASSWORD")
    if env:
        return env
    compose = os.environ.get("HR_COMPOSE_FILE")
    if compose:
        payload = compose_db_password(Path(compose).expanduser())
        if payload:
            return payload
    raise RuntimeError(
        "cannot resolve DB password: set HR2_DB_PASSWORD, or set "
        "HR_COMPOSE_FILE to a docker-compose.yml whose services.wiki "
        "environment defines DB_PASS/POSTGRES_PASSWORD"
    )


def connect(dbname: str = "wiki", user: Optional[str] = None,
            host: str = "localhost", port: int = 5432,
            password: Optional[str] = None) -> psycopg2.extensions.connection:
    """Return a psycopg2 connection to the wiki database.

    When ``password`` is not passed explicitly:
      1. Prefer the unified ``hr.config.db_dsn()`` (HR_DSN -> hr.toml +
         HR_DB_PASSWORD -> HR_COMPOSE_FILE compose); it wins with the full
         connection string.
      2. Fall back to field-based credentials: ``_load_db_password()``
         (HR2_DB_PASSWORD -> HR_COMPOSE_FILE compose -> error).
    """
    if password is None:
        try:
            return psycopg2.connect(db_dsn())
        except RuntimeError:
            pass  # unresolvable DSN — fall through to field-based credentials
        password = _load_db_password()
    if user is None:
        user = os.environ.get("HR_DB_USER", "wikijs")
    return psycopg2.connect(
        dbname=dbname, user=user, host=host, port=port, password=password,
    )


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_DDL = r"""
-- HR2 schema guard. Never touch public/v1 tables.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'hr2') THEN
        CREATE SCHEMA hr2;
    END IF;
END $$;

-- 1. provider ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hr2.provider (
    provider_id  TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. model ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hr2.model (
    model_id          TEXT PRIMARY KEY,
    provider_fk       TEXT NOT NULL REFERENCES hr2.provider(provider_id),
    model_name        TEXT NOT NULL,
    capabilities      JSONB NOT NULL DEFAULT '{}'::jsonb,
    default_rpm       INTEGER,
    default_cost_1k_in  NUMERIC(12, 6),
    default_cost_1k_out NUMERIC(12, 6),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS model_provider_idx ON hr2.model (provider_fk);

-- 3. control_model (v0.2: PRIMARY KEY(provider_fk) — one control per provider)
CREATE TABLE IF NOT EXISTS hr2.control_model (
    provider_fk  TEXT PRIMARY KEY REFERENCES hr2.provider(provider_id),
    model_id     TEXT NOT NULL REFERENCES hr2.model(model_id),
    mode         TEXT NOT NULL DEFAULT 'primary'
);

-- 4. seat -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hr2.seat (
    seat_code              TEXT PRIMARY KEY,
    seat_name              TEXT NOT NULL,
    domain                 TEXT NOT NULL,
    domain_specificity     NUMERIC(4, 3) NOT NULL DEFAULT 0.5,
    cost_tier              TEXT NOT NULL DEFAULT 'mid',
    budget_tier            TEXT NOT NULL DEFAULT 'mid',
    required_capabilities  JSONB NOT NULL DEFAULT '[]'::jsonb,
    ctx_p95_tokens         INTEGER,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 5. item_pool --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hr2.item_pool (
    item_id    TEXT PRIMARY KEY,
    item_code  TEXT NOT NULL,
    version    TEXT NOT NULL DEFAULT 'v1',
    domain     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    json_meta  JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS item_pool_domain_idx ON hr2.item_pool (domain);

-- 6. item -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hr2.item (
    item_id            TEXT PRIMARY KEY REFERENCES hr2.item_pool(item_id),
    model_id           TEXT NOT NULL REFERENCES hr2.model(model_id),
    payload_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
    difficulty_class   TEXT NOT NULL DEFAULT 'mid',
    calibrated_se      NUMERIC(10, 6),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS item_model_idx ON hr2.item (model_id);

-- 7. battery ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hr2.battery (
    battery_id   TEXT PRIMARY KEY,
    battery_code TEXT NOT NULL UNIQUE,
    version      TEXT NOT NULL DEFAULT 'v1',
    description  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 8. battery_item -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS hr2.battery_item (
    battery_id TEXT NOT NULL REFERENCES hr2.battery(battery_id),
    item_id    TEXT NOT NULL REFERENCES hr2.item_pool(item_id),
    weight     NUMERIC(6, 3) NOT NULL DEFAULT 1.0,
    position   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (battery_id, item_id)
);

-- 9. seat_battery -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS hr2.seat_battery (
    seat_code  TEXT NOT NULL REFERENCES hr2.seat(seat_code),
    battery_id TEXT NOT NULL REFERENCES hr2.battery(battery_id),
    n_initial  INTEGER NOT NULL DEFAULT 3,
    n_max      INTEGER NOT NULL DEFAULT 10,
    PRIMARY KEY (seat_code, battery_id)
);

-- 10. sweep -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hr2.sweep (
    sweep_id    TEXT PRIMARY KEY,
    seat_code   TEXT NOT NULL REFERENCES hr2.seat(seat_code),
    purpose     TEXT NOT NULL DEFAULT 'primary',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS sweep_seat_idx ON hr2.sweep (seat_code);

-- 11. run -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hr2.run (
    run_id         TEXT PRIMARY KEY,
    sweep_id       TEXT NOT NULL REFERENCES hr2.sweep(sweep_id),
    model_id       TEXT NOT NULL REFERENCES hr2.model(model_id),
    battery_id     TEXT NOT NULL REFERENCES hr2.battery(battery_id),
    round          INTEGER NOT NULL DEFAULT 1,
    started_at     TIMESTAMPTZ,
    finished_at    TIMESTAMPTZ,
    total_tokens   INTEGER,
    total_cost_cny NUMERIC(12, 4),
    infra_ok       BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS run_sweep_idx  ON hr2.run (sweep_id);
CREATE INDEX IF NOT EXISTS run_model_idx  ON hr2.run (model_id);

-- 12. measurement -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS hr2.measurement (
    measurement_id TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL REFERENCES hr2.run(run_id),
    item_id        TEXT NOT NULL REFERENCES hr2.item_pool(item_id),
    repetition     INTEGER NOT NULL DEFAULT 1,
    score          NUMERIC(10, 6) NOT NULL,
    tokens_in      INTEGER,
    tokens_out     INTEGER,
    latency_ms     INTEGER,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, item_id, repetition)
);
CREATE INDEX IF NOT EXISTS measurement_run_idx    ON hr2.measurement (run_id);
CREATE INDEX IF NOT EXISTS measurement_item_score ON hr2.measurement (item_id, score);

-- 13. infra_incident --------------------------------------------------------
CREATE TABLE IF NOT EXISTS hr2.infra_incident (
    incident_id TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES hr2.run(run_id),
    kind        TEXT NOT NULL,
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS incident_run_idx ON hr2.infra_incident (run_id);

-- 14. control_reading -------------------------------------------------------
CREATE TABLE IF NOT EXISTS hr2.control_reading (
    reading_id      TEXT PRIMARY KEY,
    control_model_fk TEXT NOT NULL REFERENCES hr2.model(model_id),
    battery_id      TEXT NOT NULL REFERENCES hr2.battery(battery_id),
    round           INTEGER NOT NULL,
    read_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    result_json     JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- 15. separation ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hr2.separation (
    separation_id TEXT PRIMARY KEY,
    sweep_id      TEXT NOT NULL REFERENCES hr2.sweep(sweep_id),
    battery_id    TEXT NOT NULL REFERENCES hr2.battery(battery_id),
    model_a       TEXT NOT NULL REFERENCES hr2.model(model_id),
    model_b       TEXT NOT NULL REFERENCES hr2.model(model_id),
    p_separated   NUMERIC(6, 4) NOT NULL,
    p_weak        NUMERIC(6, 4) NOT NULL,
    p_tie         NUMERIC(6, 4) NOT NULL,
    estimated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (model_a <> model_b)
);
CREATE INDEX IF NOT EXISTS separation_sweep_idx ON hr2.separation (sweep_id);

-- 16. assignment ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hr2.assignment (
    assignment_id TEXT PRIMARY KEY,
    seat_code     TEXT NOT NULL REFERENCES hr2.seat(seat_code),
    primary_model TEXT NOT NULL REFERENCES hr2.model(model_id),
    fallback1     TEXT REFERENCES hr2.model(model_id),
    fallback2     TEXT REFERENCES hr2.model(model_id),
    fallback3     TEXT REFERENCES hr2.model(model_id),
    reason_json   JSONB NOT NULL DEFAULT '{}'::jsonb,
    assigned_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS assignment_seat_idx ON hr2.assignment (seat_code);

-- 17. policy_override -------------------------------------------------------
CREATE TABLE IF NOT EXISTS hr2.policy_override (
    override_id TEXT PRIMARY KEY,
    seat_code   TEXT NOT NULL REFERENCES hr2.seat(seat_code),
    rule        TEXT NOT NULL,
    before_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    after_json  JSONB NOT NULL DEFAULT '{}'::jsonb,
    reason      TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 18. calibration_event -----------------------------------------------------
CREATE TABLE IF NOT EXISTS hr2.calibration_event (
    event_id   TEXT PRIMARY KEY,
    item_id    TEXT NOT NULL REFERENCES hr2.item_pool(item_id),
    kind       TEXT NOT NULL,
    before_se  NUMERIC(10, 6),
    after_se   NUMERIC(10, 6),
    evidence_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS calibration_item_idx ON hr2.calibration_event (item_id);

-- 19. judge_verdict ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS hr2.judge_verdict (
    verdict_id     TEXT PRIMARY KEY,
    sweep_id       TEXT NOT NULL REFERENCES hr2.sweep(sweep_id),
    item_id        TEXT NOT NULL REFERENCES hr2.item_pool(item_id),
    model_id       TEXT NOT NULL REFERENCES hr2.model(model_id),
    round          INTEGER NOT NULL DEFAULT 1,
    judgement_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (sweep_id, item_id, model_id, round)
);
CREATE INDEX IF NOT EXISTS judge_sweep_idx ON hr2.judge_verdict (sweep_id);
"""


def ddl() -> str:
    """Return the full DDL as a string (useful for tests/review)."""
    return _DDL


def migrate_add_response_columns(conn: psycopg2.extensions.connection) -> None:
    """Idempotently add response_text/thinking_text to hr2.measurement.

    Use ``ALTER TABLE … ADD COLUMN IF NOT EXISTS`` so it is safe to re-run
    against a populated schema (existing rows keep NULL for these columns).
    """
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE hr2.measurement "
            "ADD COLUMN IF NOT EXISTS response_text TEXT"
        )
        cur.execute(
            "ALTER TABLE hr2.measurement "
            "ADD COLUMN IF NOT EXISTS thinking_text TEXT"
        )
    conn.commit()


def migrate_add_measurement_cap_column(conn: psycopg2.extensions.connection) -> None:
    """Idempotently add requested_max_output to hr2.measurement.

    Records the output cap that was ACTUALLY requested for each call (e.g.
    8192 for the openai-compat default). hr2.health judges truncation
    against this per-row cap when present; existing rows keep NULL and fall
    back to the legacy proxy. Writers of measurement rows set it from the
    ChatRequest.max_output they sent.
    """
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE hr2.measurement "
            "ADD COLUMN IF NOT EXISTS requested_max_output INTEGER"
        )
    conn.commit()


def migrate() -> None:
    """Run all post-DDL migrations against a fresh default connection."""
    conn = connect()
    try:
        migrate_add_response_columns(conn)
        migrate_add_measurement_cap_column(conn)
    finally:
        conn.close()


def init_schema(conn: Optional[psycopg2.extensions.connection] = None,
                own_connection: bool = True) -> None:
    """Apply idempotent schema DDL.

    If ``conn`` is not provided, opens a fresh connection using the default
    credentials.  When ``own_connection`` is True the connection is committed
    and closed on exit.
    """
    close_after = False
    if conn is None:
        conn = connect()
        close_after = True
    try:
        with conn.cursor() as cur:
            cur.execute(_DDL)
        conn.commit()
        # Apply any post-DDL migrations on the same connection.
        migrate_add_response_columns(conn)
        migrate_add_measurement_cap_column(conn)
    finally:
        if own_connection and close_after:
            conn.close()
