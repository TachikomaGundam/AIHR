"""One-time migration from the retired database namespace."""

from __future__ import annotations

import psycopg2.extensions


def migrate_schema_namespace(conn: psycopg2.extensions.connection) -> None:
    """Rename the legacy schema in place while refusing ambiguous dual state."""
    with conn.cursor() as cur:
        cur.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'hr2')
                   AND NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'hr') THEN
                    ALTER SCHEMA hr2 RENAME TO hr;
                ELSIF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'hr2')
                      AND EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'hr') THEN
                    RAISE EXCEPTION 'both legacy and canonical HR schemas exist; merge them before continuing';
                END IF;
            END $$;
            """
        )
    conn.commit()


def migrate_run_status_columns(conn: psycopg2.extensions.connection) -> None:
    """Add status and failure_reason columns to hr.run if they don't exist."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'hr'")
        if not cur.fetchone():
            return

        cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'hr'
                      AND table_name = 'run'
                      AND column_name = 'status'
                ) THEN
                    ALTER TABLE hr.run ADD COLUMN status TEXT NOT NULL DEFAULT 'scored';
                END IF;
            END $$;
            """
        )

        cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'hr'
                      AND table_name = 'run'
                      AND column_name = 'failure_reason'
                ) THEN
                    ALTER TABLE hr.run ADD COLUMN failure_reason TEXT;
                END IF;
            END $$;
            """
        )
    conn.commit()


def migrate_measurement_scorer_columns(conn: psycopg2.extensions.connection) -> None:
    """Add grader provenance to legacy measurement rows."""
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE hr.measurement ADD COLUMN IF NOT EXISTS scorer_name TEXT NOT NULL DEFAULT 'unknown'"
        )
        cur.execute(
            "ALTER TABLE hr.measurement ADD COLUMN IF NOT EXISTS scorer_version TEXT"
        )
    conn.commit()
