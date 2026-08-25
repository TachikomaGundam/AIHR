
from __future__ import annotations

from dataclasses import dataclass


from hr.items.schema import ItemEnvelope
from hr.stats.sequential import SequentialConfig
from hr.stage1_selection import (
    EST_TOKENS_PER_CALL,
    STAGE1_FULL_BANK_SIZES,
    STAGE1_TOKEN_CAP,
    FinalistSelection,
)

# Reuse stage0's helpers and DB plumbing.

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
    print("\nSequential-n:")
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
