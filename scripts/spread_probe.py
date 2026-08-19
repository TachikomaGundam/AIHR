"""Spread smoke test: run 3 hard benchmarks on two frontier-ish models.

ONE-OFF LIVE PROBE — not part of any suite: calls live vendor APIs with the
real credentials from the opencode config / auth.json. Only run it on a
machine whose credential store is configured (and billed); it will fail or
hang without network/keys.

Prints scores to confirm deepseek-v3.2 is substantially weaker than
qwen3.6-flash on these harder tasks.
"""
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hr.benchmark import (
    BenchmarkEngine,
    _bench_code_gen,
    _bench_reasoning,
    _bench_tool_use,
)

MODELS = ["qwen3.6-flash", "deepseek-v3.2"]
BENCHES = [
    ("code_gen", _bench_code_gen),
    ("reasoning", _bench_reasoning),
    ("tool_use", _bench_tool_use),
]


def main() -> None:
    with BenchmarkEngine() as engine:
        rows: list[tuple[str, str, float, bool, str]] = []
        for model in MODELS:
            for name, fn in BENCHES:
                print(f"  → {model} / {name} ...", end=" ", flush=True)
                outcome = fn(engine, model)
                print(f"score={outcome.score}, passed={outcome.passed}, raw={outcome.raw_output!r}")
                rows.append((model, name, outcome.score, outcome.passed, outcome.raw_output))

    print("\n=== results table ===")
    print(f"{'model':<20}{'benchmark':<18}{'score':>8}{'passed':>8}")
    for model, name, score, passed, _ in rows:
        print(f"{model:<20}{name:<18}{score:>8.2f}{str(passed):>8}")

    print("\n=== spread check ===")
    by_bench: dict[str, dict[str, float]] = {}
    for model, name, score, _, _ in rows:
        by_bench.setdefault(name, {})[model] = score
    spreads = []
    for bench, per_model in by_bench.items():
        values = list(per_model.values())
        spread = max(values) - min(values)
        spreads.append((bench, spread, per_model))
        print(f"{bench:<18} spread={spread:6.2f}  {per_model}")

    by_model_avg: dict[str, float] = {m: 0.0 for m in MODELS}
    by_model_cnt: dict[str, int] = {m: 0 for m in MODELS}
    for model, _name, score, _, _ in rows:
        by_model_avg[model] += score
        by_model_cnt[model] += 1
    avgs = {m: by_model_avg[m] / max(1, by_model_cnt[m]) for m in MODELS}
    agg_spread = max(avgs.values()) - min(avgs.values())

    print(f"\naverages: {avgs}")
    print(f"aggregate spread: {agg_spread:.2f} points")

    qwen_above_ds = all(
        per_model.get("qwen3.6-flash", 0) >= per_model.get("deepseek-v3.2", 0)
        for _, _, per_model in spreads
    )
    print(f"qwen3.6-flash >= deepseek-v3.2 on every bench? {qwen_above_ds}")

    # Acceptance: aggregate spread >= 20 points AND qwen >= deepseek on every bench.
    # This matches the spec's wording: the SUITE produces spread-out graded scores
    # (deepseek SUBSTANTIALLY lower on aggregate) even if one bench happens to be
    # within reach of the weaker model.
    if agg_spread < 20:
        print(f"FAIL: aggregate spread {agg_spread:.2f} < 20 — suite still saturated.")
        sys.exit(1)
    if not qwen_above_ds:
        print("FAIL: deepseek-v3.2 beat qwen3.6-flash on some bench — ordering wrong.")
        sys.exit(1)
    print(f"OK: suite produces {agg_spread:.0f}-point aggregate spread with correct ordering.")


if __name__ == "__main__":
    main()
