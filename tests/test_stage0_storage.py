"""Offline unit tests for hr.stage0_storage SQL helpers (fake connection).

Every helper takes a ``conn`` and blindly issues INSERTs through
``conn.cursor()`` + ``conn.commit()`` — a scripted in-memory connection
records the exact SQL/params executed, so no database is ever touched.
The two DB-bootstrap helpers are covered via monkeypatched ``hr.db``
functions; ``hr.seats.seed.upsert_seat`` is likewise replaced.
"""

from __future__ import annotations

import json

import pytest

import hr.stage0_storage as storage
from hr.items.schema import GradingSpec, ItemEnvelope, ItemMeta


class FakeCursor:
    """Records every execute() against its parent FakeConn.

    ``fetchone`` serves the scorer-identity lookup the measurement writer
    performs when no scorer is passed explicitly: the fake item_pool row
    reports kind ``tool_a`` unless a test overrides ``kind_row``.
    """

    def __init__(self, conn: "FakeConn", kind_row: tuple[str, ...] = ("tool_a",)) -> None:
        self.conn = conn
        self.kind_row = kind_row

    def execute(self, sql: str, params: object = None) -> None:
        self.conn.executed.append((sql, params))

    def fetchone(self) -> tuple[str, ...] | None:
        return self.kind_row

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class FakeConn:
    """Connection stand-in: cursor() context managers + commit counting."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, object]] = []
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1


def _envelope(item_key: str = "tool_a.calc.001", seats: list[str] | None = None) -> ItemEnvelope:
    return ItemEnvelope(
        item_key=item_key,
        type="tool_a",
        tier=2,
        payload={},
        grading=GradingSpec(grader="exact_match@1.0"),
        meta=ItemMeta(seats=seats if seats is not None else ["quick"]),
    )


# ---------------------------------------------------------------------------
# upserts
# ---------------------------------------------------------------------------
def test_upsert_battery_returns_battery_id() -> None:
    conn = FakeConn()
    battery_id = storage._upsert_battery(conn, "code_gen", "Code gen battery")
    assert battery_id == "battery-code_gen"
    (sql, params) = conn.executed[0]
    assert "INSERT INTO hr.battery" in sql
    assert params == ("battery-code_gen", "code_gen", "v1", "Code gen battery")


def test_upsert_seat_delegates_to_seats_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn()
    seen: list[tuple[object, str, str]] = []

    def fake_upsert_seat(c, seat_code: str, seat_name: str) -> None:
        seen.append((c, seat_code, seat_name))

    monkeypatch.setattr("hr.seats.seed.upsert_seat", fake_upsert_seat)
    storage._upsert_seat(conn, "quick", "Quick seat")
    assert seen == [(conn, "quick", "Quick seat")]
    assert conn.executed == []  # seed owns the SQL


# ---------------------------------------------------------------------------
# inserts
# ---------------------------------------------------------------------------
def test_sanitize_db_text_strips_control_bytes_keeps_tabs() -> None:
    assert storage._sanitize_db_text(None) is None
    assert storage._sanitize_db_text("plain") == "plain"
    assert storage._sanitize_db_text("a\x00b\x01c") == "abc"
    assert storage._sanitize_db_text("tab\tnl\ncr\r\x07ok") == "tab\tnl\ncr\rok"
    assert storage._sanitize_db_text("héllo ✓") == "héllo ✓"


def test_insert_measurement_sanitizes_text_and_records_max_output() -> None:
    conn = FakeConn()
    storage._insert_measurement(
        conn,
        "m-1",
        "run-1",
        "tool_a.calc.001",
        1,
        0.9,
        100,
        50,
        42,
        response_text="ok\x00",
        thinking_text="th\x01ink",
        requested_max_output=2048,
    )
    # first statement resolves the scorer identity from item_pool.kind
    (kind_sql, kind_params) = conn.executed[0]
    assert "SELECT kind FROM hr.item_pool" in kind_sql
    assert kind_params == ("tool_a.calc.001",)
    (sql, params) = conn.executed[1]
    assert "INSERT INTO hr.measurement" in sql
    assert params[0:8] == ("m-1", "run-1", "tool_a.calc.001", 1, 0.9, 100, 50, 42)
    assert params[8] is not None  # created_at
    assert params[9] == "ok"
    assert params[10] == "think"
    assert params[11] == 2048
    # kind tool_a -> schema_valid@1.0 — never 'unknown'
    assert params[12] == "schema_valid"
    assert params[13] == "1.0"


def test_insert_measurement_explicit_scorer_skips_kind_lookup() -> None:
    conn = FakeConn()
    storage._insert_measurement(
        conn,
        "m-2",
        "run-1",
        "tool_a.calc.001",
        1,
        0.9,
        100,
        50,
        42,
        scorer_name="livebench:code_gen",
        scorer_version="1.0.0",
    )
    (sql, params) = conn.executed[0]
    assert "INSERT INTO hr.measurement" in sql
    assert params[12] == "livebench:code_gen"
    assert params[13] == "1.0.0"


def test_resolve_scorer_identity_maps_kind_to_routed_grader() -> None:
    # routed kinds resolve to their _ROUTING grader spec (name@version)
    assert storage.resolve_scorer_identity("tool_a") == ("schema_valid", "1.0")
    assert storage.resolve_scorer_identity("reasoning") == ("constraint", "1.0")
    assert storage.resolve_scorer_identity("factuality_qa") == ("exact_match", "1.0")


def test_resolve_scorer_identity_unknown_kinds_are_explicit_not_unknown() -> None:
    # kinds with no grader routing must never masquerade as 'unknown'
    assert storage.resolve_scorer_identity("longctx") == ("no_grader", None)
    assert storage.resolve_scorer_identity("replay") == ("no_grader", None)
    assert storage.resolve_scorer_identity("bogus-kind") == ("no_grader", None)


def test_insert_infra_incident_uses_uuid_and_json_details() -> None:
    conn = FakeConn()
    storage._insert_infra_incident(conn, "run-1", "timeout", {"retries": 6})
    (sql, params) = conn.executed[0]
    assert "INSERT INTO hr.infra_incident" in sql
    assert params[0].startswith("inc-")
    assert params[1:4] == ("run-1", "timeout", json.dumps({"retries": 6}))

    storage._insert_infra_incident(conn, "run-1", "timeout", {"retries": 1})
    assert conn.executed[1][1][0] != params[0]  # fresh uuid per incident


def test_ensure_provider_model_records_splits_slugs_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn()
    monkeypatch.setattr(
        storage, "provider_display_names", lambda: {"acme": "Acme Corp"}
    )
    mapping = storage._ensure_provider_model_records(
        conn, ("acme/m1", "acme/m2", "bare-model")
    )
    assert mapping == {"acme/m1": "acme", "acme/m2": "acme", "bare-model": "unknown"}
    provider_sqls = [p for p in conn.executed if "hr.provider" in p[0]]
    model_sqls = [p for p in conn.executed if "hr.model" in p[0]]
    assert ("acme", "Acme Corp") in [p[1] for p in provider_sqls]
    assert ("unknown", "unknown") in [p[1] for p in provider_sqls]
    assert ("bare-model", "unknown", "bare-model") in [p[1] for p in model_sqls]
    assert conn.commits == 6