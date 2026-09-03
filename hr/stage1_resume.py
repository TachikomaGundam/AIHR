from __future__ import annotations

from itertools import combinations

from hr.stage0 import _key
from hr.stats.sequential import SequentialConfig, SequentialStopper, normalize_bounded_score
from hr.stage1_state import Stage1SweepState, make_pair_sequence

#: Stage-1 scores persisted by the loop are GradeResult values on [0, 1]
#: (hr/graders/base.py); normalize explicitly so resumed paired differences
#: enter the anytime-valid sequence in [-1, 1] just like a fresh sweep.
STAGE1_SCORE_SCALE: float = 1.0


def _pair_key(model_a: str, model_b: str, battery: str) -> str:
    return f"{model_a}|{model_b}|{battery}"

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
    expected_measurements: dict[str, int] | None = None,
) -> None:
    """Reconstruct stoppers + n_rounds_done from DB for resume."""
    # Reset stoppers.
    for battery in batteries:
        state.stoppers[battery] = SequentialStopper(battery_code=battery, config=seq_config)
        for model_id in state.finalists:
            state.model_stoppers[_key(model_id, battery)] = SequentialStopper(
                battery_code=battery, config=seq_config
            )
        for model_a, model_b in combinations(state.finalists, 2):
            state.pair_stoppers[_pair_key(model_a, model_b, battery)] = make_pair_sequence(
                battery, seq_config, len(state.finalists)
            )
        state.n_rounds_done[battery] = 0
    if conn is None:
        return
    # Pull all measurements per (model, battery, round), group into per-round scores.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.model_id, b.battery_code, r.round, m.score, m.item_id, m.repetition
              FROM hr.measurement m
              JOIN hr.run r ON m.run_id = r.run_id
              JOIN hr.battery b ON r.battery_id = b.battery_id
            WHERE r.sweep_id = %s
            ORDER BY r.model_id, b.battery_code, r.round, m.item_id, m.repetition
            """,
            (sweep_id,),
        )
        rows = cur.fetchall()
    # Group by both battery and model so resumed stopping preserves the same
    # independent per-model precision rule as a fresh sweep. The widened
    # projection also carries the per-measurement (item_id, repetition) keys
    # for key-aligned pair feeds; legacy 4-tuple rows keep positional pairing.
    per_battery_round: dict[str, dict[int, list[float]]] = {}
    per_model_battery_round: dict[str, dict[int, list[float]]] = {}
    per_model_round_keys: dict[str, dict[int, list[tuple[str, int]]]] = {}
    has_keys = bool(rows) and len(rows[0]) >= 6
    for row in rows:
        model_id, battery_code, round_num, score = row[0], row[1], row[2], row[3]
        per_battery_round.setdefault(battery_code, {}).setdefault(round_num, []).append(float(score))
        mb_key = _key(model_id, battery_code)
        per_model_battery_round.setdefault(mb_key, {}).setdefault(
            round_num, []
        ).append(float(score))
        if has_keys:
            per_model_round_keys.setdefault(mb_key, {}).setdefault(round_num, []).append(
                (row[4], row[5])
            )
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

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(m.tokens_in + m.tokens_out), 0)::bigint,
                   COUNT(m.measurement_id)::int
              FROM hr.measurement m
              JOIN hr.run r ON m.run_id = r.run_id
             WHERE r.sweep_id = %s
            """,
            (sweep_id,),
        )
        usage_rows = cur.fetchall()
    if usage_rows:
        state.total_tokens = int(usage_rows[0][0])
        state.total_calls = int(usage_rows[0][1])

    # Update stoppers per battery: for each round 1..max_round, add_round(scores).
    for battery in batteries:
        per_round = per_battery_round.get(battery, {})
        if not per_round:
            continue
        max_round = max(per_round.keys())
        for r in range(1, max_round + 1):
            scores = per_round.get(r, [])
            expected = (expected_measurements or {}).get(battery)
            if expected is not None and len(scores) < expected:
                break
            state.stoppers[battery].add_round(scores)
            for model_id in state.finalists:
                model_scores = per_model_battery_round.get(
                    _key(model_id, battery), {}
                ).get(r, [])
                if model_scores:
                    state.model_stoppers[_key(model_id, battery)].add_round(model_scores)
            for model_a, model_b in combinations(state.finalists, 2):
                mb_key_a = _key(model_a, battery)
                mb_key_b = _key(model_b, battery)
                scores_a = per_model_battery_round.get(mb_key_a, {}).get(r, [])
                scores_b = per_model_battery_round.get(mb_key_b, {}).get(r, [])
                expected = (expected_measurements or {}).get(battery)
                if has_keys:
                    # Key-aligned mode: completeness is judged per model
                    # (expected // n_finalists — expected itself is the
                    # battery-wide count) and diffs pair only the shared
                    # (item_id, repetition) set in sorted order.
                    expected_model = (
                        expected // max(len(state.finalists), 1)
                        if expected is not None
                        else None
                    )
                    keys_a = per_model_round_keys.get(mb_key_a, {}).get(r, [])
                    keys_b = per_model_round_keys.get(mb_key_b, {}).get(r, [])
                    complete = (
                        bool(scores_a)
                        and bool(scores_b)
                        and (
                            expected_model is None
                            or (
                                len(keys_a) == expected_model
                                and len(keys_b) == expected_model
                            )
                        )
                    )
                    if not complete:
                        continue
                    shared = sorted(set(keys_a) & set(keys_b))
                    if not shared:
                        continue
                    by_key_a = dict(zip(keys_a, scores_a))
                    by_key_b = dict(zip(keys_b, scores_b))
                    diffs = [
                        normalize_bounded_score(by_key_a[k], max_score=STAGE1_SCORE_SCALE)
                        - normalize_bounded_score(by_key_b[k], max_score=STAGE1_SCORE_SCALE)
                        for k in shared
                    ]
                else:
                    # Legacy rows carry no item keys: fall back to positional
                    # pairing with the historical completeness formula.
                    complete = (
                        scores_a
                        and len(scores_a) == len(scores_b)
                        and (expected is None or len(scores_a) == expected)
                    )
                    if not complete:
                        continue
                    diffs = [
                        normalize_bounded_score(a, max_score=STAGE1_SCORE_SCALE)
                        - normalize_bounded_score(b, max_score=STAGE1_SCORE_SCALE)
                        for a, b in zip(scores_a, scores_b)
                    ]
                state.pair_stoppers[_pair_key(model_a, model_b, battery)].add_round(
                    diffs
                )
        state.n_rounds_done[battery] = state.stoppers[battery].n_rounds


# ---------------------------------------------------------------------------
# Separation (2-D aligned: n_items × n_reps)
