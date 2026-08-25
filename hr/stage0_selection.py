from __future__ import annotations

from typing import Any, Protocol

from hr.adapters.base import Capabilities
from hr.config import load_yaml
from hr.graders.base import ModelResponse
from hr.items.schema import ItemEnvelope, ItemType


# ---------------------------------------------------------------------------
# Model pool
# ---------------------------------------------------------------------------
# The sweep pool is the model fleet derived at RUNTIME from the opencode
# config + configs/deployable.yaml extra_deployable (hr.fleet.fleet_models,
# resolved at CALL time so tests can redirect OPENCODE_CONFIG_DIR). Model
# names live in the opencode config only.

STAGE0_BATTERIES: tuple[str, ...] = ("reasoning", "hallucination", "tool_a", "vision", "tool_b")

#: Spec §5.4 v0.2 — exact Stage-0 subset sizes.
STAGE0_SUBSET_SIZES: dict[str, int] = {
    "reasoning": 20,
    "hallucination": 25,
    "tool_a": 30,
    "vision": 15,
    "tool_b": 10,
}

#: Item-type grouping used by the item loader (mirrors calibrate.BATTERY_TYPES).
#: Hallucination = factuality_qa + unanswerable + citation.
BATTERY_ITEM_TYPES: dict[str, tuple[ItemType, ...]] = {
    "reasoning": (ItemType.REASONING,),
    "hallucination": (ItemType.FACTUALITY_QA, ItemType.UNANSWERABLE, ItemType.CITATION),
    "tool_a": (ItemType.TOOL_A,),
    "vision": (ItemType.VISION,),
    "tool_b": (ItemType.TOOL_B,),
}

#: Stage 0 budget cap fallback (spec §9.1 v0.3 set 30M, which the 34-model
#: fleet already exceeds: pilot = 34 models x 100 items x n_initial 3 x
#: 5,000 tokens = 51.0M). The OPERATIONAL value lives in
#: configs/thresholds.yaml ``stage0.token_cap`` (60M, ~18% headroom) and is
#: resolved at CALL time via :func:`_stage0_token_cap` so tests can redirect
#: configs; this constant is the offline fallback. Both values moved together.
STAGE0_TOKEN_CAP: int = 60_000_000

#: Estimated tokens per call for dry-run budgeting.
EST_TOKENS_PER_CALL: int = 5_000


def _stage0_token_cap() -> int:
    """Resolve the Stage-0 token cap: ``thresholds.yaml stage0.token_cap`` wins.

    Falls back to STAGE0_TOKEN_CAP when the config is missing or the key is
    absent/non-positive (config-wins pattern, mirrors cli._KNOB_TO_BATTERY).
    """
    try:
        section = load_yaml("thresholds.yaml").get("stage0") or {}
    except FileNotFoundError:
        return STAGE0_TOKEN_CAP
    value = section.get("token_cap") if isinstance(section, dict) else None
    if isinstance(value, int) and value > 0:
        return value
    return STAGE0_TOKEN_CAP

#: Sentinel seat upserted specifically for Stage 0.
STAGE0_SEAT_CODE: str = "_stage0_sweep"


# ---------------------------------------------------------------------------
# Adapter facade compatible with ``hr.adapters.base.Adapter``.
# ---------------------------------------------------------------------------
class _AdapterFacade(Protocol):
    def probe_capabilities(self, model_id: str) -> Capabilities: ...

    def chat(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        images: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        thinking_budget: int | None = None,
        max_output: int = 16384,
        timeout_s: int = 600,
    ) -> ModelResponse: ...


# ---------------------------------------------------------------------------
# Item subset selection
# ---------------------------------------------------------------------------
def _tier_items(items: list[ItemEnvelope]) -> dict[int, list[ItemEnvelope]]:
    """Group items by tier (1-6)."""
    out: dict[int, list[ItemEnvelope]] = {}
    for env in items:
        out.setdefault(env.tier, []).append(env)
    for k, v in out.items():
        v.sort(key=lambda e: e.item_key)
    return out


def select_reasoning_subset(
    items: list[ItemEnvelope], n: int = STAGE0_SUBSET_SIZES["reasoning"]
) -> list[ItemEnvelope]:
    """Pick ``n`` reasoning items, ~3-4 per tier across t1-t6.

    Deterministic: sort within tier by item_key, then round-robin across
    tiers in order, wrapping as needed to hit ``n`` exactly.
    """
    by_tier = _tier_items(items)
    tiers = sorted(by_tier.keys())
    # Target ~n//len(tiers) per tier; remainder spread across early tiers.
    base = n // len(tiers) if tiers else 0
    remainder = n - base * len(tiers)
    selected: list[ItemEnvelope] = []
    for i, tier in enumerate(tiers):
        k = base + (1 if i < remainder else 0)
        selected.extend(by_tier[tier][:k])
    selected.sort(key=lambda e: e.item_key)
    return selected


