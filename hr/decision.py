"""Canonical measurement queries and health-aware seat decisions."""

from __future__ import annotations

import logging
from typing import TypedDict

import numpy as np

from hr.assign.ranker import CandidateModel, RankerResult, rank
from hr.config import load_yaml
from hr.health import HealthReport
from hr.seats.health_gates import SEAT_HEALTH_GATE
from hr.seats.rolespec import DEFAULT_BATTERY_BY_SEAT, SEAT_CODES


logger = logging.getLogger(__name__)

_KNOB_TO_BATTERY: dict[str, str] = {
    "reasoning": "reasoning",
    "top_tool_fraction": "tool_a",
    "coverage": "hallucination",
    "longctx": "livebench_long_context",
    "speed_cost": "livebench_speed",
}
_WARNED_MISSING_BATTERY: set[tuple[str, str]] = set()


def _apply_knob_battery_overrides() -> None:
    try:
        overrides = load_yaml("thresholds.yaml").get("knob_battery", {})
    except FileNotFoundError:
        return
    for knob, battery_code in overrides.items():
        if knob in _KNOB_TO_BATTERY and isinstance(battery_code, str):
            _KNOB_TO_BATTERY[knob] = battery_code


_apply_knob_battery_overrides()


def _fetch(conn, sql: str, params: tuple | None = None) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return list(cur.fetchall())


def latest_sweep_id(conn) -> str:
    """Newest sweep by wall clock; contaminated-sweep selection fix (audit bug 6)."""
    rows = _fetch(
        conn,
        "SELECT sweep_id FROM hr.sweep ORDER BY created_at DESC LIMIT 1",
    )
    if not rows:
        raise ValueError("no sweeps found in hr.sweep")
    return str(rows[0][0])


def measurement_count(conn, sweep_id: str) -> int:
    rows = _fetch(
        conn,
        """
        SELECT COUNT(m.measurement_id)::int
          FROM hr.measurement m
          JOIN hr.run r ON r.run_id = m.run_id
         WHERE r.sweep_id = %s
        """,
        (sweep_id,),
    )
    return int(rows[0][0]) if rows else 0


def capability_means(conn, sweep_id: str) -> dict[str, dict[str, float]]:
    rows = _fetch(
        conn,
        """
        SELECT r.model_id, b.battery_code, AVG(m.score)::float8
          FROM hr.measurement m
          JOIN hr.run r ON r.run_id = m.run_id
          JOIN hr.battery b ON b.battery_id = r.battery_id
         WHERE r.sweep_id = %s
         GROUP BY r.model_id, b.battery_code
        """,
        (sweep_id,),
    )
    means: dict[str, dict[str, float]] = {}
    for model_id, battery_code, mean in rows:
        means.setdefault(str(model_id), {})[str(battery_code)] = float(mean)
    return means


def battery_codes(conn) -> list[str]:
    return [str(row[0]) for row in _fetch(
        conn, "SELECT battery_code FROM hr.battery ORDER BY battery_code"
    )]


def seat_rows(conn) -> dict[str, dict]:
    rows = _fetch(
        conn,
        "SELECT seat_code, required_capabilities, ctx_p95_tokens FROM hr.seat",
    )
    return {
        str(seat_code): {
            "seat_code": str(seat_code),
            "required_capabilities": list(capabilities or []),
            "ctx_p95": int(context) if context is not None else None,
        }
        for seat_code, capabilities, context in rows
    }


def model_capabilities(conn) -> dict[str, dict]:
    rows = _fetch(conn, "SELECT model_id, capabilities FROM hr.model")
    return {str(model_id): dict(capabilities or {}) for model_id, capabilities in rows}


def separation_probabilities(
    conn,
    sweep_id: str,
) -> dict[str, dict[tuple[str, str], float]]:
    rows = _fetch(
        conn,
        """
        SELECT b.battery_code, s.model_a, s.model_b,
               s.p_separated, s.p_weak, s.p_tie
          FROM hr.separation s
          JOIN hr.battery b ON b.battery_id = s.battery_id
         WHERE s.sweep_id = %s AND s.directional = TRUE
        """,
        (sweep_id,),
    )
    by_battery: dict[str, dict[tuple[str, str], float]] = {}
    for battery, model_a, model_b, separated, weak, _tie in rows:
        probability = float(separated or weak or 0.5)
        by_battery.setdefault(str(battery), {})[(str(model_a), str(model_b))] = probability
    return by_battery


