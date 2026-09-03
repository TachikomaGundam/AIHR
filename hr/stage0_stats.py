from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)



@dataclass
class SweepState:
    sweep_id: str
    total_tokens: int = 0
    total_calls: int = 0
    stopped_at_cap: bool = False
    stopped_reason: str = ""
    measurements_by_model_battery: dict[str, dict[str, list[float]]] = field(default_factory=dict)
    # key: f"{model_id}|{battery_code}" -> { item_key: [scores] }
    # We keep a per-(model,battery) dict of item_key -> [scores across repetitions]


def _key(model_id: str, battery: str) -> str:
    return f"{model_id}|{battery}"


def _bootstrap_separation_from_state(
    state: SweepState,
) -> dict[str, list[dict]]:
    """Run paired bootstrap separation within each battery.

    Per battery, every model pair is aligned on the intersection of item keys
    present in BOTH models' per-item dicts. Each side is flattened in
    ``sorted(shared_keys)`` order and truncated per item to the first
    k = min(len(a[item]), len(b[item])) scores. Pairing is POSITIONAL in
    append order, not round-index aligned: stage0 records scores, not
    (round, score) tuples, so mid-list holes pair the first-k scores of each
    item against each other — the same pairing philosophy HEAD used (its
    flatten was already positional), and no anytime-valid claim rides stage0's
    heuristic separation.

    Flattening in sorted item order (vs HEAD's dict-insertion order) is a
    deliberate trade-off: results become resume-stable — hr/stage1_resume.py
    rebuilds measurements ``ORDER BY item_id`` — at the cost of a possible
    p-value shift for fixtures whose insertion order was not already sorted.
    ACCEPTED.

    Pairs with no shared usable items are SKIPPED with a warning and emit no
    row (never raise, never zip mismatched shapes). Models present as MISSING
    keys and models present as empty {} dicts are treated alike: they share
    no items with anyone, so every pair involving them is skipped. A battery
    with zero surviving pairs still yields its key with an empty list.

    Returns: dict of battery_code -> list of {model_a, model_b, p_separated, p_weak, p_tie}.
    """
    from hr.stats.bootstrap import classify, paired_bootstrap_separation

    result: dict[str, list[dict]] = {}
    # battery_code -> model_id -> item_key -> [scores across repetitions]
    per_battery: dict[str, dict[str, dict[str, list[float]]]] = {}
    for key_str, per_item in state.measurements_by_model_battery.items():
        model_id, battery = key_str.split("|", 1)
        # Register the battery even when the model contributes nothing, so a
        # battery with zero surviving pairs still yields its key ([] pairs).
        per_battery.setdefault(battery, {})[model_id] = per_item

    for battery_code, model_item_dicts in per_battery.items():
        pairs: list[dict] = []
        model_ids = sorted(model_item_dicts.keys())
        for i, ma in enumerate(model_ids):
            for mb in model_ids[i + 1 :]:
                a_items = model_item_dicts[ma]
                b_items = model_item_dicts[mb]
                common_keys = sorted(set(a_items) & set(b_items))
                if len(common_keys) < 1:
                    log.warning(
                        "skipping separation pair %s vs %s on %s: no shared items (%d vs %d)",
                        ma,
                        mb,
                        battery_code,
                        len(a_items),
                        len(b_items),
                    )
                    continue
                sa: list[float] = []
                sb: list[float] = []
                for item in common_keys:
                    # Positional truncation: pair only the first
                    # min(len_a, len_b) scores of each shared item.
                    k = min(len(a_items[item]), len(b_items[item]))
                    sa.extend(a_items[item][:k])
                    sb.extend(b_items[item][:k])
                if not sa:
                    log.warning(
                        "skipping separation pair %s vs %s on %s: shared items have no paired scores (%d vs %d)",
                        ma,
                        mb,
                        battery_code,
                        len(a_items),
                        len(b_items),
                    )
                    continue
                if len(sa) != len(sb):  # defensive: unreachable by construction above
                    log.warning(
                        "skipping separation pair %s vs %s on %s: unequal aligned lengths (%d vs %d)",
                        ma,
                        mb,
                        battery_code,
                        len(sa),
                        len(sb),
                    )
                    continue
                # Use both directions to compute weak / separated / tie.
                p_a = paired_bootstrap_separation(sa, sb)
                p_b = paired_bootstrap_separation(sb, sa)
                # spec §10.2: p = P(mean(A) > mean(B)); weak = max(p, 1-p) when not separated.
                if p_a >= p_b:
                    winner, loser, raw = ma, mb, p_a
                else:
                    winner, loser, raw = mb, ma, p_b
                classified = classify(raw)
                if classified == "separated":
                    pairs.append(
                        {
                            "model_a": winner,
                            "model_b": loser,
                            "p_separated": raw,
                            "p_weak": 1.0 - raw,
                            "p_tie": 0.0,
                            "directional": True,
                        }
                    )
                elif classified == "weak":
                    pairs.append(
                        {
                            "model_a": winner,
                            "model_b": loser,
                            "p_separated": 0.0,
                            "p_weak": raw,
                            "p_tie": 1.0 - raw,
                            "directional": True,
                        }
                    )
                else:
                    pairs.append(
                        {
                            "model_a": winner,
                            "model_b": loser,
                            "p_separated": 0.0,
                            "p_weak": 0.0,
                            "p_tie": 1.0,
                            "directional": True,
                        }
                    )
        result[battery_code] = pairs
    return result


