#!/usr/bin/env python3
"""Register the Stage-0 ``tool_b`` battery (10 existing itemrepo items).

Idempotent row-only registration (no DDL):
* hr.battery          → battery-tool_b (v1)
* hr.item_pool        → 10 tool_b item rows (item_id = item_key, kind tool_b)
* hr.battery_item     → 1 row per item (weight 1.0, position 0..9)
* hr.seat_battery     → ('_stage0_sweep', battery-tool_b, 3, 10)

All upserts are ON CONFLICT DO NOTHING, so re-running is safe. Mirrors the
upsert path stage0.run_sweep uses when recording to the DB.
"""
from __future__ import annotations

from hr.calibrate import load_item_repo
from hr.config import itemrepo_path

#: Seat that owns Stage-0 battery links (same sentinel as stage0 runner).
SEAT_CODE = "_stage0_sweep"


def main() -> int:
    from hr.stage0 import (
        _connect,
        _upsert_battery,
        _upsert_battery_item,
        _upsert_item_pool,
        _upsert_seat,
        _upsert_seat_battery,
    )

    conn = None
    try:
        conn = _connect()
        battery_id = _upsert_battery(conn, "tool_b", "Stage-0 tool_b battery")

        items = load_item_repo(itemrepo_path(), batteries=["tool_b"])["tool_b"]
        if not items:
            print("error: no tool_b items found in itemrepo")
            return 1
        items.sort(key=lambda e: e.item_key)
        for pos, env in enumerate(items):
            _upsert_item_pool(conn, env)
            _upsert_battery_item(conn, battery_id, env.item_key, pos)

        # Ensure the sweep pseudo-seat exists (fresh DBs have no seat rows).
        _upsert_seat(conn, SEAT_CODE, "Stage-0 sweep")
        _upsert_seat_battery(conn, SEAT_CODE, battery_id)
        print(
            f"ok: battery {battery_id} + {len(items)} battery_item rows "
            f"+ seat_battery link ({SEAT_CODE})"
        )
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
