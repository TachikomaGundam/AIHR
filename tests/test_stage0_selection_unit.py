"""Stage-0 subset selection contract tests against the COMMITTED surface.

Exercises hr.stage0_selection pure functions on the committed shape:
tier grouping, per-battery deterministic subset selection, and the
config-wins token cap resolution. Offline, deterministic, no DB/API.
"""

from __future__ import annotations

import pytest

from hr.items.schema import ItemType, build_envelope
from hr.stage0_selection import (
    BATTERY_ITEM_TYPES,
    STAGE0_BATTERIES,
    STAGE0_SEAT_CODE,
    STAGE0_SUBSET_SIZES,
    STAGE0_TOKEN_CAP,
    _stage0_token_cap,
    _tier_items,
    select_hallucination_subset,
    select_reasoning_subset,
    select_subsets,
    select_tool_subset,
    select_vision_subset,
)


def make_env(item_key: str, type_: ItemType, tier: int = 1) -> object:
    return build_envelope(
        item_key=item_key,
        type=type_,
        payload={},
        grading={"grader": "passthrough@1.0"},
        meta={"seats": ["f1"]},
        tier=tier,
    )


def test_constants_are_spec_frozen() -> None:
    assert STAGE0_BATTERIES == (
        "reasoning",
        "hallucination",
        "tool_a",
        "vision",
        "tool_b",
    )
    assert STAGE0_SUBSET_SIZES == {
        "reasoning": 20,
        "hallucination": 25,
        "tool_a": 30,
        "vision": 15,
        "tool_b": 10,
    }
    assert STAGE0_TOKEN_CAP == 60_000_000
    assert STAGE0_SEAT_CODE == "_stage0_sweep"
    assert BATTERY_ITEM_TYPES["hallucination"] == (
        ItemType.FACTUALITY_QA,
        ItemType.UNANSWERABLE,
        ItemType.CITATION,
    )


def test_tier_items_groups_and_sorts() -> None:
    items = [
        make_env("reasoning.b2", ItemType.REASONING, tier=2),
        make_env("reasoning.a1", ItemType.REASONING, tier=1),
        make_env("reasoning.c3", ItemType.REASONING, tier=2),
        make_env("reasoning.z9", ItemType.REASONING, tier=6),
    ]
    grouped = _tier_items(items)
    assert sorted(grouped.keys()) == [1, 2, 6]
    assert [e.item_key for e in grouped[1]] == ["reasoning.a1"]
    # Within-tier sort by item_key.
    assert [e.item_key for e in grouped[2]] == ["reasoning.b2", "reasoning.c3"]


def test_select_reasoning_subset_spread_and_remainder() -> None:
    items = [
        make_env(f"reasoning.t{tier}.{i:02d}", ItemType.REASONING, tier=tier)
        for tier in range(1, 7)
        for i in range(10)
    ]
    selected = select_reasoning_subset(items, n=20)
    assert len(selected) == 20
    # Deterministic: sorted output, first tiers absorb the remainder.
    keys = [e.item_key for e in selected]
    assert keys == sorted(keys)
    from collections import Counter

    tiers = Counter(e.tier for e in selected)
    assert tiers[1] == 4  # base 3 + remainder 1
    assert tiers[2] == 4
    assert tiers[3] == 3
    assert tiers[6] == 3


def test_select_reasoning_subset_hits_cap_when_short() -> None:
    items = [make_env("reasoning.only", ItemType.REASONING, tier=1)]
    selected = select_reasoning_subset(items, n=20)
    assert [e.item_key for e in selected] == ["reasoning.only"]


def test_select_hallucination_subset_proportional_mix() -> None:
    by_type = {
        ItemType.FACTUALITY_QA: [
            make_env(f"hallucination.qa.{i:02d}", ItemType.FACTUALITY_QA) for i in range(20)
        ],
        ItemType.UNANSWERABLE: [
            make_env(f"hallucination.ua.{i:02d}", ItemType.UNANSWERABLE) for i in range(10)
        ],
        ItemType.CITATION: [
            make_env(f"hallucination.cit.{i:02d}", ItemType.CITATION) for i in range(5)
        ],
    }
    selected = select_hallucination_subset(by_type, n=25)
    assert len(selected) == 25
    from collections import Counter

    counts = Counter(e.type for e in selected)
    assert counts[ItemType.FACTUALITY_QA] == 15  # round(25*0.6)
    assert counts[ItemType.UNANSWERABLE] == 7  # round(25*0.28)
    assert counts[ItemType.CITATION] == 3  # remainder
    assert [e.item_key for e in selected] == sorted(e.item_key for e in selected)


def test_select_hallucination_subset_empty() -> None:
    assert select_hallucination_subset({}, n=25) == []


