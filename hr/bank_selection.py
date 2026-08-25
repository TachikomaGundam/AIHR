"""Contamination-aware bank selection: state, allocation, disclosure.

Pure selection domain for benchmark banks (no SQL — rows arrive as
`(item_id, stratum, state, total_exposures, unique_models, last_exposed)`
tuples from :mod:`hr.benchmark_banks`).

Selection contract:
- only ``active`` items are eligible; holdout/retired/flagged items and
  overexposed items (``total_exposures >= max_exposures``) are disclosed
  as exclusions, never drawn
- the draw is a seeded, stratified, proportional sample (largest-remainder
  allocation across strata), deterministic for a fixed policy
- every result carries full traceability (bank version, stratum, exposure,
  contamination risk) and a machine-readable contamination disclosure
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ItemState(StrEnum):
    """Governance state of a benchmark item.

    Only ``ACTIVE`` items may enter normal evaluation.
    """

    ACTIVE = "active"
    HOLDOUT = "holdout"
    RETIRED = "retired"
    FLAGGED = "flagged"


_STATE_REASONS: dict[ItemState, str] = {
    ItemState.HOLDOUT: "holdout item — reserved for contamination-resistant evaluation",
    ItemState.RETIRED: "retired from the active pool",
    ItemState.FLAGGED: "contamination-flagged — manual review required before evaluation",
}


@dataclass(frozen=True)
class ItemExclusion:
    """Why an item was kept out of a selection."""

    item_id: str
    reason: str  # one of the ItemState values, or "overexposed"
    detail: str


@dataclass(frozen=True)
class SelectedBankItem:
    """One drawn item with full traceability back to the bank."""

    item_id: str
    bank_version: str
    stratum: str
    exposure_count: int
    unique_models_exposed: int
    last_exposed_at: str | None
    contamination_risk: float
    is_safe: bool
    state: ItemState


@dataclass(frozen=True)
class BankSelectionPolicy:
    """Selection knobs; defaults are the revisable site-wide policy."""

    max_exposures: int = 100
    seed: int = 42
    version: str = "latest"
    stratum_key: str = "difficulty"


@dataclass(frozen=True)
class BankSelectionResult:
    """A full selection with contamination disclosure.

    ``contamination_method`` and ``caveats`` are the machine-readable
    disclosure contract every report ships.
    """

    bank_code: str
    bank_version: str
    seed: int
    requested_count: int
    strategy: str
    contamination_method: list[str]
    selected: list[SelectedBankItem]
    exclusions: list[ItemExclusion]
    caveats: list[str]

    def to_report_dict(self) -> dict[str, Any]:
        return {
            "bank_code": self.bank_code,
            "bank_version": self.bank_version,
            "seed": self.seed,
            "requested_count": self.requested_count,
            "strategy": self.strategy,
            "contamination_method": list(self.contamination_method),
            "exclusions": [
                {"item_id": e.item_id, "reason": e.reason, "detail": e.detail}
                for e in self.exclusions
            ],
            "caveats": list(self.caveats),
            "selected": [
                {
                    "item_id": it.item_id,
                    "bank_version": it.bank_version,
                    "stratum": it.stratum,
                    "exposure_count": it.exposure_count,
                    "unique_models_exposed": it.unique_models_exposed,
                    "last_exposed_at": it.last_exposed_at,
                    "contamination_risk": it.contamination_risk,
                    "is_safe": it.is_safe,
                    "state": it.state.value,
                }
                for it in self.selected
            ],
        }


def build_selection(
    rows: list[tuple],
    *,
    bank_code: str,
    bank_version: str,
    count: int,
    policy: BankSelectionPolicy,
) -> BankSelectionResult:
    """Turn raw bank rows into a governed, traceable selection.

    Items are excluded by state (holdout/retired/flagged) or by exposure
    (``total_exposures >= policy.max_exposures``); the remainder is drawn
    proportionally per stratum with the policy's RNG seed.
    """
    info: dict[str, tuple[str, int, int, Any]] = {}
    eligible: dict[str, list[str]] = {}
    exclusions: list[ItemExclusion] = []
    for item_id, stratum, state_raw, total, uniq, last in rows:
        state = ItemState(state_raw)
        info[item_id] = (stratum, total, uniq, last)
        if state is not ItemState.ACTIVE:
            exclusions.append(
                ItemExclusion(item_id, state.value, _STATE_REASONS[state])
            )
            continue
        if total >= policy.max_exposures:
            exclusions.append(
                ItemExclusion(
                    item_id,
                    "overexposed",
                    f"{total} exposures >= max_exposures {policy.max_exposures}",
                )
            )
            continue
        eligible.setdefault(stratum, []).append(item_id)

    picked = _stratified_select(eligible, count, policy.seed)

    selected = [
        SelectedBankItem(
            item_id=item_id,
            bank_version=bank_version,
            stratum=info[item_id][0],
            exposure_count=info[item_id][1],
            unique_models_exposed=info[item_id][2],
            last_exposed_at=str(info[item_id][3]) if info[item_id][3] else None,
            contamination_risk=min(1.0, info[item_id][1] / 100.0),
            is_safe=info[item_id][1] < policy.max_exposures,
            state=ItemState.ACTIVE,
        )
        for item_id in picked
    ]

    caveats: list[str] = []
    if len(selected) < count:
        caveats.append(
            f"underfill: requested {count} items, only {len(selected)} "
            f"eligible (active, under exposure cap) available"
        )

    contamination_method = [
        f"versioned bank {bank_version}",
        "state governance: active/holdout/retired/flagged",
        f"exposure cap: {policy.max_exposures}+ prior exposures excluded",
        f"stratified proportional draw across {len(eligible)} strata",
    ]

    return BankSelectionResult(
        bank_code=bank_code,
        bank_version=bank_version,
        seed=policy.seed,
        requested_count=count,
        strategy="stratified-proportional",
        contamination_method=contamination_method,
        selected=selected,
        exclusions=exclusions,
        caveats=caveats,
    )


def _stratified_select(
    eligible: dict[str, list[str]], count: int, seed: int
) -> list[str]:
    """Proportional largest-remainder allocation, seeded per draw."""
    sizes = {stratum: len(ids) for stratum, ids in eligible.items()}
    if count <= 0 or not sizes:
        return []
    total = sum(sizes.values())
    if count >= total:
        return sorted(item for ids in eligible.values() for item in ids)

    alloc = {stratum: count * n // total for stratum, n in sizes.items()}
    remaining = count - sum(alloc.values())
    if remaining:
        remainders = {
            stratum: (count * n) % total for stratum, n in sizes.items()
        }
        for stratum in sorted(sizes, key=lambda s: (-remainders[s], s)):
            if remaining == 0:
                break
            room = sizes[stratum] - alloc[stratum]
            take = min(remaining, room)
            alloc[stratum] += take
            remaining -= take
    if remaining:
        for stratum in sorted(sizes):
            if remaining == 0:
                break
            room = sizes[stratum] - alloc[stratum]
            take = min(remaining, room)
            alloc[stratum] += take
            remaining -= take

    rng = random.Random(seed)
    picked: list[str] = []
    for stratum in sorted(sizes):
        picked.extend(rng.sample(sorted(eligible[stratum]), alloc[stratum]))
    return sorted(picked)


__all__ = [
    "BankSelectionPolicy",
    "BankSelectionResult",
    "ItemExclusion",
    "ItemState",
    "SelectedBankItem",
    "build_selection",
]