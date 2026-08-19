"""Livebench battery registry — the 10 live capability benchmarks (task 12).

Port of v1's benchmark engine onto the unified hr.adapters. Each battery is
registered in the hr2 schema exactly like tool_b (todo 15): hr2.battery row,
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
    BenchmarkCategory.vision: ("image",),
    BenchmarkCategory.speed: ("speed",),
    BenchmarkCategory.long_horizon: (
        "critical_path", "duration", "slack", "action",
    ),
}


def battery_code(battery: BenchmarkCategory) -> str:
    """hr2 ``battery_code`` for a livebench battery (``livebench_<name>``)."""
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


__all__ = [
    "LIVEBENCH_BATTERIES",
    "battery_code",
    "battery_description",
    "battery_item_id",
    "battery_item_labels",
    "seat_battery_bounds",
]