def test_select_tool_subset_round_robin_and_cap() -> None:
    items = [
        make_env(f"tool_a.calc.{i:02d}", ItemType.TOOL_A) for i in range(4)
    ] + [
        make_env(f"tool_a.bash.{i:02d}", ItemType.TOOL_A) for i in range(4)
    ] + [
        make_env(f"tool_a.repl.{i:02d}", ItemType.TOOL_A) for i in range(4)
    ]
    selected = select_tool_subset(items, n=10)
    assert len(selected) == 10
    assert [e.item_key for e in selected] == sorted(e.item_key for e in selected)
    # All three subkinds appear within the first round-robin pass (n=10 >= 12 items).
    subkinds = {e.item_key.split(".")[1] for e in selected}
    assert subkinds == {"calc", "bash", "repl"}
    # Cap below the item count.
    capped = select_tool_subset(items, n=2)
    assert len(capped) == 2

def test_select_vision_subset_five_per_kind_then_fill() -> None:
    items = [
        make_env(f"vision.ui_read.{i:02d}", ItemType.VISION) for i in range(6)
    ] + [
        make_env(f"vision.chart_extract.{i:02d}", ItemType.VISION) for i in range(6)
    ] + [
        make_env(f"vision.schematic.{i:02d}", ItemType.VISION) for i in range(6)
    ]
    selected = select_vision_subset(items, n=15)
    assert len(selected) == 15
    from collections import Counter

    kinds = Counter(e.item_key.split(".")[1] for e in selected)
    assert kinds == {"ui_read": 5, "chart_extract": 5, "schematic": 5}
    # Short-kind fill: 2 per kind available, n=15 -> fill from everything.
    scarce = [
        make_env(f"vision.ui_read.{i:02d}", ItemType.VISION) for i in range(2)
    ] + [
        make_env(f"vision.chart_extract.{i:02d}", ItemType.VISION) for i in range(2)
    ] + [
        make_env(f"vision.schematic.{i:02d}", ItemType.VISION) for i in range(2)
    ]
    filled = select_vision_subset(scarce, n=15)
    assert len(filled) == len(scarce)  # fill is bounded by available items


def test_select_subsets_full_pipeline() -> None:
    by_battery = {
        "reasoning": [make_env(f"reasoning.{i:03d}", ItemType.REASONING) for i in range(40)],
        "hallucination": [
            make_env(f"hallucination.qa.{i:02d}", ItemType.FACTUALITY_QA) for i in range(30)
        ]
        + [
            make_env(f"hallucination.ua.{i:02d}", ItemType.UNANSWERABLE) for i in range(15)
        ]
        + [make_env(f"hallucination.cit.{i:02d}", ItemType.CITATION) for i in range(8)],
        "tool_a": [make_env(f"tool_a.calc.{i:02d}", ItemType.TOOL_A) for i in range(35)],
        "vision": [
            make_env(f"vision.ui_read.{i:02d}", ItemType.VISION) for i in range(20)
        ]
        + [
            make_env(f"vision.chart_extract.{i:02d}", ItemType.VISION) for i in range(20)
        ],
        "tool_b": [make_env(f"tool_b.r1.{i:02d}", ItemType.TOOL_B) for i in range(12)],
    }
    out = select_subsets(by_battery)
    assert set(out.keys()) == set(STAGE0_BATTERIES)
    assert len(out["reasoning"]) == STAGE0_SUBSET_SIZES["reasoning"]
    assert len(out["hallucination"]) == STAGE0_SUBSET_SIZES["hallucination"]
    assert len(out["tool_a"]) == STAGE0_SUBSET_SIZES["tool_a"]
    assert len(out["vision"]) == STAGE0_SUBSET_SIZES["vision"]
    assert len(out["tool_b"]) == len(by_battery["tool_b"])


def test_select_subsets_missing_batteries_are_empty() -> None:
    out = select_subsets({})
    assert out == {"reasoning": [], "hallucination": [], "tool_a": [], "vision": [], "tool_b": []}


@pytest.mark.parametrize(
    ("config_value", "expected"),
    [
        ({"token_cap": 12_345_678}, 12_345_678),  # config wins
        ({"token_cap": 0}, STAGE0_TOKEN_CAP),  # non-positive ignored
        ({"token_cap": -1}, STAGE0_TOKEN_CAP),
        ({}, STAGE0_TOKEN_CAP),  # missing section
    ],
)
def test_stage0_token_cap_config_wins(monkeypatch, config_value: dict, expected: int) -> None:
    monkeypatch.setattr(
        "hr.stage0_selection.load_yaml",
        lambda _name: {"stage0": config_value},
    )
    assert _stage0_token_cap() == expected


def test_stage0_token_cap_falls_back_when_config_missing(monkeypatch) -> None:
    def _raise(_name: str):
        raise FileNotFoundError("no configs")

    monkeypatch.setattr("hr.stage0_selection.load_yaml", _raise)
    assert _stage0_token_cap() == STAGE0_TOKEN_CAP