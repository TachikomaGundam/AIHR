r"""
Canonical HR schema DDL, split into two idempotent phases (W4-fix contract):

* ``DDL_SCHEMA`` — namespace + ``CREATE TABLE IF NOT EXISTS`` statements with
  their inline constraints. Safe on any database: fresh ones get every table
  with the full column set, legacy ones are no-ops.
* ``DDL_INDEXES`` — every ``CREATE INDEX`` statement, executed LAST.

The ordering contract for ``hr.db.init_schema`` is CREATE TABLES → column-add
migrations → CREATE INDEXES. Indexes must never be created before the columns
they reference exist on a pre-existing (pre-migration) table; the shipped
``migrate_add_calibration_measurement_columns`` adds pool_hash/anchor to a
legacy ``hr.calibration_event``, and ``calibration_anchor_measurement_idx``
depends on those columns. Fresh databases carry the columns in the CREATE
TABLE, so they only ever hit the ordering issue during an in-place upgrade.
``DDL`` keeps the legacy full-string export (tables + indexes) so callers
that treat it as the complete schema specification keep working.
"""

DDL_SCHEMA = r"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'hr') THEN
        CREATE SCHEMA hr;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS hr.provider (
    provider_id  TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS hr.model (
    model_id          TEXT PRIMARY KEY,
    provider_fk       TEXT NOT NULL REFERENCES hr.provider(provider_id),
    model_name        TEXT NOT NULL,
    capabilities      JSONB NOT NULL DEFAULT '{}'::jsonb,
    default_rpm       INTEGER,
    default_cost_1k_in  NUMERIC(12, 6),
    default_cost_1k_out NUMERIC(12, 6),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS hr.control_model (
    provider_fk  TEXT PRIMARY KEY REFERENCES hr.provider(provider_id),
    model_id     TEXT NOT NULL REFERENCES hr.model(model_id),
    mode         TEXT NOT NULL DEFAULT 'primary'
);

CREATE TABLE IF NOT EXISTS hr.seat (
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

CREATE TABLE IF NOT EXISTS hr.item_pool (
    item_id    TEXT PRIMARY KEY,
    item_code  TEXT NOT NULL,
    version    TEXT NOT NULL DEFAULT 'v1',
    domain     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    json_meta  JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS hr.item (
    item_id            TEXT PRIMARY KEY REFERENCES hr.item_pool(item_id),
    model_id           TEXT NOT NULL REFERENCES hr.model(model_id),
    payload_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
    difficulty_class   TEXT NOT NULL DEFAULT 'mid',
    calibrated_se      NUMERIC(10, 6),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS hr.battery (
    battery_id   TEXT PRIMARY KEY,
    battery_code TEXT NOT NULL UNIQUE,
    version      TEXT NOT NULL DEFAULT 'v1',
    description  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS hr.battery_item (
    battery_id TEXT NOT NULL REFERENCES hr.battery(battery_id),
    item_id    TEXT NOT NULL REFERENCES hr.item_pool(item_id),
    weight     NUMERIC(6, 3) NOT NULL DEFAULT 1.0,
    position   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (battery_id, item_id)
);

CREATE TABLE IF NOT EXISTS hr.seat_battery (
    seat_code  TEXT NOT NULL REFERENCES hr.seat(seat_code),
    battery_id TEXT NOT NULL REFERENCES hr.battery(battery_id),
    n_initial  INTEGER NOT NULL DEFAULT 3,
    n_max      INTEGER NOT NULL DEFAULT 10,
    PRIMARY KEY (seat_code, battery_id)
);

CREATE TABLE IF NOT EXISTS hr.sweep (
    sweep_id    TEXT PRIMARY KEY,
    seat_code   TEXT NOT NULL REFERENCES hr.seat(seat_code),
    purpose     TEXT NOT NULL DEFAULT 'primary',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS hr.experiment_manifest (
    sweep_id      TEXT PRIMARY KEY REFERENCES hr.sweep(sweep_id),
    manifest_json JSONB NOT NULL,
    digest        TEXT NOT NULL,
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS hr.run (
    run_id         TEXT PRIMARY KEY,
    sweep_id       TEXT NOT NULL REFERENCES hr.sweep(sweep_id),
    model_id       TEXT NOT NULL REFERENCES hr.model(model_id),
    battery_id     TEXT NOT NULL REFERENCES hr.battery(battery_id),
    round          INTEGER NOT NULL DEFAULT 1,
    started_at     TIMESTAMPTZ,
    finished_at    TIMESTAMPTZ,
    total_tokens   INTEGER,
    total_cost_cny NUMERIC(12, 4),
    infra_ok       BOOLEAN NOT NULL DEFAULT TRUE,
    status         TEXT NOT NULL DEFAULT 'scored',
    failure_reason TEXT
);
CREATE TABLE IF NOT EXISTS hr.measurement (
    measurement_id TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL REFERENCES hr.run(run_id),
    item_id        TEXT NOT NULL REFERENCES hr.item_pool(item_id),
    repetition     INTEGER NOT NULL DEFAULT 1,
    score          NUMERIC(10, 6) NOT NULL,
    tokens_in      INTEGER,
    tokens_out     INTEGER,
    latency_ms     INTEGER,
    scorer_name    TEXT NOT NULL DEFAULT 'unknown',
    scorer_version TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, item_id, repetition)
);
CREATE TABLE IF NOT EXISTS hr.infra_incident (
    incident_id TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES hr.run(run_id),
    kind        TEXT NOT NULL,
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS hr.control_reading (
    reading_id      TEXT PRIMARY KEY,
    control_model_fk TEXT NOT NULL REFERENCES hr.model(model_id),
    battery_id      TEXT NOT NULL REFERENCES hr.battery(battery_id),
    round           INTEGER NOT NULL,
    read_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    result_json     JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS hr.separation (
    separation_id TEXT PRIMARY KEY,
    sweep_id      TEXT NOT NULL REFERENCES hr.sweep(sweep_id),
    battery_id    TEXT NOT NULL REFERENCES hr.battery(battery_id),
    model_a       TEXT NOT NULL REFERENCES hr.model(model_id),
    model_b       TEXT NOT NULL REFERENCES hr.model(model_id),
    p_separated   NUMERIC(6, 4) NOT NULL,
    p_weak        NUMERIC(6, 4) NOT NULL,
    p_tie         NUMERIC(6, 4) NOT NULL,
    directional   BOOLEAN NOT NULL DEFAULT FALSE,
    estimated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (model_a <> model_b)
);
CREATE TABLE IF NOT EXISTS hr.assignment (
    assignment_id TEXT PRIMARY KEY,
    seat_code     TEXT NOT NULL REFERENCES hr.seat(seat_code),
    primary_model TEXT NOT NULL REFERENCES hr.model(model_id),
    fallback1     TEXT REFERENCES hr.model(model_id),
    fallback2     TEXT REFERENCES hr.model(model_id),
    fallback3     TEXT REFERENCES hr.model(model_id),
    reason_json   JSONB NOT NULL DEFAULT '{}'::jsonb,
    assigned_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS hr.policy_override (
    override_id TEXT PRIMARY KEY,
    seat_code   TEXT NOT NULL REFERENCES hr.seat(seat_code),
    rule        TEXT NOT NULL,
    before_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    after_json  JSONB NOT NULL DEFAULT '{}'::jsonb,
    reason      TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS hr.calibration_event (
    event_id   TEXT PRIMARY KEY,
    item_id    TEXT NOT NULL REFERENCES hr.item_pool(item_id),
    kind       TEXT NOT NULL,
    before_se  NUMERIC(10, 6),
    after_se   NUMERIC(10, 6),
    evidence_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    pool_hash  TEXT,
    anchor     TEXT,
    battery    TEXT,
    tier       INTEGER,
    item_type  TEXT,
    score      NUMERIC(10, 6),
    passed     BOOLEAN,
    tokens_in  INTEGER,
    tokens_out INTEGER,
    latency_ms INTEGER,
    infra_failure TEXT,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS hr.judge_verdict (
    verdict_id     TEXT PRIMARY KEY,
    sweep_id       TEXT NOT NULL REFERENCES hr.sweep(sweep_id),
    item_id        TEXT NOT NULL REFERENCES hr.item_pool(item_id),
    model_id       TEXT NOT NULL REFERENCES hr.model(model_id),
    round          INTEGER NOT NULL DEFAULT 1,
    judgement_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (sweep_id, item_id, model_id, round)
);
"""

DDL_INDEXES = r"""
CREATE INDEX IF NOT EXISTS model_provider_idx ON hr.model (provider_fk);
CREATE INDEX IF NOT EXISTS item_pool_domain_idx ON hr.item_pool (domain);
CREATE INDEX IF NOT EXISTS item_model_idx ON hr.item (model_id);
CREATE INDEX IF NOT EXISTS sweep_seat_idx ON hr.sweep (seat_code);
CREATE INDEX IF NOT EXISTS run_sweep_idx  ON hr.run (sweep_id);
CREATE INDEX IF NOT EXISTS run_model_idx  ON hr.run (model_id);
CREATE INDEX IF NOT EXISTS measurement_run_idx ON hr.measurement (run_id);
CREATE INDEX IF NOT EXISTS measurement_item_score ON hr.measurement (item_id, score);
CREATE INDEX IF NOT EXISTS incident_run_idx ON hr.infra_incident (run_id);
CREATE INDEX IF NOT EXISTS separation_sweep_idx ON hr.separation (sweep_id);
CREATE INDEX IF NOT EXISTS assignment_seat_idx ON hr.assignment (seat_code);
CREATE INDEX IF NOT EXISTS calibration_item_idx ON hr.calibration_event (item_id);
CREATE UNIQUE INDEX IF NOT EXISTS calibration_anchor_measurement_idx
    ON hr.calibration_event (pool_hash, anchor, item_id)
    WHERE kind = 'anchor_measurement';
CREATE INDEX IF NOT EXISTS judge_sweep_idx ON hr.judge_verdict (sweep_id);
"""

# Legacy full-string export (tables + indexes) for callers that treat the
# DDL as one complete specification; init_schema executes the two phases
# separately with the migrations in between.
DDL = DDL_SCHEMA + DDL_INDEXES

__all__ = ["DDL", "DDL_INDEXES", "DDL_SCHEMA"]
