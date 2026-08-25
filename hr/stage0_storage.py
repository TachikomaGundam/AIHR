from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from hr.fleet import provider_display_names
from hr.items.schema import ItemEnvelope


def _init_db() -> None:
    """Initialize the HR schema in the configured database."""
    from hr.db import init_schema

    init_schema()


def _connect():
    from hr.db import connect

    return connect()


def _upsert_provider(conn, provider_id: str, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
                "INSERT INTO hr.provider (provider_id, name) VALUES (%s, %s) "
            "ON CONFLICT (provider_id) DO NOTHING",
            (provider_id, name),
        )
    conn.commit()


def _upsert_model(conn, model_id: str, provider_id: str, model_name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
                "INSERT INTO hr.model (model_id, provider_fk, model_name) VALUES (%s, %s, %s) "
            "ON CONFLICT (model_id) DO NOTHING",
            (model_id, provider_id, model_name),
        )
    conn.commit()


def _upsert_battery(conn, battery_code: str, description: str) -> str:
    battery_id = f"battery-{battery_code}"
    with conn.cursor() as cur:
        cur.execute(
                "INSERT INTO hr.battery (battery_id, battery_code, version, description) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (battery_id) DO NOTHING",
            (battery_id, battery_code, "v1", description),
        )
    conn.commit()
    return battery_id


def _upsert_seat(conn, seat_code: str, seat_name: str) -> None:
    """Upsert one seat row through the shared yaml-driven seat path.

    Real seats (codes present in ``configs/seats.yaml``) get their full typed
    values from the yaml via ``hr.seats.seed`` (``ON CONFLICT DO UPDATE``);
    sweep pseudo-seats absent from the yaml (``_stage0_sweep``,
    ``_stage1_finals``) keep the legacy generic fallback shape
    (``ON CONFLICT DO NOTHING``) so Stage-0/1 scheduling rows are unchanged.
    """
    from hr.seats.seed import upsert_seat

    upsert_seat(conn, seat_code, seat_name)


def _upsert_item_pool(conn, env: ItemEnvelope) -> None:
    """Upsert the item_pool row for an envelope (item_id = item_key)."""
    domain = env.item_key.split(".")[0] if "." in env.item_key else "general"
    with conn.cursor() as cur:
        cur.execute(
                    "INSERT INTO hr.item_pool (item_id, item_code, version, domain, kind, json_meta) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb) ON CONFLICT (item_id) DO NOTHING",
            (
                env.item_key,
                env.item_key,
                "v1",
                domain,
                env.type.value,
                json.dumps({"tier": env.tier, "seats": list(env.meta.seats or [])}),
            ),
        )
    conn.commit()


def _upsert_battery_item(conn, battery_id: str, item_id: str, position: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
                    "INSERT INTO hr.battery_item (battery_id, item_id, weight, position) "
            "VALUES (%s, %s, 1.0, %s) ON CONFLICT DO NOTHING",
            (battery_id, item_id, position),
        )
    conn.commit()


def _upsert_seat_battery(
    conn, seat_code: str, battery_id: str, n_initial: int = 3, n_max: int = 10
) -> None:
    with conn.cursor() as cur:
        cur.execute(
                "INSERT INTO hr.seat_battery (seat_code, battery_id, n_initial, n_max) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (seat_code, battery_id, n_initial, n_max),
        )
    conn.commit()


def _insert_sweep(conn, sweep_id: str, seat_code: str, purpose: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hr.sweep (sweep_id, seat_code, purpose, created_at) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (sweep_id, seat_code, purpose, datetime.now(timezone.utc)),
        )
    conn.commit()


def _insert_run(
    conn,
    run_id: str,
    sweep_id: str,
    model_id: str,
    battery_id: str,
    round_num: int,
    total_tokens: int,
    total_cost_cny: float,
    infra_ok: bool,
    status: str = "scored",
    failure_reason: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
                "INSERT INTO hr.run (run_id, sweep_id, model_id, battery_id, round, "
            "started_at, finished_at, total_tokens, total_cost_cny, infra_ok, status, failure_reason) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (
                run_id,
                sweep_id,
                model_id,
                battery_id,
                round_num,
                now,
                now,
                total_tokens,
                total_cost_cny,
                infra_ok,
                status,
                failure_reason,
            ),
        )
    conn.commit()


def _sanitize_db_text(text: str | None) -> str | None:
    """Strip characters PostgreSQL ``text`` rejects before DB insert.

    Models occasionally emit NUL (0x00) and other raw control bytes in their
    output; psycopg2 raises "A string literal cannot contain NUL (0x00)
    characters" when such bytes reach an INSERT. Remove NUL and the other
    disallowed C0 controls (keeping \t \n \r), since they carry no answer content.
    """
    if text is None:
        return None
    return "".join(
        ch for ch in text if ch in ("\t", "\n", "\r") or ord(ch) >= 0x20
    )


