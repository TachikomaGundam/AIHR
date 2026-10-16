"""Livebench battery registry — the 10 live capability benchmarks (task 12).

Port of v1's benchmark engine onto the unified hr.adapters. Each battery is
registered in the HR schema exactly like tool_b: battery row,
one item_pool row per graded unit, battery_item links, and a seat_battery
link under the ``_stage0_sweep`` pseudo-seat.

Item counts are the natural graded units of each v1 scorer (semantics kept
intact — the battery mean over the item scores equals the v1 battery score):

  - code_gen          13 hidden tests (8 median + 3 burst + 1 inversion + 1 perf gate)
  - reasoning         13 runtime-truth questions
  - instruction_follow 16 independent constraints
  - tool_use           1 multi-turn calculate loop (target 105.63)
  - long_context       3 needles (alpha/bravo/charlie); the 3 decoys stay
                       informational (a trap note), exactly as in v1
  - attention_probe    8 binary probes (5-band UUID needle position sweep,
                       associative retrieval pair, distractor resistance)
  - attention_stress   4 checkpoints (5-constraint survival over a 20-turn
                       scripted conversation)
  - vision             1 hand-made PNG (2x2 colored squares)
  - speed              1 run scored into tok/s tiers (30..90)
  - long_horizon       4 scorer components (critical path / duration / slack / action)
                       over the 6-task CPM graph ("6 tasks" is benchmark-
                       internal structure, not item count)

seat_battery bounds are chosen honestly: n_initial = min(3, n_items) and
n_max = min(10, n_items) — never more rounds than the battery can measure.
"""

from __future__ import annotations

from hr.models import BenchmarkCategory

#: The 10 livebench batteries, in v1 registry order.
LIVEBENCH_BATTERIES: tuple[BenchmarkCategory, ...] = (
    BenchmarkCategory.code_gen,
    BenchmarkCategory.reasoning,
    BenchmarkCategory.instruction_follow,
    BenchmarkCategory.tool_use,
    BenchmarkCategory.long_context,
    BenchmarkCategory.attention_probe,
    BenchmarkCategory.attention_stress,
    BenchmarkCategory.vision,
    BenchmarkCategory.speed,
    BenchmarkCategory.long_horizon,
    BenchmarkCategory.factuality,
)

_BATTERY_DESCRIPTIONS: dict[BenchmarkCategory, str] = {
    BenchmarkCategory.code_gen: (
        "13 hidden Python tests: sliding_window_median (8), burst_balloons (3), "
        "count_inversions (1) + O(n log n) performance gate (1, SIGALRM 8s)"
    ),
    BenchmarkCategory.reasoning: (
        "13 runtime-truth math/number-theory questions (computed in-process)"
    ),
    BenchmarkCategory.instruction_follow: (
        "16 independent constraints on a clock-tower JSON response"
    ),
    BenchmarkCategory.tool_use: (
        "multi-turn calculate-tool loop; answer must match 105.63"
    ),
    BenchmarkCategory.long_context: (
        "3 needles + 3 decoys inside a ~240K-char haystack"
    ),
    BenchmarkCategory.attention_probe: (
        "8 probes: 5-band UUID needle position sweep + associative retrieval "
        "pair + distractor resistance"
    ),
    BenchmarkCategory.attention_stress: (
        "4 checkpoints: 5-constraint survival over a 20-turn scripted conversation"
    ),
    BenchmarkCategory.vision: (
        "hand-made 180x180 PNG with 4 colored squares (count/colors/positions)"
    ),
    BenchmarkCategory.speed: "tokens/sec scored into tiers (30..90)",
    BenchmarkCategory.factuality: (
        "8 context-grounded cards: key-fact recall + unanswerable-trap fabrication check"
    ),
    BenchmarkCategory.long_horizon: (
        "critical-path-method plan over a 6-task project graph, 4 components"
    ),
}

_ITEM_LABELS: dict[BenchmarkCategory, tuple[str, ...]] = {
    BenchmarkCategory.code_gen: tuple(f"test.{i:02d}" for i in range(13)),
    BenchmarkCategory.reasoning: tuple(f"q{i}" for i in range(1, 14)),
    BenchmarkCategory.instruction_follow: tuple(f"c{i}" for i in range(1, 17)),
    BenchmarkCategory.tool_use: ("total",),
    BenchmarkCategory.long_context: ("alpha", "bravo", "charlie"),
    BenchmarkCategory.attention_probe: (
        "pos_head", "pos_mid_early", "pos_mid", "pos_mid_late", "pos_tail",
        "assoc_literal", "assoc_infer", "decoy_resist",
    ),
    BenchmarkCategory.attention_stress: (
        "survive_t5", "survive_t10", "survive_t15", "survive_t20",
    ),
    BenchmarkCategory.factuality: tuple(f"f{i:02d}" for i in range(8)),
    BenchmarkCategory.vision: ("image",),
    BenchmarkCategory.speed: ("speed",),
    BenchmarkCategory.factuality: tuple(f"f{i:02d}" for i in range(8)),
    BenchmarkCategory.long_horizon: (
        "critical_path", "duration", "slack", "action",
    ),
}


