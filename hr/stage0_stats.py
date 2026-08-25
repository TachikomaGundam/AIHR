from __future__ import annotations

from dataclasses import dataclass, field



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


def should_exclude_zero(infra_failure: str | None) -> bool:
    """Whether a 0-score caused by infra failure should be excluded from stats.

    Stage 0 policy: only exclude if corroboration exists. For full rigor we
    defer to the stats module's ``should_exclude_zero``. Here we return
    conservatively (exclude only retryable classes).
    """
    if infra_failure is None:
        return False
    from hr.scheduler.taxonomy import FailureCode, retryable

    try:
        failure_code = FailureCode(infra_failure)
    except ValueError:
        return False
    return retryable(failure_code)


def _bootstrap_separation_from_state(
    state: SweepState,
) -> dict[str, list[dict]]:
    """Run paired bootstrap separation within each battery.

    Returns: dict of battery_code -> list of {model_a, model_b, p_separated, p_weak, p_tie}.
    """
    from hr.stats.bootstrap import classify, paired_bootstrap_separation

    result: dict[str, list[dict]] = {}
    per_battery: dict[str, dict[str, list[float]]] = {}
    # Group scores by per (battery, model) — mean over items per round.
    for key_str, per_item in state.measurements_by_model_battery.items():
        model_id, battery = key_str.split("|", 1)
        per_battery.setdefault(battery, {})
        # Per-model scores: average over all item scores in this battery.
        all_scores: list[float] = []
        for item_scores in per_item.values():
            all_scores.extend(item_scores)
        if not all_scores:
            continue
        per_battery[battery][model_id] = all_scores

    for battery_code, model_scores in per_battery.items():
        pairs: list[dict] = []
        model_ids = sorted(model_scores.keys())
        for i, ma in enumerate(model_ids):
            for mb in model_ids[i + 1 :]:
                sa = model_scores[ma]
                sb = model_scores[mb]
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
