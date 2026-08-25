
from __future__ import annotations


import numpy as np

from hr.stage1_state import Stage1SweepState

# Reuse stage0's helpers and DB plumbing.

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
    """Per-battery pair-decision matrix over the full finals.

    Every finalist pair whose decision is computed goes through its
    anytime-valid empirical-Bernstein sequence (``state.pair_stoppers``,
    complete-round paired differences only). Each row carries the effect
    estimate, the anytime-valid interval, the resolution status and the
    practical-effect decision, plus the legacy ``p_*`` columns consumed by
    the DB separation insert (``model_a`` = winner for decided pairs;
    ``p_separated`` = 1 - per-pair alpha for decided pairs).
    """
    result: dict[str, list[dict]] = {}
    for key_str, seq in state.pair_stoppers.items():
        # key: f"{model_a}|{model_b}|{battery_code}"
        parts = key_str.split("|", 2)
        if len(parts) != 3:
            continue
        model_a, model_b, battery = parts
        decision = seq.decide(model_a=model_a, model_b=model_b)
        row = decision.to_dict()
        if decision.status == "decided":
            if decision.winner == model_b:
                row["model_a"], row["model_b"] = model_b, model_a
            row["p_separated"] = 1.0 - decision.alpha
            row["p_weak"] = decision.alpha
            row["p_tie"] = 0.0
        else:
            row["p_separated"] = 0.0
            row["p_weak"] = 0.0
            row["p_tie"] = 1.0
        result.setdefault(battery, []).append(row)
    for battery, rows in result.items():
        rows.sort(key=lambda r: (r["model_a"], r["model_b"]))
    return result


# ---------------------------------------------------------------------------
# Dry-run plan
