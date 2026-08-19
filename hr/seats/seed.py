"""Seed ``hr2.seat`` from ``configs/seats.yaml`` — the hard-gate plumbing.

Single source of truth
----------------------
The 18 real seats are authored in :file:`configs/seats.yaml`; ``seed_seats()``
makes ``hr2.seat`` mirror that file exactly. Every managed column of every
yaml seat is overwritten on ``seat_code`` conflict (``INSERT ... ON CONFLICT
(seat_code) DO UPDATE``), so re-running the seed is idempotent: any drift in
the seat rows is healed back to the yaml values and the row count stays 18.

YAML → column mapping (verified against the ``hr2.seat`` DDL in ``hr/db.py``):

=======================  =============================
seats.yaml field         hr2.seat column
=======================  =============================
seat_code                seat_code (TEXT PK)
seat_name                seat_name
domain                   domain
domain_specificity       domain_specificity (NUMERIC(4,3))
cost_tier                cost_tier
budget_tier              budget_tier
required_capabilities    required_capabilities (JSONB)
ctx_p95                  ctx_p95_tokens (INTEGER)
=======================  =============================

Gate state as of the TODO-9 commit (data state, not code)
---------------------------------------------------------
* **14 of the 18 seats carry an EMPTY ``required_capabilities`` list**, so
  their capability hard gates stay **inert** until ``configs/seats.yaml`` is
  enriched with capability data — that is a follow-up *data* edit, NOT part
  of this plan.
* 4 seats already declare capabilities and their capability gates are live
  exactly as authored: ``artistry`` / ``visual_engineering`` /
  ``multimodal_looker`` → ``[vision]``; ``hephaestus`` → ``[tools]``.
* ``ctx_p95_tokens`` is populated for **all 18** seats → context gates become
  REAL immediately after seeding.

Scope rails
-----------
* Only ``hr2.seat`` is written. The 18 legacy ``public.hr_assignments`` rows
  are NOT modified; no data migration of any kind happens here.
* Sweep pseudo-seats (``_stage0_sweep`` / ``_stage1_finals``) are not in the
  yaml — ``upsert_seat()`` keeps a minimal generic fallback for them so the
  Stage-0/1 scheduling rows are unchanged (see stage0/1 callers).

Everything resolves through the unified config layer (``hr.config``): the
yaml via ``load_yaml("seats.yaml")`` → ``config_path()`` (zero hardcoded
paths), the DB via ``db_dsn()`` (zero password literals).
"""
from __future__ import annotations

import json
import sys
from typing import Any, Sequence

import psycopg2

from hr.config import db_dsn, load_yaml

# Managed columns, in EXACT hr2.seat DDL order (mirrors hr/db.py DDL block).
_MANAGED_COLUMNS = (
    "seat_name",
    "domain",
    "domain_specificity",
    "cost_tier",
    "budget_tier",
    "required_capabilities",
    "ctx_p95_tokens",
)

# Generic fallback shape for sweep pseudo-seats absent from seats.yaml —
# deliberately identical to the retired stage0._upsert_seat generic seeding.
_FALLBACK_VALUES = ("general", 0.5, "mid", "mid", "[]", None)


def load_seats() -> list[dict[str, Any]]:
    """Load the 18 seat records from ``configs/seats.yaml`` (unified layer)."""
    data = load_yaml("seats.yaml")
    seats = data.get("seats")
    if not isinstance(seats, list) or not seats:
        raise ValueError(
            "configs/seats.yaml carries no 'seats' list "
            f"(got {type(seats).__name__ if seats is not None else None})"
        )
    return list(seats)


def seed_seats(conn) -> int:
    """Idempotent full upsert of every yaml seat into ``hr2.seat``.

    Returns the number of yaml seats upserted (18 today). Rows tracked in the
    DB but absent from the yaml are left in place (no deletes — the sweep
    seats and any future data must never be destroyed by a seed).
    """
    seats = load_seats()
    _UPSEAT_SQL = _build_upsert_sql(update=True)
    with conn.cursor() as cur:
        for seat in seats:
            cur.execute(_UPSEAT_SQL, _row_from_seat(seat))
    conn.commit()
    return len(seats)


def upsert_seat(conn, seat_code: str, seat_name: str) -> None:
    """Upsert ONE seat row, yaml-first (shared path for stage0/stage1).

    Real seats (``seat_code`` present in ``configs/seats.yaml``) are written
    with their full yaml values and ``ON CONFLICT DO UPDATE``, so later
    ``seed_seats()`` runs heal them to the yaml. Codes absent from the yaml
    (Stage-0/1 sweep pseudo-seats) fall back to the legacy generic shape with
    ``ON CONFLICT DO NOTHING``, preserving the pre-TODO-9 behavior exactly.
    """
    seat = _seat_by_code().get(seat_code)
    if seat is not None:
        sql, row = _build_upsert_sql(update=True), _row_from_seat(seat)
    else:
        if not seat_name:
            raise ValueError(
                f"seat_code {seat_code!r} not in configs/seats.yaml and no "
                f"fallback seat_name given"
            )
        # Legacy generic sweep-seat path (ON CONFLICT DO NOTHING, as before).
        sql, row = _build_upsert_sql(update=False), (
            seat_code,
            seat_name,
            *_FALLBACK_VALUES,
        )
    with conn.cursor() as cur:
        cur.execute(sql, row)
    conn.commit()


def _seat_by_code() -> dict[str, dict[str, Any]]:
    return {str(seat.get("seat_code")): seat for seat in load_seats()}


def _row_from_seat(seat: dict[str, Any]) -> tuple:
    """Map one yaml seat record onto the hr2.seat column tuple."""
    capabilities = json.dumps(seat.get("required_capabilities") or [])
    ctx = seat.get("ctx_p95")
    return (
        str(seat["seat_code"]),
        str(seat["seat_name"]),
        str(seat["domain"]),
        float(seat["domain_specificity"]),
        str(seat["cost_tier"]),
        str(seat["budget_tier"]),
        capabilities,
        int(ctx) if ctx is not None else None,
    )


def _build_upsert_sql(update: bool) -> str:
    """Full-column seat upsert; ``update=True`` → DO UPDATE (yaml seats),
    ``update=False`` → DO NOTHING (generic fallback)."""
    cols = ", ".join(_MANAGED_COLUMNS)
    conflict = (
        "DO UPDATE SET "
        + ", ".join(f"{c} = EXCLUDED.{c}" for c in _MANAGED_COLUMNS)
        if update
        else "DO NOTHING"
    )
    return (
        f"INSERT INTO hr2.seat (seat_code, {cols}) "
        f"VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s) "
        f"ON CONFLICT (seat_code) {conflict}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry: seed ``hr2.seat`` from ``configs/seats.yaml``.

    Usage: ``python3 scripts/seed_seats.py`` or ``python3 -m hr.seats.seed``.
    Exit 0 on success; 1 with ``error: …`` on stderr when the DSN cannot be
    resolved or the seed fails.
    """
    try:
        conn = psycopg2.connect(db_dsn())
    except RuntimeError as exc:  # credential/DSN resolution failure only
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        n = seed_seats(conn)
    except Exception as exc:  # connection/statement errors propagate visibly
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"seeded {n} seats into hr2.seat from configs/seats.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())