"""Stage-1 finalist-selection contract tests (committed surface).

Covers hr.stage1_selection: constants, finalist selection from Stage-0
DB rows (top-k per deciding battery with union and rationale), the
empty-DB fallback and fail-loud paths, and full-bank item loading.
Offline and deterministic.
"""

from __future__ import annotations

import pytest

import hr.db
from hr.stage1_selection import (
    DEFAULT_THRESHOLDS_PATH,
    FinalistSelection,
    STAGE1_DECIDING_BATTERIES,
    STAGE1_FINALISTS_PER_BATTERY,
    STAGE1_FULL_BANK_SIZES,
    STAGE1_N_INITIAL,
    STAGE1_N_MAX,
    STAGE1_SEAT_CODE,
    STAGE1_TOKEN_CAP,
    load_full_banks,
    select_finalists_from_stage0,
)


class _FakeCursor:
    def __init__(self, rows: list[tuple]):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def execute(self, sql, params) -> None:  # noqa: ARG002
        pass

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[tuple]):
        self._rows = rows
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._rows)

    def close(self) -> None:
        self.closed = True


def test_stage1_constants() -> None:
    assert STAGE1_DECIDING_BATTERIES == ("reasoning", "tool_a", "hallucination", "vision")
    assert STAGE1_FINALISTS_PER_BATTERY == 6
    assert STAGE1_N_INITIAL == 3
    assert STAGE1_N_MAX == 10
    assert STAGE1_TOKEN_CAP == 90_000_000
    assert STAGE1_SEAT_CODE == "_stage1_finals"
    assert STAGE1_FULL_BANK_SIZES == {
        "reasoning": 60,
        "hallucination": 70,
        "tool_a": 100,
        "vision": 22,
    }
    assert DEFAULT_THRESHOLDS_PATH.name == "thresholds.yaml"


def test_select_finalists_ranks_and_unions(monkeypatch) -> None:
    rows = [
        ("m1", "reasoning", 0.9),
        ("m2", "reasoning", 0.8),
        ("m1", "tool_a", 0.7),
        ("m3", "tool_a", 0.6),
        ("m4", "skipped-battery", 1.0),
    ]
    conn = _FakeConn(rows)
    monkeypatch.setattr(hr.db, "connect", lambda: conn)
    selection = select_finalists_from_stage0(top_k=2)
    assert conn.closed
    assert isinstance(selection, FinalistSelection)
    assert selection.per_battery == {
        "reasoning": [("m1", 0.9), ("m2", 0.8)],
        "tool_a": [("m1", 0.7), ("m3", 0.6)],
        "hallucination": [],
        "vision": [],
    }
    assert selection.finalists == ["m1", "m2", "m3"]
    assert "reasoning: ['m1', 'm2']" in selection.rationale
    assert "Union of finalists (3)" in selection.rationale


def test_select_finalists_top_k_caps(monkeypatch) -> None:
    rows = [(f"m{i}", "reasoning", float(10 - i)) for i in range(10)]
    monkeypatch.setattr(hr.db, "connect", lambda: _FakeConn(rows))
    selection = select_finalists_from_stage0(top_k=3)
    assert [m for m, _ in selection.per_battery["reasoning"]] == ["m0", "m1", "m2"]


def test_select_finalists_empty_db_fails_loud(monkeypatch) -> None:
    monkeypatch.setattr(hr.db, "connect", lambda: _FakeConn([]))
    with pytest.raises(RuntimeError, match="No Stage 0 measurements found"):
        select_finalists_from_stage0()


def test_select_finalists_empty_db_fallback(monkeypatch) -> None:
    monkeypatch.setattr(hr.db, "connect", lambda: _FakeConn([]))
    monkeypatch.setattr("hr.stage1_selection.fleet_models", lambda: ("b", "a"))
    selection = select_finalists_from_stage0(allow_db_missing=True)
    assert selection.per_battery == {}
    assert selection.finalists == ["a", "b"]
    assert "test-only path" in selection.rationale


def test_load_full_banks_filters_batteries(monkeypatch, tmp_path) -> None:
    seen: dict = {}

    def fake_load(repo, batteries):
        seen["repo"] = repo
        seen["batteries"] = batteries
        return {"reasoning": ["env"], "tool_a": ["env"], "extra": ["env"]}

    monkeypatch.setattr("hr.calibrate.load_item_repo", fake_load)
    banks = load_full_banks(tmp_path, batteries=("reasoning", "tool_a"))
    assert seen["repo"] == tmp_path
    assert banks == {"reasoning": ["env"], "tool_a": ["env"]}


def test_load_full_banks_default_repo(monkeypatch, tmp_path) -> None:
    def fake_load(repo, batteries):
        return {"reasoning": ["env"]}

    monkeypatch.setattr("hr.calibrate.load_item_repo", fake_load)
    monkeypatch.setattr("hr.stage1_selection.itemrepo_path", lambda: tmp_path / "itemrepo")
    banks = load_full_banks()
    assert banks["reasoning"] == ["env"]
    assert set(banks.keys()) == set(STAGE1_DECIDING_BATTERIES)