#: Charter gate #5: every battery declares the seats whose routing decisions
#: its score feeds. A battery that informs no seat measures nothing (the P5
#: retirement lesson). Completeness is checked at import (in-memory only);
#: validity of the codes against configs/seats.yaml is checked by the guard
#: test, so package import never couples to operator config files.
BATTERY_SEAT_CODES: dict[BenchmarkCategory, tuple[str, ...]] = {
    BenchmarkCategory.code_gen: ("quick", "sisyphus_junior"),
    BenchmarkCategory.reasoning: ("oracle", "ultrabrain", "metis"),
    BenchmarkCategory.instruction_follow:
        ("quick", "sisyphus_junior", "atlas", "writing"),
    BenchmarkCategory.tool_use: ("deep", "sisyphus_junior", "atlas"),
    BenchmarkCategory.long_context: ("deep", "ultrabrain"),
    BenchmarkCategory.attention_probe: ("deep", "ultrabrain"),
    BenchmarkCategory.attention_stress: ("deep", "sisyphus_junior"),
    BenchmarkCategory.vision: ("visual_engineering", "multimodal_looker", "artistry"),
    BenchmarkCategory.speed: ("quick", "atlas", "explore", "librarian"),
    BenchmarkCategory.long_horizon: ("prometheus", "metis", "ultrabrain", "deep"),
    BenchmarkCategory.factuality: ("librarian", "oracle", "writing", "momus"),
}


def battery_seat_codes(battery: BenchmarkCategory) -> tuple[str, ...]:
    """Seats this battery's score feeds (declared, import-validated)."""
    return BATTERY_SEAT_CODES[battery]


def validate_seat_declarations(
    declarations: dict[BenchmarkCategory, tuple[str, ...]] | None = None,
    batteries: tuple[BenchmarkCategory, ...] | None = None,
) -> None:
    """Fail loud when a registered battery informs no seat."""
    decl = BATTERY_SEAT_CODES if declarations is None else declarations
    reg = LIVEBENCH_BATTERIES if batteries is None else batteries
    missing = sorted(b.value for b in reg if not decl.get(b))
    if missing:
        raise ValueError(
            "battery(ies) missing seat_code declaration — a battery that "
            f"informs no seat measures nothing: {missing}"
        )


validate_seat_declarations()


def battery_code(battery: BenchmarkCategory) -> str:
    """Database battery code for a livebench battery."""
    return f"livebench_{battery.value}"


def battery_description(battery: BenchmarkCategory) -> str:
    return _BATTERY_DESCRIPTIONS[battery]


def battery_item_labels(battery: BenchmarkCategory) -> list[str]:
    """Stable per-item labels, in scorer order (also used for registration)."""
    return list(_ITEM_LABELS[battery])


def battery_item_id(battery: BenchmarkCategory, label: str) -> str:
    return f"livebench.{battery.value}.{label}"


def seat_battery_bounds(battery: BenchmarkCategory) -> tuple[int, int]:
    """Honest (n_initial, n_max) for seat_battery: never exceed item count."""
    n_items = len(_ITEM_LABELS[battery])
    return min(3, n_items), min(10, n_items)


def battery_item_stratum(battery: BenchmarkCategory, label: str) -> str:
    """Stratum an item label belongs to (difficulty/characteristic group).

    ``code_gen`` items group by source problem (8 median tests, 3 burst
    balloons, 1 inversion, 1 performance gate); every other battery is its
    own single stratum.
    """
    if battery is not BenchmarkCategory.code_gen:
        return battery.value
    idx = int(label.split(".", 1)[1])
    if idx <= 7:
        return "sliding_window_median"
    if idx <= 10:
        return "burst_balloons"
    if idx == 11:
        return "count_inversions"
    return "perf_gate"


def battery_strata(battery: BenchmarkCategory) -> dict[str, tuple[str, ...]]:
    """Group the battery's item labels into their strata, in label order."""
    groups: dict[str, list[str]] = {}
    for label in battery_item_labels(battery):
        groups.setdefault(battery_item_stratum(battery, label), []).append(label)
    return {stratum: tuple(labels) for stratum, labels in groups.items()}


__all__ = [
    "BATTERY_SEAT_CODES",
    "LIVEBENCH_BATTERIES",
    "battery_code",
    "battery_seat_codes",
    "validate_seat_declarations",
    "battery_description",
    "battery_item_id",
    "battery_item_labels",
    "battery_item_stratum",
    "battery_strata",
    "seat_battery_bounds",
]
