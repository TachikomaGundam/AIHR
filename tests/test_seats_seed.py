"""Seats seeding idempotency tests (hr-unification todo 26).

``hr.seats.seed`` mirrors ``configs/seats.yaml`` into ``hr.seat`` with
``INSERT ... ON CONFLICT (seat_code) DO UPDATE`` over the managed columns —
re-running the seed heals drifted rows back to the yaml and never changes
the row count. These tests run that contract against an in-memory fake
connection that simulates the two upsert shapes (``DO UPDATE`` for yaml
seats, ``DO NOTHING`` for the sweep pseudo-seat fallback), so no real DB
is required: ``seed_seats`` / ``upsert_seat`` are exercised end-to-end;
``hr.seats.seed.main`` is additionally proven DB-free by asserting it fails
cleanly (exit 1, "error: ...") when no DSN can be resolved.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hr.seats.seed import (
    _MANAGED_COLUMNS,
    _build_upsert_sql,
    _row_from_seat,
    load_seats,
    main,
    seed_seats,
    upsert_seat,
)

SEATS_YAML = """
seats:
  - seat_code: oracle
    seat_name: Oracle
    domain: reasoning
    domain_specificity: 0.95
    cost_tier: high
    budget_tier: high
    required_capabilities: [reasoning]
    ctx_p95: 200000
  - seat_code: quick
    seat_name: Quick
    domain: support
    domain_specificity: 0.8
    cost_tier: low
    budget_tier: low
    required_capabilities: []
    ctx_p95: null
"""

EXPECTED_ORACLE = (
    "oracle",
    "Oracle",
    "reasoning",
    0.95,
    "high",
    "high",
    '["reasoning"]',
    200000,
)
EXPECTED_QUICK = (
    "quick",
    "Quick",
    "support",
    0.8,
    "low",
    "low",
    "[]",
    None,
)


# ---------------------------------------------------------------------------
# Fake connection with real ON CONFLICT semantics
# ---------------------------------------------------------------------------


class _SeedStore:
    """In-memory seat table keyed by seat code."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple] = {}
        self.statements: list[str] = []


class _SeedCursor:
    def __init__(self, store: _SeedStore) -> None:
        self.store = store
        self.rowcount = 0

    def execute(self, sql: str, params=None) -> None:
        self.store.statements.append(sql)
        self.rowcount = 0
        if "INSERT INTO hr.seat" not in sql:
            return
        values = tuple(params or ())
        seat_code = str(values[0])
        if "DO NOTHING" in sql:
            if seat_code not in self.store.rows:
                self.store.rows[seat_code] = values
                self.rowcount = 1
            return
        # DO UPDATE (yaml seats): upsert semantics — replace the row.
        self.store.rows[seat_code] = values
        self.rowcount = 1

    def __enter__(self) -> _SeedCursor:
        return self

    def __exit__(self, *_args) -> bool:
        return False


class _SeedConn:
    def __init__(self, store: _SeedStore) -> None:
        self.store = store

    def cursor(self) -> _SeedCursor:
        return _SeedCursor(self.store)

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def configs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    hr_home = tmp_path / "hr"
    configs = hr_home / "configs"
    configs.mkdir(parents=True)
    (configs / "seats.yaml").write_text(SEATS_YAML, encoding="utf-8")
    monkeypatch.setenv("HR_HOME", str(hr_home))
    return configs


def _clear_dsn_envs(monkeypatch) -> None:
    monkeypatch.delenv("HR_DSN", raising=False)
    monkeypatch.delenv("HR_DB_PASSWORD", raising=False)
    monkeypatch.delenv("HR_COMPOSE_FILE", raising=False)
    monkeypatch.delenv("HR_DB_HOST", raising=False)
    monkeypatch.delenv("HR_DB_PORT", raising=False)
    monkeypatch.delenv("HR_DB_NAME", raising=False)
    monkeypatch.delenv("HR_DB_USER", raising=False)


# ---------------------------------------------------------------------------
# load_seats / row mapping
# ---------------------------------------------------------------------------


def test_load_seats_returns_yaml_seats(configs_dir: Path) -> None:
    seats = load_seats()
    assert [s["seat_code"] for s in seats] == ["oracle", "quick"]


def test_load_seats_missing_seats_list_raises(configs_dir: Path) -> None:
    (configs_dir / "seats.yaml").write_text("other: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no 'seats' list"):
        load_seats()


def test_load_seats_empty_seats_list_raises(configs_dir: Path) -> None:
    (configs_dir / "seats.yaml").write_text("seats: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no 'seats' list"):
        load_seats()


