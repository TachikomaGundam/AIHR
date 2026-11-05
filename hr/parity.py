"""Model-generation parity predicates (AIHR charter section III, gate #1).

Answers one question mechanically: are the calibration anchors in
configs/seats.local.yaml still same-generation as the live fleet derived from
opencode config? A generation counts as drifted when the anchor's family
version is no longer present in the fleet while a higher version is (council
ruling sibyl-20261016T083925Z-1ce8 must-fix: provider change or major.minor
increment = one generation; the provider is compared INSIDE the family view,
a mirrored same-version model under another provider keeps the generation
alive). Unparseable ids and un-bumped manifests report flags, never silent
freshness.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from hr.config import load_yaml


@dataclass(frozen=True)
class Gen:
    provider: str
    family: str | None
    version: tuple[int, int] | None
    raw: str


@dataclass(frozen=True)
class DriftEntry:
    anchor_key: str
    anchor: Gen
    successor: Gen
    reason: str


@dataclass(frozen=True)
class ParityReport:
    stale_manifest: str | None
    drifted: tuple[DriftEntry, ...]
    incomparable: tuple[str, ...]
    clean: tuple[str, ...]

    @property
    def seating_stale(self) -> bool:
        return bool(self.drifted) or self.stale_manifest is not None


def classify(model_id: str, families: list[dict[str, str]]) -> Gen:
    provider, _, name = model_id.partition("/")
    for rule in families:
        match = re.match(rule["pattern"], name)
        if match:
            groups = match.groups() or ()
            digits = tuple(g for g in groups if g and g.isdigit())
            version = (int(digits[0]), int(digits[1]) if len(digits) > 1 else 0) \
                if digits else None
            return Gen(provider, rule["family"], version, model_id)
    return Gen(provider, None, None, model_id)


def check_parity(
    anchors: dict[str, str],
    fleet_ids: Iterable[str],
    *,
    today: _dt.date | None = None,
    manifest: dict | None = None,
) -> ParityReport:
    if manifest is None:
        manifest = load_yaml("model_parity.yaml")
    families = list(manifest.get("families", []))
    stale_manifest: str | None = None
    as_of = _dt.date.fromisoformat(str(manifest["as_of"]))
    age = ((today or _dt.date.today()) - as_of).days
    if age > int(manifest.get("stale_days", 90)):
        stale_manifest = (
            f"model_parity.yaml as_of={as_of} age={age}d > stale_days; "
            "operator must bump after provider-generation changes"
        )
    fleet = [classify(m, families) for m in fleet_ids]
    drifted: list[DriftEntry] = []
    incomparable: list[str] = []
    clean: list[str] = []
    for key, anchor_id in anchors.items():
        anchor = classify(anchor_id, families)
        if anchor.family is None:
            incomparable.append(f"{key}:{anchor_id}:unknown-family")
            continue
        same_family = [g for g in fleet if g.family == anchor.family]
        if anchor.version is None:
            incomparable.append(f"{key}:{anchor_id}:generation-uncomparable")
            continue
        if any(g.version == anchor.version for g in same_family):
            clean.append(key)
            continue
        higher = sorted((g for g in same_family
                         if g.version and g.version > anchor.version),
                        key=lambda g: g.version or (0, 0))
        if higher:
            successor = higher[0]
            reason = "version-increment" if any(
                g.provider == anchor.provider for g in higher) else "provider-change"
            drifted.append(DriftEntry(key, anchor, successor, reason))
        elif any(g.version is not None for g in same_family):
            drifted.append(DriftEntry(key, anchor,
                                      next(g for g in same_family
                                           if g.version is not None),
                                      "generation-absent"))
        else:
            incomparable.append(f"{key}:{anchor_id}:generation-uncomparable")
    return ParityReport(stale_manifest, tuple(drifted),
                        tuple(incomparable), tuple(clean))


def resolve_price(pricing: dict[str, float], model_id: str) -> float | None:
    """SINGLE SOURCE of pricing resolution (the same rule `hr recommend`
    consumes and gate #2 certifies — they can never drift apart):
    exact key wins, then the bare slug after the first '/'."""
    price = pricing.get(model_id)
    if price is None and "/" in model_id:
        price = pricing.get(model_id.split("/", 1)[1])
    return float(price) if price is not None else None


@dataclass(frozen=True)
class PricingReport:
    missing: tuple[str, ...]           # fleet ids resolving to NO price
    shadow_conflicts: tuple[str, ...]  # namespaced+bare both present, unequal
    priced: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.missing and not self.shadow_conflicts


def check_pricing_parity(
    fleet_ids: Iterable[str],
    pricing: dict[str, float] | None = None,
) -> PricingReport:
    """Gate #2 (charter: pricing 齐平闸): every model id the active fleet
    surfaces must resolve to a pricing entry in configs/models.yaml — the
    $/1M rail behind `hr recommend`'s cost constraint. Missing = the
    silent-zero failure this gate exists to stop (no price -> no cost
    signal -> the $0.10 rail no-ops and the seat passes on no evidence).
    Entry freshness has no per-row date in models.yaml (knowledge data,
    bare floats); generation currency is certified by gate #1, which
    covers the "非过期" dimension jointly. Empty fleet raises: certifying
    pricing for zero models would be the same silent-pass this law bans."""
    fleet = list(fleet_ids)
    if not fleet:
        raise ValueError("check_pricing_parity: empty fleet — refusing to "
                         "certify pricing for zero models")
    if pricing is None:
        pricing = dict(load_yaml("models.yaml").get("pricing", {}))
    for key, value in pricing.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f"pricing[{key!r}] must be a number — USD per 1M tokens "
                f"(got {type(value).__name__}: {value!r}); check "
                "configs/models.yaml and the .local.yaml overlay layers"
            )
    missing: list[str] = []
    conflicts: list[str] = []
    priced: list[str] = []
    for mid in fleet:
        if resolve_price(pricing, mid) is None:
            missing.append(mid)
            continue
        if "/" in mid:
            bare = mid.split("/", 1)[1]
            if (mid in pricing and bare in pricing
                    and float(pricing[mid]) != float(pricing[bare])):
                conflicts.append(f"{mid}: namespaced={pricing[mid]} "
                                 f"vs bare={pricing[bare]}")
        priced.append(mid)
    return PricingReport(tuple(missing), tuple(conflicts), tuple(priced))


def assert_pricing_parity(
    fleet_ids: Iterable[str],
    pricing: dict[str, float] | None = None,
) -> PricingReport:
    """Fail-loud entry point for the seating/apply chain: raises with the
    full gap list instead of returning a report someone might ignore."""
    rep = check_pricing_parity(fleet_ids, pricing)
    if not rep.ok:
        raise ValueError(
            f"pricing parity gate: missing={list(rep.missing)} "
            f"shadow_conflicts={list(rep.shadow_conflicts)}")
    return rep


@dataclass(frozen=True)
class CeilingReport:
    frozen: tuple[str, ...]        # every measurement == max_score (P5 disease)
    flat: tuple[str, ...]          # single distinct value, not at ceiling
    insufficient: tuple[str, ...]  # too few samples to judge — never silent-clean
    healthy: tuple[str, ...]

    @property
    def no_signal(self) -> tuple[str, ...]:
        return self.frozen + self.flat


def check_tier_ceiling(
    scores_by_battery: Mapping[str, Sequence[float]],
    *,
    min_samples: int = 6,
    max_score: float = 100.0,
) -> CeilingReport:
    """Gate #3 (tier-ceiling): a battery whose recent-window scores are ALL
    at the ceiling measures nothing — the fleet-wide-100 pattern that killed
    P5 is now mechanically flaggable before a release leans on it.
    all==max -> frozen; one distinct value off-ceiling -> flat (grader-broken
    signal); < min_samples -> insufficient (never silently healthy).
    min_samples defaults to 6 = one round across the named pilot roster."""
    if not scores_by_battery:
        raise ValueError("check_tier_ceiling: empty battery map — refusing "
                         "to certify ruler health for zero batteries")
    frozen: list[str] = []
    flat: list[str] = []
    insufficient: list[str] = []
    healthy: list[str] = []
    for battery in sorted(scores_by_battery):
        scores = [float(v) for v in scores_by_battery[battery]]
        if len(scores) < min_samples:
            insufficient.append(f"{battery}:n={len(scores)}<{min_samples}")
        elif all(v == max_score for v in scores):
            frozen.append(battery)
        elif len(set(scores)) < 2:
            flat.append(battery)
        else:
            healthy.append(battery)
    return CeilingReport(tuple(frozen), tuple(flat), tuple(insufficient),
                         tuple(healthy))


def assert_tier_ceiling(
    scores_by_battery: Mapping[str, Sequence[float]],
    *,
    min_samples: int = 6,
    max_score: float = 100.0,
) -> CeilingReport:
    """Fail-loud entry: no-signal batteries raise with full lists — verdict/
    seating consumers must not proceed on a ruler proven mute."""
    rep = check_tier_ceiling(scores_by_battery, min_samples=min_samples,
                             max_score=max_score)
    if rep.no_signal:
        raise ValueError(
            f"tier-ceiling gate: frozen={list(rep.frozen)} flat={list(rep.flat)}")
    return rep
