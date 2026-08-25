from __future__ import annotations

from dataclasses import dataclass

from hr.fleet import fleet_models
from hr.items.loader import pool_hash
from hr.items.schema import ItemEnvelope, content_hash
from hr.stage0_selection import (
    EST_TOKENS_PER_CALL,
    STAGE0_BATTERIES,
    _stage0_token_cap,
)


def compute_pool_hash(subsets: dict[str, list[ItemEnvelope]]) -> str:
    return pool_hash(
        [
            content_hash(envelope)
            for battery in STAGE0_BATTERIES
            for envelope in subsets.get(battery, [])
        ]
    )


# ---------------------------------------------------------------------------
# Dry-run plan
# ---------------------------------------------------------------------------
@dataclass
class CallPlan:
    """Summary of the planned calls."""

    models: list[str]
    battery_item_counts: dict[str, int]
    n_initial: int
    n_max: int
    estimated_tokens: int
    budget_cap: int
    within_budget: bool


def build_call_plan(
    subsets: dict[str, list[ItemEnvelope]],
    models: tuple[str, ...] | None = None,
    n_initial: int = 3,
    budget_cap: int | None = None,
) -> CallPlan:
    battery_counts = {b: len(items) for b, items in subsets.items()}
    if models is None:
        models = fleet_models()
    if budget_cap is None:
        budget_cap = _stage0_token_cap()
    total_items = sum(battery_counts.values())
    est_total_calls = len(models) * total_items * n_initial
    est_tokens = est_total_calls * EST_TOKENS_PER_CALL
    return CallPlan(
        models=list(models),
        battery_item_counts=battery_counts,
        n_initial=n_initial,
        n_max=10,
        estimated_tokens=est_tokens,
        budget_cap=budget_cap,
        within_budget=est_tokens <= budget_cap,
    )


def print_call_plan(plan: CallPlan) -> None:
    print("=== Stage 0 Call Plan ===")
    print(f"Models ({len(plan.models)}):")
    for m in plan.models:
        print(f"  - {m}")
    print("Batteries:")
    for b, count in plan.battery_item_counts.items():
        print(f"  {b}: {count} items")
    total_items = sum(plan.battery_item_counts.values())
    n_calls = len(plan.models) * total_items * plan.n_initial
    print(f"Pilot n={plan.n_initial}, max n={plan.n_max}")
    print(f"Pilot call plan: {len(plan.models)} models × {total_items} items × {plan.n_initial} = {n_calls} calls")
    print(f"Estimated tokens (pilot): {plan.estimated_tokens:,}")
    print(f"Stage-0 budget cap: {plan.budget_cap:,} tokens")
    if plan.within_budget:
        print("✓ Estimated tokens are within cap.")
    else:
        print(f"✗ OVER budget by {plan.estimated_tokens - plan.budget_cap:,} tokens.")
        print("  (Stage 0 runner will halt when the cap is reached.)")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