def test_load_seats_corrupt_yaml_raises(configs_dir: Path) -> None:
    (configs_dir / "seats.yaml").write_text("seats: [unclosed\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        load_seats()


def test_row_from_seat_maps_yaml_onto_ddl_columns() -> None:
    """ctx_p95 -> ctx_p95_tokens (int or None); capabilities -> JSONB string."""
    seat = {
        "seat_code": "oracle",
        "seat_name": "Oracle",
        "domain": "reasoning",
        "domain_specificity": 0.95,
        "cost_tier": "high",
        "budget_tier": "high",
        "required_capabilities": ["vision", "tools"],
        "ctx_p95": 262144,
    }
    row = _row_from_seat(seat)
    assert row[0] == "oracle"
    assert row[6] == '["vision", "tools"]'
    assert row[7] == 262144
    with_caps = dict(seat, ctx_p95=None)
    assert _row_from_seat(with_caps)[7] is None


# ---------------------------------------------------------------------------
# seed_seats: idempotent full upsert
# ---------------------------------------------------------------------------


def test_seed_seats_inserts_every_yaml_seat(configs_dir: Path) -> None:
    conn = _SeedConn(_SeedStore())
    n = seed_seats(conn)
    assert n == 2
    assert conn.store.rows["oracle"] == EXPECTED_ORACLE
    assert conn.store.rows["quick"] == EXPECTED_QUICK
    # every statement goes through the ON CONFLICT DO UPDATE upsert
    assert all("ON CONFLICT (seat_code) DO UPDATE" in s for s in conn.store.statements)


def test_seed_seats_idempotent_second_run_changes_nothing(
    configs_dir: Path,
) -> None:
    conn = _SeedConn(_SeedStore())
    seed_seats(conn)
    snapshot = dict(conn.store.rows)
    seed_seats(conn)
    assert conn.store.rows == snapshot
    assert len(conn.store.rows) == 2


def test_seed_seats_heals_drifted_row_back_to_yaml(configs_dir: Path) -> None:
    """A hand-corrupted seat row is overwritten by the yaml values on re-seed
    (DO UPDATE over the managed columns) — the todo-9 corruption probe
    contract, now a permanent unit test."""
    conn = _SeedConn(_SeedStore())
    seed_seats(conn)
    conn.store.rows["oracle"] = (
        "oracle", "Corrupted", "general", 0.5, "mid", "mid", "[]", 12345,
    )
    seed_seats(conn)
    assert conn.store.rows["oracle"] == EXPECTED_ORACLE


def test_seed_seats_never_deletes_db_rows_absent_from_yaml(
    configs_dir: Path,
) -> None:
    """Seed is an upsert, not a sync: rows not in the yaml (e.g. the sweep
    pseudo-seats) must survive a run."""
    conn = _SeedConn(_SeedStore())
    conn.store.rows["_stage0_sweep"] = (
        "_stage0_sweep", "Stage 0 sweep", "general", 0.5, "mid", "mid", "[]", None,
    )
    seed_seats(conn)
    assert "_stage0_sweep" in conn.store.rows
    assert len(conn.store.rows) == 3


# ---------------------------------------------------------------------------
# upsert_seat: yaml-first shared path + generic fallback
# ---------------------------------------------------------------------------


def test_upsert_seat_yaml_code_writes_full_yaml_values(configs_dir: Path) -> None:
    conn = _SeedConn(_SeedStore())
    upsert_seat(conn, "oracle", "ignored")
    assert conn.store.rows["oracle"] == EXPECTED_ORACLE
    assert "DO UPDATE SET" in conn.store.statements[-1]


def test_upsert_seat_pseudo_code_uses_generic_fallback(configs_dir: Path) -> None:
    conn = _SeedConn(_SeedStore())
    upsert_seat(conn, "_stage0_sweep", "Stage 0 sweep")
    row = conn.store.rows["_stage0_sweep"]
    assert row[0] == "_stage0_sweep"
    assert row[1] == "Stage 0 sweep"
    # legacy generic shape: ("general", 0.5, "mid", "mid", "[]", None)
    assert row[2:8] == ("general", 0.5, "mid", "mid", "[]", None)
    assert "DO NOTHING" in conn.store.statements[-1]


def test_upsert_seat_unknown_code_without_name_raises(configs_dir: Path) -> None:
    conn = _SeedConn(_SeedStore())
    with pytest.raises(ValueError, match="no fallback seat_name"):
        upsert_seat(conn, "ghost_seat", "")


# ---------------------------------------------------------------------------
# SQL shape: the idempotency-sensitive clauses exist
# ---------------------------------------------------------------------------


def test_build_upsert_sql_update_covers_all_managed_columns() -> None:
    sql = _build_upsert_sql(update=True)
    for col in _MANAGED_COLUMNS:
        assert col in sql, f"managed column {col} missing from the upsert"
    assert "INSERT INTO hr.seat (seat_code, " in sql
    assert "ON CONFLICT (seat_code) DO UPDATE SET" in sql
    assert "EXCLUDED." in sql  # healing requires EXCLUDED.<col> assignments
    assert "DO NOTHING" not in sql


def test_build_upsert_sql_fallback_is_do_nothing() -> None:
    sql = _build_upsert_sql(update=False)
    assert "ON CONFLICT (seat_code) DO NOTHING" in sql
    assert "EXCLUDED" not in sql


# ---------------------------------------------------------------------------
# main(): the no-DB proof — unresolvable DSN fails cleanly without connecting
# ---------------------------------------------------------------------------


def test_main_without_any_dsn_returns_1_and_prints_error(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """With HR_DSN/HR_DB_PASSWORD/HR_COMPOSE_FILE all unset and no hr.toml,
    main() must exit 1 with 'error: ...' on stderr — proving not even a
    connection attempt happens when no DB is reachable."""
    _clear_dsn_envs(monkeypatch)
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    assert main() == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error:")
    assert "HR_DSN" in captured.err
    assert captured.out == ""