def resolve_scorer_identity(item_kind: str) -> tuple[str, str | None]:
    """Map an item_pool ``kind`` to the grader identity that scored it.

    The grading router for every item type carries the grader spec
    (``name@version``) in ``hr.calibration_items._ROUTING``; this mirrors the
    same lookup ``call_and_grade`` performs so persisted scorer_name/version
    always match the grader that actually produced the score.

    Never returns ``'unknown'``: an item kind with no grader routing (e.g.
    ``longctx``/``replay``) resolves to ``('no_grader', None)`` so provenance
    is explicit instead of silently mislabeled.
    """
    from hr.calibration_items import _ROUTING
    from hr.graders.base import GRADER_VERSION
    from hr.items.schema import ItemType

    try:
        item_type = ItemType(item_kind)
    except ValueError:
        return ("no_grader", None)
    routing = _ROUTING.get(item_type)
    if routing is None:
        return ("no_grader", None)
    spec = routing[0]
    if "@" in spec:
        name, version = spec.split("@", 1)
        return (name, version)
    return (spec, GRADER_VERSION)


def _insert_measurement(
    conn,
    measurement_id: str,
    run_id: str,
    item_id: str,
    repetition: int,
    score: float,
    tokens_in: int,
    tokens_out: int,
    latency_ms: int,
    response_text: str | None = None,
    thinking_text: str | None = None,
    requested_max_output: int | None = None,
    scorer_name: str | None = None,
    scorer_version: str | None = None,
) -> None:
    """Insert one measurement row (ON CONFLICT DO NOTHING).

    ``requested_max_output`` records the output cap that was ACTUALLY sent on
    the wire (e.g. the livebench bench writer sets it from ChatRequest.
    max_output); ``hr.health`` judges truncation against this per-row cap when
    present. Existing callers omit it -> NULL (legacy-proxy fallback).

    ``scorer_name`` defaulting to None resolves the scorer identity from the
    item's ``kind`` (never persists ``unknown``); callers that know their
    grader (bench writer, stage0 loop) pass it explicitly.
    """
    response_text = _sanitize_db_text(response_text)
    thinking_text = _sanitize_db_text(thinking_text)
    if scorer_name is None:
        scorer_name, scorer_version = _resolve_scorer_for_item(conn, item_id)
    with conn.cursor() as cur:
        cur.execute(
                "INSERT INTO hr.measurement (measurement_id, run_id, item_id, repetition, "
                "score, tokens_in, tokens_out, latency_ms, created_at, "
            "response_text, thinking_text, requested_max_output, scorer_name, scorer_version) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (
                measurement_id,
                run_id,
                item_id,
                repetition,
                score,
                tokens_in,
                tokens_out,
                latency_ms,
                datetime.now(timezone.utc),
                response_text,
                thinking_text,
                requested_max_output,
                scorer_name,
                scorer_version,
            ),
        )
    conn.commit()


def _resolve_scorer_for_item(conn, item_id: str) -> tuple[str, str | None]:
    """Look up the item's kind and map it to the routed grader identity."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind FROM hr.item_pool WHERE item_id = %s", (item_id,)
        )
        row = cur.fetchone()
    if not row:
        return ("no_grader", None)
    return resolve_scorer_identity(row[0])


def _insert_infra_incident(conn, run_id: str, kind: str, details: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
                "INSERT INTO hr.infra_incident (incident_id, run_id, kind, details_json, recorded_at) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (
                f"inc-{uuid.uuid4()}",
                run_id,
                kind,
                json.dumps(details),
                datetime.now(timezone.utc),
            ),
        )
    conn.commit()


def _insert_separation(
    conn,
    separation_id: str,
    sweep_id: str,
    battery_id: str,
    model_a: str,
    model_b: str,
    p_separated: float,
    p_weak: float,
    p_tie: float,
    directional: bool = True,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
                    "INSERT INTO hr.separation (separation_id, sweep_id, battery_id, model_a, model_b, "
            "p_separated, p_weak, p_tie, directional, estimated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (
                separation_id,
                sweep_id,
                battery_id,
                model_a,
                model_b,
                p_separated,
                p_weak,
                p_tie,
                directional,
                datetime.now(timezone.utc),
            ),
        )
    conn.commit()


def _ensure_provider_model_records(conn, models: tuple[str, ...]) -> dict[str, str]:
    """Return a map of model_id -> provider_id, inserting records as needed."""
    # Provider display names come from the opencode config's `name` field
    # (dynamic derivation); providers absent from the config fall back to
    # their provider id.
    provider_names = provider_display_names()
    model_to_provider: dict[str, str] = {}
    for model_id in models:
        if "/" in model_id:
            provider_id, slug = model_id.split("/", 1)
        else:
            provider_id, slug = "unknown", model_id
        provider_names.setdefault(provider_id, provider_id)
        _upsert_provider(conn, provider_id, provider_names[provider_id])
        _upsert_model(conn, model_id, provider_id, slug)
        model_to_provider[model_id] = provider_id
    return model_to_provider


# ---------------------------------------------------------------------------
# Sweep core
# ---------------------------------------------------------------------------
