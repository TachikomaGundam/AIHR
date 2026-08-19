#!/usr/bin/env python3
"""Register the 8 livebench batteries (hr bench, task 12) in the hr2 schema.

Idempotent row-only registration (no DDL), mirroring
scripts/register_tool_b_battery.py:
  * hr2.battery         -> battery-livebench_<name> (v1) x8
  * hr2.item_pool       -> one row per graded unit (52 total:
                          13 code_gen + 13 reasoning + 16 instruction_follow
                          + 1 tool_use + 3 long_context + 1 vision + 1 speed
                          + 4 long_horizon)
  * hr2.battery_item    -> 1 row per item (weight 1.0, position ordered)
  * hr2.seat_battery    -> ('_stage0_sweep', battery, n_initial, n_max) with
                          honest bounds: n_initial=min(3, items),
                          n_max=min(10, items)

Also upserts the pseudo-seat first (fresh DBs have no seat rows — the FK
self-heal pattern the tool_b script learned in task 15).

Required battery rows in configs/thresholds.yaml are validated at the start
(missing entry -> explicit config error naming the battery).

Run: python3 scripts/register_livebench_batteries.py
     (or with HR_COMPOSE_FILE=/path/to/docker-compose.yml for the live DB)
"""
from __future__ import annotations

from hr.bench.engine import LivebenchEngine
from hr.bench.livebench import LIVEBENCH_BATTERIES
from hr.models import BenchmarkCategory


def main() -> int:
    from hr.stage0 import _connect

    engine = LivebenchEngine()
    engine.require_thresholds(list(LIVEBENCH_BATTERIES))
    conn = None
    try:
        conn = _connect()
        engine.ensure_registered(conn)
        print(
            f"ok: {len(LIVEBENCH_BATTERIES)} livebench batteries registered "
            f"({sum(len(engine_items(b)) for b in LIVEBENCH_BATTERIES)} items)"
        )
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def engine_items(b: BenchmarkCategory) -> list[str]:
    from hr.bench.livebench import battery_item_labels

    return battery_item_labels(b)


if __name__ == "__main__":
    raise SystemExit(main())