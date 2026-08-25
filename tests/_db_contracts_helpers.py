"""Shared seeding helpers for the live-schema DB contract tests (T2).

The db-marked contract tests build a linked provider -> model -> seat ->
sweep -> run -> measurement chain on the module-scoped ``scratch_db``
Postgres fixture. Wherever the module under test IS a production SQL writer
(``hr.stage0_storage``), seeding goes through that production code so the
round trip exercises the real SQL; rows the production modules do not write
(control_model, bank item_pool metadata, calibration_event, …) are inserted
with explicit ``INSERT`` helpers defined here.
"""
from __future__ import annotations

import json
from typing import Any

import psycopg2
import psycopg2.extensions

import hr.stage0_storage as storage
from hr.items.schema import GradingSpec, ItemEnvelope, ItemMeta


def connect(dsn: str) -> psycopg2.extensions.connection:
    return psycopg2.connect(dsn)


def scalar(conn: psycopg2.extensions.connection, sql: str, params: Any = None) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        row = cur.fetchone()
        return row[0] if row else None


def columns(
    conn: psycopg2.extensions.connection,
) -> dict[str, set[str]]:
    """Map table_name -> set(column_name) for every ``hr.*`` table."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'hr'"
        )
        out: dict[str, set[str]] = {}
        for table, column in cur.fetchall():
            out.setdefault(str(table), set()).add(str(column))
    return out


def seed_item_pool(
    conn: psycopg2.extensions.connection,
    item_id: str,
    *,
    domain: str = "tool_a",
    meta: dict[str, Any] | None = None,
) -> None:
    """Explicit item_pool row (production writers only insert envelopes)."""
    payload = meta if meta is not None else {"tier": 2, "seats": ["quick"]}
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hr.item_pool (item_id, item_code, version, domain, kind, json_meta) "
            "VALUES (%s, %s, 'v1', %s, %s, %s::jsonb) ON CONFLICT (item_id) DO NOTHING",
            (item_id, item_id, domain, domain, json.dumps(payload)),
        )
    conn.commit()


def seed_envelope_item(
    conn: psycopg2.extensions.connection,
    item_key: str = "tool_a.calc.001",
) -> None:
    """item_pool row through the PRODUCTION envelope writer."""
    env = ItemEnvelope(
        item_key=item_key,
        type="tool_a",
        tier=2,
        payload={},
        grading=GradingSpec(grader="exact_match@1.0"),
        meta=ItemMeta(seats=["quick"]),
    )
    storage._upsert_item_pool(conn, env)


def seed_provider_models(
    conn: psycopg2.extensions.connection,
    models: tuple[str, ...],
) -> None:
    """provider + model rows through the PRODUCTION writer."""
    storage._ensure_provider_model_records(conn, models)


def seed_control_model(
    conn: psycopg2.extensions.connection, provider_fk: str, model_id: str
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hr.control_model (provider_fk, model_id) "
            "VALUES (%s, %s) ON CONFLICT (provider_fk) DO NOTHING",
            (provider_fk, model_id),
        )
    conn.commit()


def seed_seat(conn: psycopg2.extensions.connection, seat_code: str, seat_name: str) -> None:
    """Seat row through the PRODUCTION yaml-driven path."""
    storage._upsert_seat(conn, seat_code, seat_name)


def seed_battery(conn: psycopg2.extensions.connection, code: str) -> str:
    return storage._upsert_battery(conn, code, f"{code} battery")


def seed_battery_item(
    conn: psycopg2.extensions.connection, battery_id: str, item_id: str, position: int
) -> None:
    storage._upsert_battery_item(conn, battery_id, item_id, position)


def seed_sweep(conn: psycopg2.extensions.connection, sweep_id: str, seat_code: str = "oracle") -> None:
    storage._insert_sweep(conn, sweep_id, seat_code, "primary")


def seed_run(
    conn: psycopg2.extensions.connection,
    run_id: str,
    sweep_id: str,
    model_id: str,
    battery_id: str,
    *,
    status: str = "scored",
    failure_reason: str | None = None,
) -> None:
    storage._insert_run(
        conn,
        run_id,
        sweep_id,
        model_id,
        battery_id,
        round_num=1,
        total_tokens=1000,
        total_cost_cny=0.01,
        infra_ok=True,
        status=status,
        failure_reason=failure_reason,
    )


def seed_measurement(
    conn: psycopg2.extensions.connection,
    measurement_id: str,
    run_id: str,
    item_id: str,
    *,
    repetition: int = 1,
    score: float = 0.8,
    tokens_out: int = 500,
    latency_ms: int = 250,
    response_text: str = "The answer is 42.",
    requested_max_output: int | None = 8192,
    scorer_name: str = "unknown",
    scorer_version: str | None = None,
) -> None:
    storage._insert_measurement(
        conn,
        measurement_id,
        run_id,
        item_id,
        repetition,
        score,
        tokens_in=100,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        response_text=response_text,
        thinking_text=None,
        requested_max_output=requested_max_output,
        scorer_name=scorer_name,
        scorer_version=scorer_version,
    )