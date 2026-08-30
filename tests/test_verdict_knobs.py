"""Verdict knob→battery mapping tests (hr-unification plan, todo 11).

Fixture-based; no live DB. The acceptance test rotates the longctx knob
(seats.rolespec weight) 0→100 across a 2-candidate fixture and proves the
verdict primary flips — i.e. livebench_long_context scores influence the
fitness ordering again after the mapping restore.

Seat weights are monkeypatched on ``hr.decision.DEFAULT_BATTERY_BY_SEAT`` (the
rolespec ``_DEFAULT_RAW`` numbers themselves are untouched by the fix).
"""

from __future__ import annotations

import logging

import pytest

from hr import decision as cli_mod

# Oracle's own raw knob weights (rolespec._DEFAULT_RAW, unchanged by the fix)
# — top_tool_fraction 30 / longctx 90 / reasoning 85 / speed_cost 40 /
# coverage 50. Tests below override ONLY longctx to rotate it 0→100.
_ORACLE_RAW = {
    "top_tool_fraction": 30.0,
    "longctx": 90.0,
    "reasoning": 85.0,
    "speed_cost": 40.0,
    "coverage": 50.0,
}

# All five batteries present in the fixture sweep: the two livebench
# batteries the restored knobs map onto + the three legacy ones.
CODES = [
    "reasoning",
    "tool_a",
    "hallucination",
    "livebench_long_context",
    "livebench_speed",
]

# m_a: strong reasoning, weak long-context. m_b: the mirror image. Equal on
# the remaining three batteries so those weights cancel on both sides.
MEANS = {
    "m_a": {
        "reasoning": 1.0,
        "tool_a": 0.5,
        "hallucination": 0.5,
        "livebench_long_context": 0.0,
        "livebench_speed": 0.5,
    },
    "m_b": {
        "reasoning": 0.0,
        "tool_a": 0.5,
        "hallucination": 0.5,
        "livebench_long_context": 1.0,
        "livebench_speed": 0.5,
    },
}


def _seats_with_oracle(longctx: float) -> dict[str, dict[str, float]]:
    """DEFAULT_BATTERY_BY_SEAT copy with oracle's longctx knob rotated."""
    seats = dict(cli_mod.DEFAULT_BATTERY_BY_SEAT)
    oracle = dict(_ORACLE_RAW)
    oracle["longctx"] = float(longctx)
    seats["oracle"] = oracle
    return seats


def _oracle_primary(
    seats: dict[str, dict[str, float]],
    means: dict[str, dict[str, float]] = MEANS,
    codes: list[str] = CODES,
) -> str:
    """Primary recommendation for the oracle seat from a fixture sweep."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(cli_mod, "DEFAULT_BATTERY_BY_SEAT", seats)
    try:
        rows = cli_mod.seat_assignments(
            pool=set(means),
            means=means,
            reports={},
            seat_db={},
            caps_db={},
            codes=codes,
            retired_set=set(),
            include_retired=False,
        )
    finally:
        monkeypatch.undo()
    oracle_row = next(r for r in rows if r["seat_code"] == "oracle")
    assert oracle_row["primary"] is not None
    return str(oracle_row["primary"])


def test_longctx_knob_rotation_changes_verdict_ordering():
    """Acceptance: rotating the longctx knob 0→100 flips the verdict primary.

    longctx=0 → oracle fitness is reasoning-dominated → strong-reasoning m_a
    wins. longctx=100 → the livebench_long_context score dominates → m_b
    wins. This proves the longctx knob influences fitness again through its
    livebench_long_context battery.
    """
    # longctx knob 0: long-context weight gone → m_a (reasoning 1.0) wins.
    assert _oracle_primary(_seats_with_oracle(0)) == "m_a"
    # longctx knob 100: long-context weight dominates → m_b (longctx 1.0) wins.
    assert _oracle_primary(_seats_with_oracle(100)) == "m_b"


def test_missing_livebench_data_warns_and_contributes_zero(monkeypatch, caplog):
    """Failure probe: sweep WITHOUT livebench data → knob 0 + warning, no crash."""
    available = {"reasoning", "tool_a", "hallucination"}
    monkeypatch.setattr(cli_mod, "DEFAULT_BATTERY_BY_SEAT", _seats_with_oracle(90))
    cli_mod._WARNED_MISSING_BATTERY.clear()
    with caplog.at_level(logging.WARNING, logger="hr.decision"):
        weights = cli_mod._fit_weights("oracle", available)
    assert "livebench_long_context" not in weights  # knob contributes 0
    assert "livebench_speed" not in weights
    assert any(
        "longctx" in r.message and "contributes 0" in r.message
        for r in caplog.records
    ), f"no longctx warning logged: {[r.message for r in caplog.records]}"

    # The verdict still completes end-to-end with the battery absent.
    primary = _oracle_primary(
        _seats_with_oracle(90),
        means={"m_a": {"reasoning": 1.0}, "m_b": {"reasoning": 0.2}},
        codes=sorted(available),
    )
    assert primary == "m_a"  # reasoning-only ordering, no crash


def test_speed_cost_maps_to_livebench_speed():
    """Both restored knobs map onto livebench batteries (non-None)."""
    assert cli_mod._KNOB_TO_BATTERY["longctx"] == "livebench_long_context"
    assert cli_mod._KNOB_TO_BATTERY["speed_cost"] == "livebench_speed"
    # With livebench data present both restored batteries enter the fit.
    weights = cli_mod._fit_weights("oracle", set(CODES))
    assert "livebench_long_context" in weights
    assert "livebench_speed" in weights


def test_knob_battery_override_reads_config(monkeypatch):
    """The mapping is data-driven: configs/thresholds.yaml `knob_battery:`
    section repoints knobs; unknown/non-str config entries are ignored."""
    saved = dict(cli_mod._KNOB_TO_BATTERY)
    try:
        monkeypatch.setattr(
            cli_mod,
            "load_yaml",
            lambda name: {
                "knob_battery": {
                    "longctx": "custom_ctx",
                    "speed_cost": "custom_speed",
                    "bogus": "livebench_x",
                    "reasoning": 42,  # non-str value → ignored
                }
            },
        )
        cli_mod._apply_knob_battery_overrides()
        assert cli_mod._KNOB_TO_BATTERY["longctx"] == "custom_ctx"
        assert cli_mod._KNOB_TO_BATTERY["speed_cost"] == "custom_speed"
        assert "bogus" not in cli_mod._KNOB_TO_BATTERY  # unknown knob ignored
        assert cli_mod._KNOB_TO_BATTERY["reasoning"] == "reasoning"  # int ignored
    finally:
        cli_mod._KNOB_TO_BATTERY.clear()
        cli_mod._KNOB_TO_BATTERY.update(saved)


def test_missing_thresholds_file_keeps_code_defaults(monkeypatch):
    """No threshold config at all → the restored mapping still stands (the
    code default), verdict keeps working — no crash, no config required."""
    saved = dict(cli_mod._KNOB_TO_BATTERY)
    try:
        def _raise(name):
            raise FileNotFoundError(f"config file not found: {name}")

        monkeypatch.setattr(cli_mod, "load_yaml", _raise)
        cli_mod._apply_knob_battery_overrides()  # must not raise
        assert cli_mod._KNOB_TO_BATTERY["longctx"] == "livebench_long_context"
        assert cli_mod._KNOB_TO_BATTERY["speed_cost"] == "livebench_speed"
    finally:
        cli_mod._KNOB_TO_BATTERY.clear()
        cli_mod._KNOB_TO_BATTERY.update(saved)