def select_hallucination_subset(
    items_by_type: dict[ItemType, list[ItemEnvelope]],
    n: int = STAGE0_SUBSET_SIZES["hallucination"],
) -> list[ItemEnvelope]:
    """Pick ``n`` hallucination items — a mix of qa/unanswerable/citation."""
    qa = sorted(items_by_type.get(ItemType.FACTUALITY_QA, []), key=lambda e: e.item_key)
    ua = sorted(items_by_type.get(ItemType.UNANSWERABLE, []), key=lambda e: e.item_key)
    cit = sorted(items_by_type.get(ItemType.CITATION, []), key=lambda e: e.item_key)
    # Split roughly proportionally but ensure all 3 subtypes appear.
    # Target 15 qa + 7 unanswerable + 3 citation = 25.
    qa_n = int(round(n * 0.6))
    ua_n = int(round(n * 0.28))
    cit_n = n - qa_n - ua_n
    out = qa[:qa_n] + ua[:ua_n] + cit[:cit_n]
    out.sort(key=lambda e: e.item_key)
    return out


def select_tool_subset(
    items: list[ItemEnvelope], n: int = STAGE0_SUBSET_SIZES["tool_a"]
) -> list[ItemEnvelope]:
    """Pick ``n`` tool_a items, stratified by sub-kind when possible."""
    by_subkind: dict[str, list[ItemEnvelope]] = {}
    for env in items:
        # tool_a item_keys: "tool_a.<subkind>.<name>"
        parts = env.item_key.split(".", 2)
        subkind = parts[1] if len(parts) >= 2 else "unknown"
        by_subkind.setdefault(subkind, []).append(env)
    for k, v in by_subkind.items():
        v.sort(key=lambda e: e.item_key)
    # Round-robin across sub-kinds then top up.
    subkinds = sorted(by_subkind.keys())
    selected: list[ItemEnvelope] = []
    seen: set[str] = set()
    for _ in range(10):  # up to 10 rounds (enough for any realistic n)
        for sk in subkinds:
            for env in by_subkind[sk]:
                if env.item_key in seen:
                    continue
                selected.append(env)
                seen.add(env.item_key)
                if len(selected) >= n:
                    break
            if len(selected) >= n:
                break
        if len(selected) >= n:
            break
    selected.sort(key=lambda e: e.item_key)
    return selected[:n]


def select_vision_subset(
    items: list[ItemEnvelope], n: int = STAGE0_SUBSET_SIZES["vision"]
) -> list[ItemEnvelope]:
    """Pick ``n`` vision items — 5 per kind (ui_read / chart_extract / schematic).

    Spec §5.4 v0.2: UI/schematic/chart × 5 each = 15. Falls back to
    proportional allocation if kinds are missing.
    """
    kinds = ("ui_read", "chart_extract", "schematic")
    by_kind: dict[str, list[ItemEnvelope]] = {k: [] for k in kinds}
    for env in items:
        # vision item_key like "vision.ui_read.X"
        parts = env.item_key.split(".", 2)
        kind = parts[1] if len(parts) >= 2 else "unknown"
        if kind in by_kind:
            by_kind[kind].append(env)
    for k in kinds:
        by_kind[k].sort(key=lambda e: e.item_key)
    # 5 per kind when available; otherwise take what's there.
    per_kind = n // len(kinds)
    remainder = n - per_kind * len(kinds)
    selected: list[ItemEnvelope] = []
    for i, kind in enumerate(kinds):
        take = per_kind + (1 if i < remainder else 0)
        selected.extend(by_kind[kind][:take])
    # Fill if any kind was short — spread across the rest.
    if len(selected) < n:
        remaining = [
            e for e in items if e.item_key not in {s.item_key for s in selected}
        ]
        remaining.sort(key=lambda e: e.item_key)
        i = 0
        while len(selected) < n and i < len(remaining):
            selected.append(remaining[i])
            i += 1
    selected.sort(key=lambda e: e.item_key)
    return selected


def select_subsets(
    items_by_battery: dict[str, list[ItemEnvelope]],
) -> dict[str, list[ItemEnvelope]]:
    """Apply Stage-0 subset selection to each battery's items.

    For hallucination, ``items_by_battery["hallucination"]`` is a flattened
    list; we must re-group by type.
    """
    out: dict[str, list[ItemEnvelope]] = {}

    # reasoning
    reasoning_items = items_by_battery.get("reasoning", [])
    out["reasoning"] = select_reasoning_subset(reasoning_items)

    # hallucination (mixed types)
    halluc_items_by_type: dict[ItemType, list[ItemEnvelope]] = {}
    for env in items_by_battery.get("hallucination", []):
        halluc_items_by_type.setdefault(env.type, []).append(env)
    out["hallucination"] = select_hallucination_subset(halluc_items_by_type)

    # tool_a
    tool_items = items_by_battery.get("tool_a", [])
    out["tool_a"] = select_tool_subset(tool_items)

    # vision
    vision_items = items_by_battery.get("vision", [])
    out["vision"] = select_vision_subset(vision_items)

    # tool_b
    tool_b_items = items_by_battery.get("tool_b", [])
    out["tool_b"] = select_tool_subset(tool_b_items)

    return out


# ---------------------------------------------------------------------------
# Adapter wiring helpers
# ---------------------------------------------------------------------------
