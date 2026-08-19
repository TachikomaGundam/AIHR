#!/usr/bin/env python3
"""
truths_verification.py — recompute every item's truth from the reference
implementation and cross-check tier 5/6 items against an independent method.

Recomputes:
  1. expected_value  (from payload.answer_schema.expected_value in each JSON)
  2. computed_truth  (by calling the reference implementation here, freshly)
  3. crosscheck_truth (tier 5/6 only, from a *second* independent impl)

Prints a PASS/FAIL table per item. All rows must PASS.
"""
from __future__ import annotations

import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from generate_items import ITEMS  # each spec exposes ref + xcheck


def main():
    print(f"{'item_key':<45} {'tier':>4} {'computed':>14} {'xcheck':>14} {'embedded':>14}  status")
    print("-" * 115)

    total = 0
    pass_count = 0
    failures = []

    for spec in ITEMS:
        tier = spec["tier"]
        slug = spec["slug"]
        path = ROOT / f"t{tier}" / f"reason.t{tier}.{slug}.json"
        item_key = f"reasoning.t{tier}.{slug}"

        with open(path, "r") as f:
            item = json.load(f)

        embedded = item["payload"]["answer_schema"]["expected_value"]
        computed = spec["ref"]()

        crosscheck = None
        if tier >= 5 and spec.get("xcheck") is not None:
            crosscheck = spec["xcheck"]()

        # Float tolerance: only when spec.tolerance is set (none of ours)
        if spec.get("tolerance") is not None:
            ok_main = abs(computed - embedded) <= spec["tolerance"]
            ok_xcheck = crosscheck is None or abs(computed - crosscheck) <= spec["tolerance"]
        else:
            ok_main = computed == embedded
            ok_xcheck = crosscheck is None or computed == crosscheck

        status = "PASS" if (ok_main and ok_xcheck) else "FAIL"
        if ok_main and ok_xcheck: pass_count += 1
        else: failures.append(item_key)
        total += 1

        xc_str = f"{crosscheck}" if crosscheck is not None else "-"
        print(f"{item_key:<45} t{tier:<3} {computed!s:>14} {xc_str:>14} {embedded!s:>14}  {status}")

    print("-" * 115)
    print(f"Total: {total}  Passed: {pass_count}  Failed: {total - pass_count}")
    if failures:
        print("\nFAILURES:")
        for f in failures: print(f"  • {f}")
        sys.exit(1)
    print("\nAll truths verified.")


if __name__ == "__main__":
    main()
