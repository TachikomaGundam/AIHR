
from __future__ import annotations

import re
from dataclasses import dataclass, field


from hr.stats.sequential import SequentialStopper, SequentialConfig, bonferroni_pair_alpha
from hr.stats.empirical_bernstein import EmpiricalBernsteinSequence

# Reuse stage0's helpers and DB plumbing.

#: Bump when the sweep-state semantics change so that in-progress sweeps
#: recorded under an older decision regime are explicitly invalidated and
#: restarted — never silently reinterpreted. Version 1 = repeated-peek
#: bootstrap-CI stopper; version 2 = anytime-valid empirical-Bernstein pairs.
STAGE1_STATE_VERSION: int = 2

_STATE_VERSION_RE = re.compile(r"^state_version:\s*(\d+)\s*$", re.MULTILINE)


def _purpose_with_state_version(purpose: str) -> str:
    """Stamp a sweep purpose string with the current state version marker,
    replacing any stale marker from an older decision regime."""
    purpose = _STATE_VERSION_RE.sub("", purpose or "").rstrip("\n")
    return f"{purpose}\nstate_version: {STAGE1_STATE_VERSION}"


def parse_state_version(purpose: str | None) -> int | None:
    """Extract the state-version marker from a sweep purpose; None for legacy
    sweeps recorded before markers existed."""
    match = _STATE_VERSION_RE.search(purpose or "")
    return int(match.group(1)) if match else None


def _sweep_purpose(conn, sweep_id: str) -> str | None:
    """Purpose text of a stored sweep; None when the sweep row is absent."""
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT purpose FROM hr.sweep WHERE sweep_id = %s", (sweep_id,))
        row = cur.fetchone()
    return row[0] if row else None


def _sweep_state_version(conn, sweep_id: str) -> int | None:
    """State version recorded on a stored sweep (None = legacy or absent)."""
    return parse_state_version(_sweep_purpose(conn, sweep_id))


def _resume_requires_restart(stored_version: int | None) -> bool:
    """True when recorded rounds predate (or disagree with) the current state
    version: resuming would silently reinterpret legacy stopping decisions."""
    return stored_version != STAGE1_STATE_VERSION


def _ensure_sweep_state_stamped(conn, sweep_id: str) -> None:
    """Write the current state-version marker into a sweep's purpose row
    (in place, idempotent). Awaiting sweeps get stamped so a later resume can
    distinguish current-regime rounds from legacy ones."""
    purpose = _sweep_purpose(conn, sweep_id)
    if purpose is None or parse_state_version(purpose) == STAGE1_STATE_VERSION:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE hr.sweep SET purpose = %s WHERE sweep_id = %s",
            (_purpose_with_state_version(purpose), sweep_id),
        )
    conn.commit()


def make_pair_sequence(battery_code: str, config: SequentialConfig, n_finalists: int) -> EmpiricalBernsteinSequence:
    """Factory: one anytime-valid pair sequence for a battery, carrying the
    Bonferroni-adjusted per-pair alpha, the battery's practical-effect
    region, and the round budget. Used identically by the finals loop and
    the resume path so fresh and resumed sweeps share the same contract."""
    n_pairs = max(0, n_finalists * (n_finalists - 1) // 2)
    alpha = bonferroni_pair_alpha(config.family_alpha, n_pairs) if n_pairs else config.family_alpha
    return EmpiricalBernsteinSequence(
        battery_code=battery_code,
        alpha=alpha,
        min_effect=config.min_effect_for(battery_code),
        max_rounds=config.max_rounds,
    )


@dataclass
class Stage1SweepState:
    """Sweep state for Stage 1 finals.

    Layout is identical to stage0.SweepState so the DB plumbing reuses
    the same insertion helpers.
    """

    sweep_id: str
    finalists: list[str]
    total_tokens: int = 0
    total_calls: int = 0
    stopped_at_cap: bool = False
    stopped_reason: str = ""
    measurements_by_model_battery: dict[str, dict[str, list[float]]] = field(default_factory=dict)
    # key: f"{model_id}|{battery_code}" -> { item_key: [scores across repetitions] }
    stoppers: dict[str, SequentialStopper] = field(default_factory=dict)
    # key: battery_code -> aggregate diagnostic stopper (not a decision rule)
    model_stoppers: dict[str, SequentialStopper] = field(default_factory=dict)
    # key: f"{model_id}|{battery_code}" -> independent precision stopper
    pair_stoppers: dict[str, EmpiricalBernsteinSequence] = field(default_factory=dict)
    # key: f"{model_a}|{model_b}|{battery_code}" -> anytime-valid paired-diff sequence
    n_rounds_done: dict[str, int] = field(default_factory=dict)
    # key: battery_code -> number of rounds completed


RecordedMeasurements = dict[tuple[str, str, int, str, int], float]


def _recorded_measurement_keys(conn, sweep_id: str) -> RecordedMeasurements:
    """Return persisted scores keyed by model, battery, round, item, and repetition."""
    if conn is None:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.model_id, b.battery_code, r.round,
                   m.item_id, m.repetition, m.score
              FROM hr.measurement m
              JOIN hr.run r ON m.run_id = r.run_id
              JOIN hr.battery b ON r.battery_id = b.battery_id
            WHERE r.sweep_id = %s
            """,
            (sweep_id,),
        )
        return {
            (model_id, battery, int(round_num), item_id, int(repetition)): float(score)
            for model_id, battery, round_num, item_id, repetition, score in cur.fetchall()
        }


def _max_round_per_model_battery(conn, sweep_id: str) -> dict[tuple[str, str], int]:
    """For resume: max round already recorded per (model, battery)."""
    if conn is None:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.model_id, b.battery_code, MAX(r.round)
              FROM hr.run r
              JOIN hr.battery b ON r.battery_id = b.battery_id
            WHERE r.sweep_id = %s
            GROUP BY r.model_id, b.battery_code
            """,
            (sweep_id,),
        )
        return {(m, b): int(r) for m, b, r in cur.fetchall()}


# ---------------------------------------------------------------------------
# Core sweep loop (sequential-n)
