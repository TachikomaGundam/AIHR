"""Stage-0 dry-run call-plan contract tests (committed surface).

Exercises hr.stage0_plan: pool hashing over subset envelopes, the
budgeting math of build_call_plan (explicit and fleet-derived model
lists), and both printed branches of print_call_plan.
"""

from __future__ import annotations

from hr.fleet import fleet_models
from hr.items.schema import ItemType, build_envelope
from hr.stage0_plan import (
    build_call_plan,
    compute_pool_hash,
    print_call_plan,
)
from hr.stage0_selection import EST_TOKENS_PER_CALL, STAGE0_TOKEN_CAP


def make_env(item_key: str, type_: ItemType = ItemType.REASONING) -> object:
    return build_envelope(
        item_key=item_key,
        type=type_,
        payload={},
        grading={"grader": "passthrough@1.0"},
        meta={"seats": ["f1"]},
    )


def subsets_fixture() -> dict[str, list[object]]:
    return {
        "reasoning": [make_env(f"reasoning.{i:03d}") for i in range(20)],
        "tool_a": [make_env(f"tool_a.c.{i:02d}", ItemType.TOOL_A) for i in range(30)],
        "vision": [],
    }


def test_compute_pool_hash_stable_and_sensitive() -> None:
    subs = subsets_fixture()
    h1 = compute_pool_hash(subs)
    h2 = compute_pool_hash(subs)
    assert h1 == h2
    assert isinstance(h1, str) and h1.startswith("sha256:")
    # A content change in any envelope changes the pool hash.
    subs2 = subsets_fixture()
    subs2["reasoning"][0] = make_env("reasoning.modified")
    assert compute_pool_hash(subs2) != h1


def test_build_call_plan_explicit_models_and_budget(monkeypatch) -> None:
    monkeypatch.setattr(
        "hr.stage0_plan._stage0_token_cap", lambda: STAGE0_TOKEN_CAP
    )
    plan = build_call_plan(subsets_fixture(), models=("m1", "m2"), n_initial=3)
    assert plan.models == ["m1", "m2"]
    assert plan.battery_item_counts == {"reasoning": 20, "tool_a": 30, "vision": 0}
    total_items = 50
    expected = len(plan.models) * total_items * 3 * EST_TOKENS_PER_CALL
    assert plan.estimated_tokens == expected
    assert plan.budget_cap == STAGE0_TOKEN_CAP
    assert plan.within_budget is True
    assert plan.n_max == 10  # spec constant
    assert plan.n_initial == 3


def test_build_call_plan_fleet_models_and_over_budget(monkeypatch) -> None:
    monkeypatch.setattr("hr.stage0_plan.fleet_models", lambda: ("a", "b", "c"))
    monkeypatch.setattr("hr.stage0_plan._stage0_token_cap", lambda: 1_000)
    plan = build_call_plan(subsets_fixture(), models=None)
    # fleet-derived models used when models=None.
    assert plan.models == ["a", "b", "c"]
    assert plan.within_budget is False


def test_build_call_plan_budget_cap_explicit() -> None:
    plan = build_call_plan(subsets_fixture(), models=("m",), budget_cap=1)
    assert plan.budget_cap == 1
    assert plan.within_budget is False


def test_print_call_plan_within_budget(capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        "hr.stage0_plan._stage0_token_cap", lambda: STAGE0_TOKEN_CAP
    )
    plan = build_call_plan(subsets_fixture(), models=("rmodel/one",), n_initial=3)
    print_call_plan(plan)
    out = capsys.readouterr().out
    assert "=== Stage 0 Call Plan ===" in out
    assert "rmodel/one" in out
    assert "Models (1):" in out
    assert "reasoning: 20 items" in out
    assert "Pilot n=3, max n=10" in out
    assert "1 models × 50 items × 3 = 150 calls" in out
    assert "Estimated tokens (pilot): 750,000" in out
    assert "✓ Estimated tokens are within cap." in out
    assert "OVER budget" not in out


def test_print_call_plan_over_budget(capsys, monkeypatch) -> None:
    monkeypatch.setattr("hr.stage0_plan._stage0_token_cap", lambda: 1_000)
    plan = build_call_plan(subsets_fixture(), models=("m",), n_initial=3)
    print_call_plan(plan)
    out = capsys.readouterr().out
    assert "✗ OVER budget by 749,000 tokens." in out
    assert "(Stage 0 runner will halt when the cap is reached.)" in out
    assert "✓ Estimated tokens" not in out