def print_separation_matrix(state: SweepState | None = None, sweep_id: str | None = None) -> None:
    """Print the per-battery separation matrix from DB (or live state)."""
    if state is None and sweep_id is None:
        print("Provide sweep_id or live state.")
        return

    if state is not None:
        sep = _bootstrap_separation_from_state(state)
    else:
        # Load from DB.
        from hr.db import connect

        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT battery_id, model_a, model_b, p_separated, p_weak, p_tie "
            "FROM hr.separation WHERE sweep_id = %s ORDER BY battery_id, model_a, model_b",
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

    _print_matrix(sep)


def _print_matrix(sep: dict[str, list[dict]]) -> None:
    """Pretty-print the separation matrix."""
    print()
    print("=== Stage 0 Separation Matrix ===")

    for battery_code, pairs in sep.items():
        print(f"\n--- Battery: {battery_code} ---")
        if not pairs:
            print("  (no pairs recorded)")
            continue
        # Collect model ids and classify.
        classifications: dict[str, dict[str, str]] = {}
        all_models: set[str] = set()
        for p in pairs:
            ma, mb = p["model_a"], p["model_b"]
            all_models.add(ma)
            all_models.add(mb)
            label = "sep" if p["p_separated"] > 0 else "weak" if p["p_weak"] > 0 else "tie"
            classifications.setdefault(ma, {})[mb] = label
            classifications.setdefault(mb, {})[ma] = label

        sorted_models = sorted(all_models)
        print(f"  Models ({len(sorted_models)}): {', '.join(sorted_models)}")
        # Summary counts.
        n_sep = sum(1 for _, m in classifications.items() for _, v in m.items() if v == "sep") // 2
        n_weak = sum(1 for _, m in classifications.items() for _, v in m.items() if v == "weak") // 2
        n_tie = sum(1 for _, m in classifications.items() for _, v in m.items() if v == "tie") // 2
        print(f"  Separated: {n_sep} pairs | Weak: {n_weak} pairs | Tie: {n_tie} pairs")

        # Show a matrix.
        header = "     " + "".join([f"{m[-12:]:>13}" for m in sorted_models])
        print(header)
        for ma in sorted_models:
            row = f"{ma[-20:]:>21} "
            for mb in sorted_models:
                if ma == mb:
                    row += f"{'--':>13}"
                else:
                    label = classifications.get(ma, {}).get(mb, "?")
                    row += f"{label:>13}"
            print(row)


# ---------------------------------------------------------------------------
# Main sweep runner
# ---------------------------------------------------------------------------
