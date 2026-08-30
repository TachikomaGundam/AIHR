"""Stage 1 — finalists run the FULL banks with sequential-n.

Takes the top 5–6 models per deciding battery from Stage 0, runs them on the
full item banks (reasoning 60, hallucination 70, tool_a 100, vision 22) with
sequential-n (pilot n=3, extend until CI half-width ≤ threshold or n_max=10),
records to the hr2 DB, and produces a per-battery separation matrix.

CLI:
    python3 -m hr2.stage1 --dry-run       print plan + finalist selection (no API calls)
    python3 -m hr2.stage1 --run           run the finals sweep with live adapter
    python3 -m hr2.stage1 --report        print separation matrix from DB
    --sweep-id=...                        resume an existing sweep (skip recorded pairs)
    --models=...                          override finalist list (comma-sep model ids)
    --token-cap=...                       override Stage 1 token cap (default 90M)
    --thresholds=...                      path to thresholds.yaml

The full sweep is fired separately by the orchestrator; this module is the
RUNNER. For local smoke-testing, use --dry-run (no API calls) or
--run with a FakeAdapter injected.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from hr.config import itemrepo_path
from hr.graders import build_default_registry
from hr.fleet import fleet_models
from hr.items.schema import ItemEnvelope
from hr.stats.sequential import SequentialConfig, SequentialStopper
from hr.stats.bootstrap import classify, paired_bootstrap_separation

# Reuse stage0's helpers and DB plumbing.
from hr.stage0 import (
    BATTERY_ITEM_TYPES,
    STAGE0_BATTERIES,
    STAGE0_SEAT_CODE,
    SingleCallResult,
    SweepState,
    _AdapterFacade,
    _connect,
    _ensure_provider_model_records,
    _init_db,
    _insert_infra_incident,
    _insert_measurement,
    _insert_run,
    _insert_separation,
    _insert_sweep,
    _key,
    _print_matrix,
    _upsert_battery,
    _upsert_battery_item,
    _upsert_item_pool,
    _upsert_seat,
    call_and_grade,
)


# ---------------------------------------------------------------------------
# Stage 1 constants
# ---------------------------------------------------------------------------
#: The four deciding batteries for Stage 1 finals (spec §5.4 v0.2/§10.7).
STAGE1_DECIDING_BATTERIES: tuple[str, ...] = ("reasoning", "tool_a", "hallucination", "vision")

#: Stage 1 runs full banks, NOT Stage 0's reduced subsets.
STAGE1_FULL_BANK_SIZES: dict[str, int] = {
    "reasoning": 60,
    "hallucination": 70,
    "tool_a": 100,
    "vision": 22,
}

#: Take top-k finalists per deciding battery (spec §5.4: top 5–6).
STAGE1_FINALISTS_PER_BATTERY: int = 6

#: Sequential-n parameters.
STAGE1_N_INITIAL: int = 3  # pilot rounds
STAGE1_N_MAX: int = 10  # budget cap per battery

#: Stage 1 token cap (spec §9.1 v0.3).
STAGE1_TOKEN_CAP: int = 90_000_000
EST_TOKENS_PER_CALL: int = 5_000

#: Seat code for the finals sweep (separate from Stage 0's _stage0_sweep).
STAGE1_SEAT_CODE: str = "_stage1_finals"

DEFAULT_THRESHOLDS_PATH: Path = Path(__file__).resolve().parents[1] / "configs" / "thresholds.yaml"


# ---------------------------------------------------------------------------
# Finalist selection from Stage 0 DB
# ---------------------------------------------------------------------------
@dataclass
class FinalistSelection:
    """Rationale for which models were selected per battery + union of finalists."""

    per_battery: dict[str, list[tuple[str, float]]]
    # battery_code -> [(model_id, mean_score), ...] top-k, sorted desc
    finalists: list[str]
    # union of all per-battery top-k, sorted for determinism
    rationale: str
    # human-readable rationale text


def select_finalists_from_stage0(
    *,
    deciding_batteries: tuple[str, ...] = STAGE1_DECIDING_BATTERIES,
    top_k: int = STAGE1_FINALISTS_PER_BATTERY,
    allow_db_missing: bool = False,
) -> FinalistSelection:
    """Query Stage 0 DB, rank models per battery by mean score, take top-k.

    Returns FinalistSelection with per-battery top-k + union of finalists.
    Falls back to all models if the DB is empty and allow_db_missing=True.
    Raises RuntimeError if DB is empty and allow_db_missing=False.
    """
    from hr.db import connect

    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.model_id, b.battery_code, AVG(m.score) AS mean_score
                FROM hr.measurement m
                JOIN hr.run r ON m.run_id = r.run_id
                JOIN hr.sweep s ON r.sweep_id = s.sweep_id
                JOIN hr.battery b ON r.battery_id = b.battery_id
                WHERE s.seat_code = %s
                GROUP BY r.model_id, b.battery_code
                ORDER BY b.battery_code ASC, mean_score DESC, r.model_id ASC
                """,
                (STAGE0_SEAT_CODE,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        if allow_db_missing:
            # Degenerate fallback — useful for unit tests without a DB.
            return FinalistSelection(
                per_battery={},
                finalists=sorted(fleet_models()),
                rationale=(
                    "Stage 0 DB is empty; falling back to full pool. "
                    "This is a test-only path — real finals selection requires Stage 0 results."
                ),
            )
        raise RuntimeError(
            f"No Stage 0 measurements found in DB (seat_code={STAGE0_SEAT_CODE}). "
            "Run Stage 0 first before selecting finalists."
        )

    # Group by battery_code preserving rank order.
    per_battery_scores: dict[str, list[tuple[str, float]]] = {}
    for model_id, battery_code, mean_score in rows:
        per_battery_scores.setdefault(battery_code, []).append((model_id, float(mean_score)))

    # Take top-k per deciding battery.
    selection: dict[str, list[tuple[str, float]]] = {}
    finalists_set: set[str] = set()
    for battery in deciding_batteries:
        ranked = per_battery_scores.get(battery, [])
        top = ranked[:top_k]
        selection[battery] = top
        finalists_set.update(m for m, _ in top)

    finalists_list = sorted(finalists_set)
    rationale_parts = [f"Stage 1 finalist selection (top-{top_k} per deciding battery):"]
    for battery in deciding_batteries:
        top = selection[battery]
        rationale_parts.append(f"  {battery}: {[m for m, _ in top]}")
    rationale_parts.append(f"Union of finalists ({len(finalists_list)}): {finalists_list}")
    rationale = "\n".join(rationale_parts)

    return FinalistSelection(
        per_battery=selection,
        finalists=finalists_list,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Full-bank item loading
# ---------------------------------------------------------------------------
def load_full_banks(
    item_repo: Path | None = None,
    *,
    batteries: tuple[str, ...] = STAGE1_DECIDING_BATTERIES,
) -> dict[str, list[ItemEnvelope]]:
    """Load full item banks for the deciding batteries (no subsetting).

    Uses stage0's BATTERY_ITEM_TYPES to map batteries to ItemTypes, then
    walks the repo without invoking select_subsets. ``item_repo`` defaults
    to :func:`hr.config.itemrepo_path`.
    """
    from hr.calibrate import load_item_repo

    if item_repo is None:
        item_repo = itemrepo_path()
    bundles = load_item_repo(item_repo, batteries=list(batteries))
    # Filter to only the deciding batteries (load_item_repo returns all it was asked).
    return {b: bundles.get(b, []) for b in batteries}


# ---------------------------------------------------------------------------
# Sweep state (mirrors stage0.SweepState for DB compatibility)
# ---------------------------------------------------------------------------
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
    # key: battery_code -> SequentialStopper for that battery (aggregating all finalists)
    n_rounds_done: dict[str, int] = field(default_factory=dict)
    # key: battery_code -> number of rounds completed


def _recorded_measurement_keys(conn, sweep_id: str) -> set[tuple[str, str, str, int]]:
    """Return set of (model_id, battery_code, item_id, repetition) already recorded."""
    if conn is None:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.model_id, b.battery_code, m.item_id, m.repetition
            FROM hr.measurement m
            JOIN hr.run r ON m.run_id = r.run_id
            JOIN hr.battery b ON r.battery_id = b.battery_id
            WHERE r.sweep_id = %s
            """,
            (sweep_id,),
        )
        return set(cur.fetchall())


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
# ---------------------------------------------------------------------------
def _run_finals_loop(
    *,
    adapter: _AdapterFacade,
    item_repo: Path,
    finalists: list[str],
    full_banks: dict[str, list[ItemEnvelope]],
    batteries: tuple[str, ...],
    battery_ids: dict[str, str],
    seq_config: SequentialConfig,
    token_cap: int,
    state: Stage1SweepState,
    registry,
    conn,
    sweep_id: str,
    record_to_db: bool,
    already_recorded: set[tuple[str, str, str, int]],
    prior_rounds: dict[tuple[str, str], int],
) -> None:
    """Sequential-n loop with per-battery independent progression.

    Each battery tracks its own round counter. At each iteration of the outer
    loop we round-robin over batteries: for each active (non-stopped, below
    n_max) battery, we run ONE round for ALL finalists, then update the
    battery's SequentialStopper and check `should_stop`.

    Resume support: if `already_recorded` is non-empty (sweep_id already has
    measurements in DB), we seed `n_rounds_done[battery]` from the max recorded
    round per (model, battery) in the DB and only run subsequent rounds.
    """
    # Initialize stoppers per battery.
    for battery in batteries:
        state.stoppers[battery] = SequentialStopper(
            battery_code=battery, config=seq_config
        )
        state.n_rounds_done[battery] = 0

    # Resume: seed state from DB.
    if already_recorded and conn is not None:
        _rebuild_stopper_from_db(state, conn, sweep_id, batteries, seq_config)

    # Determine each battery's starting round (resume-aware).
    next_round: dict[str, int] = {}
    for battery in batteries:
        # Resume: start at the max recorded round (across all finalists) + 1.
        # (We assume all finalists in a battery are at the same round in a resume
        # because we always run all finalists for a battery round together.)
        prior_max = 0
        for model_id in finalists:
            prior_max = max(prior_max, prior_rounds.get((model_id, battery), 0))
        next_round[battery] = prior_max + 1

    # Main loop: while any battery is still active, run one round of that battery.
    while True:
        any_battery_still_active = False
        for battery in batteries:
            if state.stopped_at_cap:
                break
            stopper = state.stoppers[battery]
            round_num = next_round[battery]
            # Stop conditions for this battery:
            if round_num > seq_config.n_max:
                continue
            if round_num > seq_config.n_initial and stopper.should_stop():
                continue
            any_battery_still_active = True

            items = full_banks.get(battery, [])
            if not items:
                next_round[battery] = round_num + 1
                continue

            # Run round_num for ALL finalists in this battery.
            round_scores_for_stopper: list[float] = []
            for model_id in finalists:
                if state.stopped_at_cap:
                    break
                b_id = battery_ids[battery]
                # Round ID: deterministic per (sweep, model, battery, round) so
                # resume can detect duplicates via run insert conflict.
                round_id = f"run-{uuid.uuid4()}"
                if record_to_db and conn is not None:
                    _insert_run(
                        conn,
                        run_id=round_id,
                        sweep_id=sweep_id,
                        model_id=model_id,
                        battery_id=b_id,
                        round_num=round_num,
                        total_tokens=0,
                        total_cost_cny=0.0,
                        infra_ok=True,
                    )
                round_total_tokens = 0
                round_infra_ok = True
                mb_key = _key(model_id, battery)
                state.measurements_by_model_battery.setdefault(mb_key, {})
                round_scores_for_model: list[float] = []
                for rep, env in enumerate(items, start=1):
                    # Skip already-recorded (model, battery, item, rep) pairs.
                    if (model_id, battery, env.item_key, rep) in already_recorded:
                        # Pull existing score from state (populated by _rebuild_stopper_from_db).
                        existing = state.measurements_by_model_battery[mb_key].get(env.item_key, [])
                        # Find the score for this rep index (rep is 1-indexed, list is 0-indexed).
                        if rep - 1 < len(existing):
                            round_scores_for_model.append(existing[rep - 1])
                        continue
                    ok, result = call_and_grade(adapter, model_id, env, item_repo, registry)
                    state.total_calls += 1
                    call_tokens = result.tokens_in + result.tokens_out
                    state.total_tokens += call_tokens
                    round_total_tokens += call_tokens
                    if not ok:
                        round_infra_ok = False
                        if result.infra_failure and conn is not None:
                            _insert_infra_incident(
                                conn,
                                round_id,
                                kind=result.infra_failure or "unknown",
                                details=result.detail or {},
                            )
                    if record_to_db and conn is not None:
                        _insert_measurement(
                            conn,
                            measurement_id=f"m-{uuid.uuid4()}",
                            run_id=round_id,
                            item_id=env.item_key,
                            repetition=rep,
                            score=result.score,
                            tokens_in=result.tokens_in,
                            tokens_out=result.tokens_out,
                            latency_ms=result.latency_ms,
                            response_text=result.response_text,
                            thinking_text=result.thinking_text,
                        )
                    state.measurements_by_model_battery[mb_key].setdefault(env.item_key, []).append(
                        result.score
                    )
                    round_scores_for_model.append(result.score)

                    # Token cap check.
                    if state.total_tokens >= token_cap:
                        state.stopped_at_cap = True
                        state.stopped_reason = (
                            f"Token cap {state.total_tokens:,} / {token_cap:,} reached "
                            f"during {model_id}/{battery}/{round_num}."
                        )
                        break
                # Update run row with total tokens.
                if record_to_db and conn is not None:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE hr.run SET total_tokens = %s, infra_ok = %s, "
                            "finished_at = %s WHERE run_id = %s",
                            (
                                round_total_tokens,
                                round_infra_ok,
                                datetime.now(timezone.utc),
                                round_id,
                            ),
                        )
                    conn.commit()
                # Aggregate this model's round scores into battery-wide stopper bucket.
                round_scores_for_stopper.extend(round_scores_for_model)

                if state.stopped_at_cap:
                    break

            # After this round for all finalists, push the aggregate scores to the stopper.
            state.stoppers[battery].add_round(round_scores_for_stopper)
            state.n_rounds_done[battery] = state.stoppers[battery].n_rounds
            next_round[battery] = round_num + 1

            if state.stopped_at_cap:
                break

        if state.stopped_at_cap:
            break
        if not any_battery_still_active:
            break


def _parse_mb_key(mb_key: str) -> tuple[str, str, str]:
    """Parse '{model_id}|{battery_code}' back into (model_id, battery_code).
    Returns (model_id, '', battery_code) for API symmetry but we only use the first two."""
    parts = mb_key.split("|", 1)
    return (parts[0], "", parts[1] if len(parts) == 2 else "")


def _rebuild_stopper_from_db(
    state: Stage1SweepState,
    conn,
    sweep_id: str,
    batteries: tuple[str, ...],
    seq_config: SequentialConfig,
) -> None:
    """Reconstruct stoppers + n_rounds_done from DB for resume."""
    # Reset stoppers.
    for battery in batteries:
        state.stoppers[battery] = SequentialStopper(battery_code=battery, config=seq_config)
        state.n_rounds_done[battery] = 0
    if conn is None:
        return
    # Pull all measurements per (model, battery, round), group into per-round scores.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.model_id, b.battery_code, r.round, m.score
            FROM hr.measurement m
            JOIN hr.run r ON m.run_id = r.run_id
            JOIN hr.battery b ON r.battery_id = b.battery_id
            WHERE r.sweep_id = %s
            ORDER BY r.model_id, b.battery_code, r.round, m.item_id, m.repetition
            """,
            (sweep_id,),
        )
        rows = cur.fetchall()
    # Group by (battery, round) -> list of scores (across all finalists).
    per_battery_round: dict[str, dict[int, list[float]]] = {}
    for model_id, battery_code, round_num, score in rows:
        per_battery_round.setdefault(battery_code, {}).setdefault(round_num, []).append(float(score))
    # Rebuild state.measurements_by_model_battery and stoppers.
    state.measurements_by_model_battery = {}
    # Group again per (model, battery, item_key, repetition) to rebuild per-item scores.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.model_id, b.battery_code, m.item_id, m.repetition, m.score
            FROM hr.measurement m
            JOIN hr.run r ON m.run_id = r.run_id
            JOIN hr.battery b ON r.battery_id = b.battery_id
            WHERE r.sweep_id = %s
            ORDER BY r.model_id, b.battery_code, m.item_id, m.repetition
            """,
            (sweep_id,),
        )
        rows = cur.fetchall()
    per_model_battery: dict[str, dict[str, list[float]]] = {}
    for model_id, battery_code, item_id, repetition, score in rows:
        mb_key = _key(model_id, battery_code)
        per_model_battery.setdefault(mb_key, {}).setdefault(item_id, []).append(float(score))
    state.measurements_by_model_battery = per_model_battery

    # Update stoppers per battery: for each round 1..max_round, add_round(scores).
    for battery in batteries:
        per_round = per_battery_round.get(battery, {})
        if not per_round:
            continue
        max_round = max(per_round.keys())
        for r in range(1, max_round + 1):
            state.stoppers[battery].add_round(per_round.get(r, []))
        state.n_rounds_done[battery] = max_round


# ---------------------------------------------------------------------------
# Separation (2-D aligned: n_items × n_reps)
# ---------------------------------------------------------------------------
def build_aligned_2d(
    per_item_scores: dict[str, list[float]],
) -> tuple[np.ndarray, list[str]]:
    """Convert {item_key: [scores]} to a 2-D ndarray (n_items × n_reps).

    All items are padded to the max rep count with NaN so two models' arrays
    can be aligned item-wise. Returns (array, item_keys_in_order).
    """
    if not per_item_scores:
        return np.zeros((0, 0)), []
    item_keys = sorted(per_item_scores.keys())
    max_reps = max((len(scores) for scores in per_item_scores.values()), default=0)
    if max_reps == 0:
        return np.zeros((len(item_keys), 0)), item_keys
    arr = np.full((len(item_keys), max_reps), np.nan, dtype=float)
    for i, ik in enumerate(item_keys):
        scores = per_item_scores[ik]
        arr[i, : len(scores)] = scores
    return arr, item_keys


def _bootstrap_separation_from_stage1(
    state: Stage1SweepState,
) -> dict[str, list[dict]]:
    """Per-battery separation matrix over the full finals.

    For each battery, build aligned 2-D arrays for each model (n_items × n_reps
    where n_items is the intersection of items across finalists). Then run
    paired_bootstrap_separation in 2-D mode (item + repetition resampling).
    """
    result: dict[str, list[dict]] = {}
    per_battery: dict[str, dict[str, tuple[np.ndarray, list[str]]]] = {}

    for key_str, per_item in state.measurements_by_model_battery.items():
        parts = key_str.split("|", 1)
        if len(parts) != 2:
            continue
        model_id, battery = parts
        arr, item_keys = build_aligned_2d(per_item)
        if arr.shape[0] == 0 or arr.shape[1] == 0:
            continue
        per_battery.setdefault(battery, {})[model_id] = (arr, item_keys)

    for battery_code, model_arrays in per_battery.items():
        model_ids = sorted(model_arrays.keys())
        if len(model_ids) < 2:
            continue
        # Align on shared item_keys BEFORE any stacking: finalists that crashed /
        # resumed have ragged item sets, and np.stack demands identical shapes.
        # Intersect item_keys across all models, restrict each model's array to
        # those rows (in a common key order), then trim to the min rep count.
        shared_keys = set(model_arrays[model_ids[0]][1])
        for m in model_ids[1:]:
            shared_keys &= set(model_arrays[m][1])
        if not shared_keys:
            continue
        shared_keys = sorted(shared_keys)
        n_reps = min(model_arrays[m][0].shape[1] for m in model_ids)
        if n_reps == 0:
            continue
        aligned: dict[str, np.ndarray] = {}
        for m in model_ids:
            arr, keys = model_arrays[m]
            pos = {k: i for i, k in enumerate(keys)}
            idx = [pos[k] for k in shared_keys]
            aligned[m] = arr[idx, :n_reps]
        # Shared item rows where every model produced at least one real score.
        has = np.stack([~np.isnan(aligned[m]) for m in model_ids], axis=0)
        shared_mask = has.any(axis=-1).all(axis=0)  # (n_shared_keys,) bool
        if int(shared_mask.sum()) == 0:
            continue
        for m in model_ids:
            aligned[m] = aligned[m][shared_mask, :]
        pairs: list[dict] = []
        for i, ma in enumerate(model_ids):
            for mb in model_ids[i + 1 :]:
                sa = aligned[ma]
                sb = aligned[mb]
                p_a = paired_bootstrap_separation(sa, sb)
                p_b = paired_bootstrap_separation(sb, sa)
                # Record the ACTUAL winner: max() alone flips the label when the
                # larger confidence belongs to sb (e.g. vision models beating a
                # blind model were being reported as the blind model winning).
                if p_a >= p_b:
                    winner, loser, conf = ma, mb, p_a
                else:
                    winner, loser, conf = mb, ma, p_b
                classified = classify(conf)
                if classified == "separated":
                    pairs.append(
                        {
                            "model_a": winner,
                            "model_b": loser,
                            "p_separated": conf,
                            "p_weak": 1.0 - conf,
                            "p_tie": 0.0,
                        }
                    )
                elif classified == "weak":
                    pairs.append(
                        {
                            "model_a": winner,
                            "model_b": loser,
                            "p_separated": 0.0,
                            "p_weak": conf,
                            "p_tie": 1.0 - conf,
                        }
                    )
                else:
                    pairs.append(
                        {
                            "model_a": ma,
                            "model_b": mb,
                            "p_separated": 0.0,
                            "p_weak": 0.0,
                            "p_tie": 1.0,
                        }
                    )
        result[battery_code] = pairs
    return result


# ---------------------------------------------------------------------------
# Dry-run plan
# ---------------------------------------------------------------------------
@dataclass
class FinalsCallPlan:
    finalists: list[str]
    finalist_selection: FinalistSelection
    battery_item_counts: dict[str, int]
    battery_n_rounds_min: dict[str, int]
    battery_n_rounds_max: dict[str, int]
    battery_half_width_threshold: dict[str, float]
    estimated_min_calls: int
    estimated_max_calls: int
    estimated_min_tokens: int
    estimated_max_tokens: int
    budget_cap: int
    within_budget: bool


def build_finals_plan(
    finalists: list[str],
    full_banks: dict[str, list[ItemEnvelope]],
    seq_config: SequentialConfig,
    budget_cap: int = STAGE1_TOKEN_CAP,
) -> FinalsCallPlan:
    battery_counts = {b: len(items) for b, items in full_banks.items()}
    total_items = sum(battery_counts.values())
    n_finalists = len(finalists)
    # Each round = n_finalists × total_items calls.
    est_min_calls = n_finalists * total_items * seq_config.n_initial
    est_max_calls = n_finalists * total_items * seq_config.n_max
    est_min_tokens = est_min_calls * EST_TOKENS_PER_CALL
    est_max_tokens = est_max_calls * EST_TOKENS_PER_CALL
    thresholds = {
        b: seq_config.thresholds.get(b, float("inf")) for b in full_banks.keys()
    }
    return FinalsCallPlan(
        finalists=list(finalists),
        finalist_selection=FinalistSelection(per_battery={}, finalists=list(finalists), rationale=""),
        battery_item_counts=battery_counts,
        battery_n_rounds_min={b: seq_config.n_initial for b in full_banks.keys()},
        battery_n_rounds_max={b: seq_config.n_max for b in full_banks.keys()},
        battery_half_width_threshold=thresholds,
        estimated_min_calls=est_min_calls,
        estimated_max_calls=est_max_calls,
        estimated_min_tokens=est_min_tokens,
        estimated_max_tokens=est_max_tokens,
        budget_cap=budget_cap,
        within_budget=est_max_tokens <= budget_cap,
    )


def print_finals_plan(
    plan: FinalsCallPlan,
    selection: FinalistSelection | None = None,
) -> None:
    print("=== Stage 1 Finals Call Plan ===")
    if selection is not None and selection.rationale:
        print(selection.rationale)
    print(f"\nFinalists ({len(plan.finalists)}):")
    for m in plan.finalists:
        print(f"  - {m}")
    print("\nFull item banks:")
    total_items = 0
    for b, count in plan.battery_item_counts.items():
        spec_count = STAGE1_FULL_BANK_SIZES.get(b, "?")
        print(f"  {b}: {count} items (spec target: {spec_count})")
        total_items += count
    print(f"  TOTAL: {total_items} items across {len(plan.battery_item_counts)} batteries")
    print(f"\nSequential-n:")
    print(f"  Pilot n_initial = {min(plan.battery_n_rounds_min.values())}")
    print(f"  Max n_max       = {max(plan.battery_n_rounds_max.values())}")
    print("  Half-width thresholds (spec §10.7):")
    for b, t in plan.battery_half_width_threshold.items():
        print(f"    {b}: ±{t:.1f}")
    print(f"\nEstimate ({len(plan.finalists)} finalists × {total_items} items):")
    print(f"  Min calls (pilot only): {plan.estimated_min_calls:,} → ~{plan.estimated_min_tokens:,} tokens")
    print(f"  Max calls (n_max):      {plan.estimated_max_calls:,} → ~{plan.estimated_max_tokens:,} tokens")
    print(f"  Stage 1 budget cap: {plan.budget_cap:,} tokens")
    if plan.within_budget:
        print("  ✓ Estimated tokens are within cap (even at n_max).")
    else:
        over = plan.estimated_max_tokens - plan.budget_cap
        print(f"  ⚠ OVER budget at n_max by {over:,} tokens.")
        print("    (Sequential runner halts early on cap; pilot phase will complete.)")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------
def run_finals(
    adapter: _AdapterFacade,
    *,
    item_repo: Path | None = None,
    finalists: list[str] | None = None,
    batteries: tuple[str, ...] = STAGE1_DECIDING_BATTERIES,
    thresholds_path: Path = DEFAULT_THRESHOLDS_PATH,
    n_initial: int = STAGE1_N_INITIAL,
    n_max: int = STAGE1_N_MAX,
    token_cap: int = STAGE1_TOKEN_CAP,
    sweep_id: str | None = None,
    dry_run: bool = False,
    init_db: bool = True,
    record_to_db: bool = True,
    allow_db_missing_for_finalists: bool = False,
) -> tuple[FinalsCallPlan, Stage1SweepState | None, FinalistSelection]:
    """Run the Stage 1 finals sweep.

    Returns (plan, state, selection). ``state`` is None on dry_run.
    ``item_repo`` defaults to :func:`hr.config.itemrepo_path`.
    """
    if item_repo is None:
        item_repo = itemrepo_path()
    # 1. Finalist selection.
    if finalists is None:
        selection = select_finalists_from_stage0(
            deciding_batteries=batteries,
            allow_db_missing=allow_db_missing_for_finalists,
        )
        finalists = selection.finalists
    else:
        # User-overridden finalist list.
        selection = FinalistSelection(
            per_battery={},
            finalists=list(finalists),
            rationale=f"User-provided finalist list: {finalists}",
        )

    # 2. Load full banks.
    full_banks = load_full_banks(item_repo, batteries=batteries)

    # 3. Configure sequential stopper.
    if thresholds_path.exists():
        seq_config = SequentialConfig.from_yaml(
            str(thresholds_path), required_batteries=list(STAGE0_BATTERIES)
        )
    else:
        # Use defaults from thresholds.yaml spec.
        seq_config = SequentialConfig(
            thresholds={
                "reasoning": 2.0,
                "hallucination": 2.0,
                "tool_a": 3.0,
                "vision": 3.0,
                "tool_b": 5.0,
            },
            n_initial=n_initial,
            n_max=n_max,
        )
    # Override from CLI args.
    seq_config.n_initial = n_initial
    seq_config.n_max = n_max

    # 4. Build plan (for reporting / dry-run).
    plan = build_finals_plan(finalists, full_banks, seq_config, budget_cap=token_cap)
    plan.finalist_selection = selection
    if dry_run:
        print_finals_plan(plan, selection)
        return plan, None, selection

    # 5. Init DB + records.
    if init_db or record_to_db:
        _init_db()
        conn = _connect()
    else:
        conn = None

    try:
        # 6. Upsert reference data.
        if conn is not None:
            _upsert_seat(conn, STAGE1_SEAT_CODE, "Stage 1 finalists sweep")
            _ensure_provider_model_records(conn, tuple(finalists))
            battery_ids: dict[str, str] = {}
            for bcode in batteries:
                battery_ids[bcode] = _upsert_battery(conn, bcode, f"Stage-1 {bcode} battery (full bank)")
            for bcode in batteries:
                b_id = battery_ids[bcode]
                for pos, env in enumerate(full_banks.get(bcode, [])):
                    _upsert_item_pool(conn, env)
                    _upsert_battery_item(conn, b_id, env.item_key, pos)

            # Create or resume sweep.
            if sweep_id is None:
                sweep_id = f"stage1-{uuid.uuid4()}"
            purpose = (
                f"Stage 1 finalists sweep\n"
                f"finalists: {finalists}\n"
                f"selection_rationale:\n{selection.rationale}\n"
                f"full_bank_sizes: { {b: len(items) for b, items in full_banks.items()} }\n"
                f"n_initial: {n_initial}, n_max: {n_max}, token_cap: {token_cap}\n"
                f"thresholds (battery -> half_width): {dict(seq_config.thresholds)}"
            )
            _insert_sweep(conn, sweep_id, STAGE1_SEAT_CODE, purpose)
        else:
            if sweep_id is None:
                sweep_id = f"stage1-{uuid.uuid4()}"
            battery_ids = {b: f"battery-{b}" for b in batteries}

        state = Stage1SweepState(sweep_id=sweep_id, finalists=list(finalists))
        registry = build_default_registry()

        # 7. Resume check.
        already_recorded: set[tuple[str, str, str, int]] = set()
        prior_rounds: dict[tuple[str, str], int] = {}
        if conn is not None:
            already_recorded = _recorded_measurement_keys(conn, sweep_id)
            prior_rounds = _max_round_per_model_battery(conn, sweep_id)
        if already_recorded:
            print(f"Resuming sweep {sweep_id}: skipping {len(already_recorded)} already-recorded measurements.")

        # 8. Run the finals loop.
        try:
            _run_finals_loop(
                adapter=adapter,
                item_repo=item_repo,
                finalists=finalists,
                full_banks=full_banks,
                batteries=batteries,
                battery_ids=battery_ids,
                seq_config=seq_config,
                token_cap=token_cap,
                state=state,
                registry=registry,
                conn=conn,
                sweep_id=sweep_id,
                record_to_db=record_to_db and conn is not None,
                already_recorded=already_recorded,
                prior_rounds=prior_rounds,
            )
        except KeyboardInterrupt:
            if conn is not None:
                print(f"\nSweep interrupted at {state.total_tokens:,} tokens.")
            raise

        if state.stopped_at_cap:
            print(f"\n⚠ Stage 1 halted at {state.total_tokens:,} / {token_cap:,} tokens.")
            print(f"   Reason: {state.stopped_reason}")
        else:
            print(f"\n✓ Stage 1 complete. Total tokens: {state.total_tokens:,} / {token_cap:,}")
            print(f"   Rounds per battery: {state.n_rounds_done}")

        # 9. Compute separation and record.
        sep = _bootstrap_separation_from_stage1(state)
        if conn is not None:
            for battery_code, pairs in sep.items():
                if battery_code not in battery_ids:
                    continue
                b_id = battery_ids[battery_code]
                for p in pairs:
                    _insert_separation(
                        conn,
                        separation_id=f"sep-{uuid.uuid4()}",
                        sweep_id=sweep_id,
                        battery_id=b_id,
                        model_a=p["model_a"],
                        model_b=p["model_b"],
                        p_separated=p["p_separated"],
                        p_weak=p["p_weak"],
                        p_tie=p["p_tie"],
                    )

        print(f"Sweep ID: {sweep_id}")
        _print_matrix(sep)
        return plan, state, selection
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Separation report (--report)
# ---------------------------------------------------------------------------
def read_finals_separation_from_db(sweep_id: str) -> dict[str, list[dict]]:
    """Load the persisted separation matrix for a Stage 1 sweep."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT battery_id, model_a, model_b, p_separated, p_weak, p_tie
                FROM hr.separation WHERE sweep_id = %s
                ORDER BY battery_id, model_a, model_b
                """,
                (sweep_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    sep: dict[str, list[dict]] = {}
    for battery_id, a, b, ps, pw, pt in rows:
        battery_code = battery_id.replace("battery-", "")
        sep.setdefault(battery_code, []).append(
            {
                "model_a": a,
                "model_b": b,
                "p_separated": float(ps),
                "p_weak": float(pw),
                "p_tie": float(pt),
            }
        )
    return sep


def list_finals_sweeps() -> list[tuple[str, str, str]]:
    """Return (sweep_id, purpose, created_at) for all Stage 1 finals sweeps in DB."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sweep_id, purpose, created_at
                FROM hr.sweep WHERE seat_code = %s
                ORDER BY created_at DESC
                """,
                (STAGE1_SEAT_CODE,),
            )
            return cur.fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hr2.stage1", description="Stage 1 finals runner.")
    p.add_argument("--dry-run", action="store_true", help="Print plan + finalist selection (no API calls)")
    p.add_argument("--run", action="store_true", help="Run the finals sweep with live adapter")
    p.add_argument("--report", action="store_true", help="Print separation matrix from DB")
    p.add_argument("--sweep-id", default=None, help="Specific sweep id for --report or --run resume")
    p.add_argument("--token-cap", type=int, default=STAGE1_TOKEN_CAP, help="Token budget cap (default 90M)")
    p.add_argument("--n-initial", type=int, default=STAGE1_N_INITIAL, help="Pilot rounds (default 3)")
    p.add_argument("--n-max", type=int, default=STAGE1_N_MAX, help="Max rounds per battery (default 10)")
    p.add_argument("--thresholds", default=str(DEFAULT_THRESHOLDS_PATH), help="Path to thresholds.yaml")
    p.add_argument(
        "--models",
        default=None,
        help="Comma-separated model ids (overrides finalist selection from DB)",
    )
    p.add_argument(
        "--item-repo",
        default=None,
        help="Path to item repo (default: HR_ITEMREPO env or HR_HOME/itemrepo)",
    )
    p.add_argument(
        "--no-db",
        action="store_true",
        help="Do not record to the DB (for local testing)",
    )
    p.add_argument(
        "--use-routed-adapter",
        action="store_true",
        help="Use RoutedAdapter (dispatches bailian/kimi→Anthropic, deepseek→OpenAI)",
    )
    return p


def _cli_main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    # Parse optional model override.
    finalists_override: list[str] | None = None
    if args.models:
        finalists_override = [m.strip() for m in args.models.split(",") if m.strip()]

    item_repo = Path(args.item_repo) if args.item_repo else itemrepo_path()
    thresholds_path = Path(args.thresholds)

    # --dry-run: just print the plan.
    if args.dry_run:
        try:
            selection = select_finalists_from_stage0(
                deciding_batteries=STAGE1_DECIDING_BATTERIES,
                allow_db_missing=(finalists_override is not None),
            )
        except RuntimeError as e:
            print(f"Cannot select finalists: {e}", file=sys.stderr)
            return 1
        if finalists_override is not None:
            selection = FinalistSelection(
                per_battery={},
                finalists=list(finalists_override),
                rationale=f"User-provided finalist list: {finalists_override}",
            )
        full_banks = load_full_banks(item_repo, batteries=STAGE1_DECIDING_BATTERIES)
        if thresholds_path.exists():
            seq_config = SequentialConfig.from_yaml(str(thresholds_path))
        else:
            seq_config = SequentialConfig(
                thresholds={"reasoning": 2.0, "hallucination": 2.0, "tool_a": 3.0, "vision": 3.0},
                n_initial=args.n_initial,
                n_max=args.n_max,
            )
        seq_config.n_initial = args.n_initial
        seq_config.n_max = args.n_max
        plan = build_finals_plan(selection.finalists, full_banks, seq_config, budget_cap=args.token_cap)
        plan.finalist_selection = selection
        print_finals_plan(plan, selection)
        return 0

    # --report: read from DB.
    if args.report:
        sweep_id = args.sweep_id
        if sweep_id is None:
            try:
                sweeps = list_finals_sweeps()
            except Exception as e:
                print(f"DB not available: {e}", file=sys.stderr)
                return 1
            if not sweeps:
                print("No Stage 1 finals sweeps recorded yet.", file=sys.stderr)
                return 1
            sweep_id = sweeps[0][0]
        sep = read_finals_separation_from_db(sweep_id)
        print(f"Sweep ID: {sweep_id}")
        _print_matrix(sep)
        return 0

    # --run (default): run with live adapter.
    from hr.adapters import RoutedAdapter

    adapter: _AdapterFacade = RoutedAdapter()
    try:
        run_finals(
            adapter=adapter,
            item_repo=item_repo,
            finalists=finalists_override,
            thresholds_path=thresholds_path,
            n_initial=args.n_initial,
            n_max=args.n_max,
            token_cap=args.token_cap,
            sweep_id=args.sweep_id,
            record_to_db=not args.no_db,
        )
    except Exception as e:
        print(f"Stage 1 failed: {e}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    return _cli_main()

from hr.stage1_cli import _cli_main, main

if __name__ == "__main__":
    raise SystemExit(main())
