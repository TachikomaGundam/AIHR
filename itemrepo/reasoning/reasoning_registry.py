from __future__ import annotations

import json
from pathlib import Path

from reasoning_register_basic import register_items as register_basic_items
from reasoning_register_t5 import register_items as register_t5_items
from reasoning_register_t6 import register_items as register_t6_items
from reasoning_registry_core import ITEMS

ROOT = Path(__file__).resolve().parent
register_basic_items()
register_t5_items()
register_t6_items()

ITEMS[44]["canary"] = True

ITEMS[55]["canary"] = True

def grader_checks(answer_kind, truth, checkpoints):
    checks = []
    if answer_kind == "numeric":
        checks.append({"kind": "numeric_eq", "value": truth})
    for cp in checkpoints:
        checks.append({"kind": "contains_all", "phrases": [cp]})
    return checks

def build_item(spec, truth, *, crosscheck_truth=None):
    item_key = f"reasoning.t{spec['tier']}.{spec['slug']}"
    answer_schema = {"kind": spec["answer_kind"], "expected_value": truth}
    if spec.get("tolerance") is not None: answer_schema["tolerance"] = spec["tolerance"]
    if crosscheck_truth is not None: answer_schema["crosscheck_value"] = crosscheck_truth
    payload = {"question": spec["question"], "answer_schema": answer_schema,
               "checkpoints": spec["checkpoints"],
               "multi_step_state": spec["multi_step_state"] or (spec["tier"] >= 4)}
    checks = grader_checks(spec["answer_kind"], truth, spec["checkpoints"])
    grading = {"grader": "constraint@1.0", "params": {"checks": checks}}
    meta = {"source": "handcrafted", "generated_by": "hr-itemgen-b1@0.1",
            "contamination_guard": "no-model-derived-truth-handcrafted-only",
            "seats": spec["seats"]}
    if spec.get("canary"): meta["canary_candidate"] = True
    meta.update(spec.get("extra_meta") or {})
    return {"item_key": item_key, "type": "reasoning", "tier": spec["tier"],
            "payload": payload, "grading": grading, "meta": meta}

def write_all():
    per_tier = {t: [] for t in range(1, 7)}
    for spec in ITEMS:
        truth = spec["ref"]()
        xcheck = spec["xcheck"]() if spec.get("xcheck") else None
        per_tier[spec["tier"]].append((spec, truth, xcheck))
    for t in range(1, 7):
        (ROOT / f"t{t}").mkdir(parents=True, exist_ok=True)
        for spec, truth, xcheck in per_tier[t]:
            item = build_item(spec, truth, crosscheck_truth=xcheck)
            path = ROOT / f"t{t}" / f"reason.t{t}.{spec['slug']}.json"
            path.write_text(json.dumps(item, ensure_ascii=False, indent=2))
    print(f"Wrote {len(ITEMS)} items:")
    for t in range(1, 7): print(f"  t{t}: {len(per_tier[t])} items")
    print(f"  canaries: {sum(1 for s in ITEMS if s.get('canary'))}")

def registry_slugs(tier: int, items=None):
    """Sorted slugs registered for a tier (one entry per distinct slug)."""
    if items is None:
        items = ITEMS
    return sorted({spec["slug"] for spec in items if spec["tier"] == tier})

def disk_slugs(tier: int, root=None):
    """Sorted slugs of on-disk items ``reason.t{<tier>}.<slug>.json``."""
    base = ROOT if root is None else Path(root)
    prefix = f"reason.t{tier}."
    return sorted(
        p.name[len(prefix):-len(".json")]
        for p in (base / f"t{tier}").glob(f"{prefix}*.json")
    )

def validate_registry_vs_disk(root=None, tiers=(3, 4)):
    """One-to-one registry-vs-disk completeness per tier.

    Returns per tier: registry_only (slugs in the registry with no
    on-disk item), disk_only (item files with no registry entry), and
    duplicate counts. A tier is complete iff the first four values are
    empty/zero.
    """
    result = {}
    for tier in tiers:
        reg = registry_slugs(tier)
        disk = disk_slugs(tier, root)
        result[tier] = {
            "registry_count": len(reg),
            "disk_count": len(disk),
            "registry_only": sorted(set(reg) - set(disk)),
            "disk_only": sorted(set(disk) - set(reg)),
            "duplicate_registry_slugs": sorted(
                {s for s in reg if reg.count(s) > 1}
            ),
            "duplicate_disk_slugs": sorted(
                {s for s in disk if disk.count(s) > 1}
            ),
        }
    return result
