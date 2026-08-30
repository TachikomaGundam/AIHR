"""Offline unit tests for hr.bench.engine_storage (fake conn, no DB).

EngineStorageMixin is pure SQL-driving code: every method takes a ``conn``
and issues INSERTs through ``conn.cursor()`` + ``conn.commit()``. A scripted
in-memory connection records the exact SQL/params, and the stage0 helpers it
delegates to are either run for real against that fake conn (they are
conn-only too) or swapped for recorders when they would read config (seat
seed path).
"""

from __future__ import annotations

import json

import pytest

from hr.bench.engine_results import BenchOutcome, ItemResult
from hr.bench.engine_storage import SEAT_CODE, EngineStorageMixin
from hr.bench.livebench import LIVEBENCH_BATTERIES, battery_code, battery_item_labels
from hr.graders.base import GRADER_VERSION


class FakeCursor:
    def __init__(self, conn: "FakeConn") -> None:
        self.conn = conn

    def execute(self, sql: str, params: object = None) -> None:
        self.conn.executed.append((sql, params))

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class FakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, object]] = []
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1


def _outcome(n_items: int = 2) -> BenchOutcome:
    return BenchOutcome(
        battery=LIVEBENCH_BATTERIES[0],
        model_id="acme/m1",
        score=100.0,
        passed=True,
        latency_ms=12,
        tokens_in=30,
        tokens_out=9,
        response_text="ok\x00",
        thinking_text="think",
        requested_max_output=2048,
        items=[
            ItemResult(
                label=f"item-{i}",
                item_id=f"tool_a.item.{i}",
                score=50.0 * i,
                passed=True,
            )
            for i in range(1, n_items + 1)
        ],
    )


def test_ensure_registered_upserts_all_batteries_items_links(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn()
    mixin = EngineStorageMixin()

    seen_seats: list[tuple] = []
    seen_batteries: list[tuple] = []
    seen_items: list[tuple] = []
    seen_seat_batteries: list[tuple] = []

    def fake_upsert_seat(c, seat_code: str, seat_name: str) -> None:
        seen_seats.append((seat_code, seat_name))

    def fake_upsert_battery(c, battery_code: str, description: str) -> str:
        seen_batteries.append((battery_code, description))
        return f"battery-{battery_code}"

    def fake_upsert_battery_item(c, battery_id: str, item_id: str, position: int) -> None:
        seen_items.append((battery_id, item_id, position))

    def fake_upsert_seat_battery(c, seat_code: str, battery_id: str, n_initial: int, n_max: int) -> None:
        seen_seat_batteries.append((seat_code, battery_id, n_initial, n_max))

    monkeypatch.setattr("hr.stage0._upsert_battery", fake_upsert_battery)
    monkeypatch.setattr("hr.stage0._upsert_battery_item", fake_upsert_battery_item)
    monkeypatch.setattr("hr.stage0._upsert_seat", fake_upsert_seat)
    monkeypatch.setattr("hr.stage0._upsert_seat_battery", fake_upsert_seat_battery)

    mixin.ensure_registered(conn)

    assert seen_seats == [(SEAT_CODE, "Stage-0 sweep")]
    assert len(seen_batteries) == len(LIVEBENCH_BATTERIES)
    total_items = sum(len(battery_item_labels(b)) for b in LIVEBENCH_BATTERIES)
    assert len(seen_items) == total_items
    assert len(seen_seat_batteries) == len(LIVEBENCH_BATTERIES)

    item_pool_sqls = [p for p in conn.executed if "hr.item_pool" in p[0]]
    assert len(item_pool_sqls) == total_items
    assert item_pool_sqls[0][1][4] == "livebench"
    assert json.loads(item_pool_sqls[0][1][5]) == {"kind": "livebench"}
    assert conn.commits == total_items


def test_store_writes_run_and_measurements_per_item() -> None:
    conn = FakeConn()
    mixin = EngineStorageMixin()
    outcome = _outcome(n_items=2)

    mixin.store(conn, "sweep-1", "acme/m1", LIVEBENCH_BATTERIES[0], outcome)

    sqls = [sql for sql, _ in conn.executed]
    assert sum("INSERT INTO hr.provider" in s for s in sqls) == 1
    assert sum("INSERT INTO hr.model" in s for s in sqls) == 1
    assert sum("INSERT INTO hr.sweep" in s for s in sqls) == 1
    assert sum("INSERT INTO hr.run" in s for s in sqls) == 1
    assert sum("INSERT INTO hr.measurement" in s for s in sqls) == 2

    run_params = [p for s, p in conn.executed if "INSERT INTO hr.run" in s][0]
    assert run_params[0].startswith("run-")
    expected_battery = f"battery-{battery_code(LIVEBENCH_BATTERIES[0])}"
    assert run_params[1:5] == ("sweep-1", "acme/m1", expected_battery, 1)
    assert run_params[7] == outcome.tokens_in + outcome.tokens_out

    meas_params = [p for s, p in conn.executed if "INSERT INTO hr.measurement" in s]
    assert meas_params[0][3] == 1
    assert meas_params[0][4] == 50.0
    assert meas_params[0][9] == "ok"
    assert meas_params[0][10] == "think"
    assert meas_params[0][11] == 2048
    assert meas_params[0][12] == f"livebench:{battery_code(LIVEBENCH_BATTERIES[0])}"
    assert meas_params[0][13] == GRADER_VERSION
    assert meas_params[1][2] == "tool_a.item.2"
    assert meas_params[1][4] == 100.0
    assert conn.commits == 6


@pytest.mark.parametrize("status", ["inconclusive", "not_applicable"])
def test_store_does_not_persist_unscored_measurements(status: str) -> None:
    conn = FakeConn()
    outcome = _outcome(n_items=2)
    outcome.status = status

    EngineStorageMixin().store(
        conn, "sweep-1", "acme/m1", LIVEBENCH_BATTERIES[0], outcome
    )

    sqls = [sql for sql, _ in conn.executed]
    assert sum("INSERT INTO hr.run" in sql for sql in sqls) == 1
    assert sum("INSERT INTO hr.measurement" in sql for sql in sqls) == 0