def _fit_weights(seat_code: str, available_batteries: set[str]) -> dict[str, float]:
    knobs = DEFAULT_BATTERY_BY_SEAT[seat_code]
    weights: dict[str, float] = {}
    for knob, battery_code in _KNOB_TO_BATTERY.items():
        if battery_code not in available_batteries:
            if (knob, battery_code) not in _WARNED_MISSING_BATTERY:
                _WARNED_MISSING_BATTERY.add((knob, battery_code))
                logger.warning(
                    "verdict knob %r maps to battery %r but that battery has no data; "
                    "knob contributes 0 to fitness",
                    knob,
                    battery_code,
                )
            continue
        weights[battery_code] = weights.get(battery_code, 0.0) + knobs.get(knob, 0.0)
    total = sum(weights.values())
    return {battery: weight / total for battery, weight in weights.items()} if total else {}


def _weighted_separations(
    weights: dict[str, float],
    by_battery: dict[str, dict[tuple[str, str], float]],
) -> dict[tuple[str, str], float]:
    totals: dict[tuple[str, str], float] = {}
    coverage: dict[tuple[str, str], float] = {}
    for battery, weight in weights.items():
        for pair, probability in by_battery.get(battery, {}).items():
            totals[pair] = totals.get(pair, 0.0) + probability * weight
            coverage[pair] = coverage.get(pair, 0.0) + weight
    return {pair: total / coverage[pair] for pair, total in totals.items()}


class SeatAssignment(TypedDict):
    seat_code: str
    gate_level: str
    primary: str | None
    fallbacks: list[tuple[str, str]]
    eliminated: list[tuple[str, str]]
    unassigned: str | None


def seat_assignments(
    pool: set[str],
    means: dict[str, dict[str, float]],
    reports: dict[str, HealthReport],
    seat_db: dict[str, dict],
    caps_db: dict[str, dict],
    codes: list[str],
    retired_set: set[str],
    include_retired: bool,
    separations: dict[str, dict[tuple[str, str], float]] | None = None,
) -> list[SeatAssignment]:
    del retired_set, include_retired
    available = set(codes)
    assignments: list[SeatAssignment] = []
    for seat_code in SEAT_CODES:
        weights = _fit_weights(seat_code, available)
        candidates = [
            CandidateModel(
                model_id=model_id,
                provider_id="",
                capabilities=caps_db.get(model_id, {}),
                ctx_p95_tokens=0,
                scores={
                    battery: np.array([mean])
                    for battery, mean in battery_means.items()
                    if battery in available
                },
                cost_per_task=0.0,
                health=reports.get(model_id),
            )
            for model_id, battery_means in sorted(means.items())
            if model_id in pool and battery_means
        ]
        candidates = [candidate for candidate in candidates if candidate.scores]
        gate_level = SEAT_HEALTH_GATE[seat_code]
        primary: str | None = None
        fallbacks: list[tuple[str, str]] = []
        eliminated: list[tuple[str, str]] = []
        unassigned: str | None = None
        if not candidates:
            unassigned = "no candidates with battery data"
        else:
            try:
                result: RankerResult = rank(
                    candidates,
                    seat_db.get(seat_code, {
                        "seat_code": seat_code,
                        "required_capabilities": [],
                        "ctx_p95": None,
                    }),
                    weights,
                    separation_pairs=_weighted_separations(weights, separations or {}),
                    gate_level=gate_level,
                )
            except ValueError as exc:
                unassigned = f"none pass gates ({exc})"
            else:
                primary = result.primary
                fallbacks = list(result.fallbacks[:2])
                eliminated = list(result.eliminated)
        assignments.append({
            "seat_code": seat_code,
            "gate_level": gate_level,
            "primary": primary,
            "fallbacks": fallbacks,
            "eliminated": eliminated,
            "unassigned": unassigned,
        })
    return assignments
