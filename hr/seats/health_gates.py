"""hr2.seats.health_gates — behavioral-health gates per seat level.

Wires the zero-cost health metrics (``hr2.health``, mined from already-run
sweep measurements — no new API calls) into seat assignment:

  * each seat belongs to a ``GateLevel`` (strict / moderate / lenient);
  * a seat's gate thresholds are enforced on a candidate's ``HealthReport``
    while ranking;
  * ``health_rank_score`` is a tie-break only — it must never overturn a
    clear capability lead (see ranker docstring).

Methodology rule encoded here: capability leads decide assignment; health
acts as a per-role *gate* (strict for decision-facing seats like oracle /
writing, lenient for high-frequency seats like explore / quick) and breaks
ties only. A 0.03 loop difference never overturns a clear capability lead.

Semantics:
  * a threshold of ``None`` on a level means "not enforced for this level";
    that check is skipped entirely.
  * a *metric* of ``None`` in a ``HealthReport`` means "not measured" —
    the check is skipped and an informational note is recorded; a model is
    NEVER failed on missing data.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from ..health import HealthReport
from .rolespec import SEAT_CODES

GateLevel = Literal["strict", "moderate", "lenient"]


class HealthThresholds(BaseModel):
    """Per-level thresholds on HealthReport metrics.

    Mirror of the rolespec.py style: extra fields are rejected so a typo'd
    threshold name fails loudly at import time.
    """

    model_config = ConfigDict(extra="forbid")

    loop_mean_max: float | None = None   # max allowed mean loop score
    truncation_max: float | None = None  # max allowed truncation rate
    unanimity_min: float | None = None   # min allowed unanimity %
    completion_min: float | None = None  # min allowed answer completion rate


GATES: dict[GateLevel, HealthThresholds] = {
    "strict": HealthThresholds(
        loop_mean_max=0.10, truncation_max=0.05,
        unanimity_min=0.90, completion_min=0.80
    ),
    "moderate": HealthThresholds(
        loop_mean_max=0.15, truncation_max=0.08,
        unanimity_min=None, completion_min=0.70
    ),
    "lenient": HealthThresholds(
        loop_mean_max=0.20, truncation_max=0.10,
        unanimity_min=None, completion_min=None
    ),
}

# Every seat in SEAT_CODES maps to exactly one gate level.
#   strict   — decision-facing / judgment seats: low tolerance for looping
#              output, truncation, or disagreement.
#   moderate — working seats with real failure cost but less finality.
#   lenient  — high-frequency / low-stakes seats: only gross health failures
#              should block.
SEAT_HEALTH_GATE: dict[str, GateLevel] = {
    # strict
    "oracle": "strict",
    "ultrabrain": "strict",
    "metis": "strict",
    "momus": "strict",
    "writing": "strict",
    "librarian": "strict",
    "prometheus": "strict",
    # moderate
    "deep": "moderate",
    "unspecified_high": "moderate",
    "visual_engineering": "moderate",
    "artistry": "moderate",
    "multimodal_looker": "moderate",
    "hephaestus": "moderate",
    "sisyphus_junior": "moderate",
    # lenient
    "explore": "lenient",
    "quick": "lenient",
    "atlas": "lenient",
    "unspecified_low": "lenient",
}

# All SEAT_CODES covered? (defensive — kept in sync by test_health_gates.py)
assert set(SEAT_HEALTH_GATE) == set(SEAT_CODES), (
    "SEAT_HEALTH_GATE must cover every seat in SEAT_CODES"
)

# Per-role weights for weighted health scoring (TIER 2).
# Each weight represents penalty contribution to health_rank_score.
# Based on industry research: HELM, OpenCompass, AgentCARD, Harness Effect,
# PraisonAI, Anthropic evals, CrewAI, Automatos.
ROLE_HEALTH_WEIGHTS: dict[str, dict[str, float]] = {
    # Strict seats: reasoning-heavy, low loop tolerance
    "oracle":            {"loop": 0.20, "truncation": 0.10, "efficiency": 0.10, "completion": 0.15},
    "ultrabrain":        {"loop": 0.20, "truncation": 0.10, "efficiency": 0.10, "completion": 0.15},
    "metis":             {"loop": 0.25, "truncation": 0.10, "efficiency": 0.10, "completion": 0.15},
    "momus":             {"loop": 0.25, "truncation": 0.10, "efficiency": 0.10, "completion": 0.15},
    "prometheus":        {"loop": 0.20, "truncation": 0.10, "efficiency": 0.10, "completion": 0.15},
    "writing":           {"loop": 0.20, "truncation": 0.05, "efficiency": 0.10, "completion": 0.30},
    "librarian":         {"loop": 0.15, "truncation": 0.10, "efficiency": 0.30, "completion": 0.15},
    # Moderate seats: working seats
    "deep":              {"loop": 0.20, "truncation": 0.25, "efficiency": 0.15, "completion": 0.15},
    "hephaestus":        {"loop": 0.20, "truncation": 0.25, "efficiency": 0.15, "completion": 0.15},
    "sisyphus_junior":   {"loop": 0.20, "truncation": 0.25, "efficiency": 0.15, "completion": 0.15},
    "artistry":          {"loop": 0.20, "truncation": 0.10, "efficiency": 0.15, "completion": 0.15},
    "visual_engineering":{"loop": 0.20, "truncation": 0.10, "efficiency": 0.15, "completion": 0.15},
    "multimodal_looker": {"loop": 0.20, "truncation": 0.10, "efficiency": 0.15, "completion": 0.15},
    "unspecified_high":  {"loop": 0.30, "truncation": 0.10, "efficiency": 0.20, "completion": 0.15},
    # Lenient seats: high-frequency, cost-sensitive
    "explore":           {"loop": 0.20, "truncation": 0.05, "efficiency": 0.50, "completion": 0.05},
    "quick":             {"loop": 0.20, "truncation": 0.05, "efficiency": 0.50, "completion": 0.05},
    "atlas":             {"loop": 0.20, "truncation": 0.05, "efficiency": 0.50, "completion": 0.05},
    "unspecified_low":   {"loop": 0.20, "truncation": 0.05, "efficiency": 0.50, "completion": 0.05},
}

# Per-role hard veto thresholds (TIER 1).
# If ANY of these are exceeded, the model is eliminated regardless of weights.
# Based on industry consensus: loop ≥3 iterations (≈0.15), code truncation >10%.
ROLE_HARD_VETOS: dict[str, dict[str, float]] = {
    "oracle":            {"loop_max": 0.15},
    "ultrabrain":        {"loop_max": 0.15},
    "metis":             {"loop_max": 0.15, "truncation_max": 0.15},
    "momus":             {"loop_max": 0.15, "truncation_max": 0.15},
    "prometheus":        {"loop_max": 0.15, "truncation_max": 0.15},
    "writing":           {"loop_max": 0.15},
    "librarian":         {"loop_max": 0.15, "truncation_max": 0.20},
    "deep":              {"loop_max": 0.15, "truncation_max": 0.10},
    "hephaestus":        {"loop_max": 0.15, "truncation_max": 0.10},
    "sisyphus_junior":   {"loop_max": 0.15, "truncation_max": 0.10},
    "artistry":          {"loop_max": 0.15},
    "visual_engineering":{"loop_max": 0.15},
    "multimodal_looker": {"loop_max": 0.15},
    "unspecified_high":  {"loop_max": 0.15},
    "explore":           {"loop_max": 0.15},
    "quick":             {"loop_max": 0.15},
    "atlas":             {"loop_max": 0.15},
    "unspecified_low":   {"loop_max": 0.15},
}

# All SEAT_CODES covered by the role tables too? (kept in sync by tests)
assert set(ROLE_HEALTH_WEIGHTS) == set(SEAT_CODES), (
    "ROLE_HEALTH_WEIGHTS must cover every seat in SEAT_CODES"
)
assert set(ROLE_HARD_VETOS) == set(SEAT_CODES), (
    "ROLE_HARD_VETOS must cover every seat in SEAT_CODES"
)


def evaluate_gate(
    report: HealthReport,
    level: GateLevel,
    seat_code: str | None = None,
) -> tuple[bool, list[str]]:
    """Evaluate a HealthReport against a gate level, optionally seat-aware.

    Returns ``(passed, notes)`` where:

      * ``passed`` is False iff at least one *measured* metric violates its
        enforced threshold (missing data never fails a model);
      * ``notes`` lists every non-passing check in check order: either a
        "… not measured" informational note or a violation description.

    Tiered evaluation when ``seat_code`` is given:

      * TIER 1 — per-role hard vetoes from ``ROLE_HARD_VETOS`` are checked
        first: an extreme worst-response loop score (or extreme truncation
        rate for the seats that set ``truncation_max``) eliminates the model
        regardless of level thresholds.
      * TIER 2 — the level-based checks below apply with the level's
        (relaxed) thresholds, exactly as without a seat_code.

    When ``seat_code`` is None, behavior is identical to the pre-tier
    gate (backward compatible).

    The loop gate uses the POOL-LEVEL MEAN (``loop_mean``); a single bad
    response must never sink a model, so ``loop_max`` is informational only
    — unless a hard veto (TIER 1) explicitly gates it per role.

    Thresholds that are ``None`` for the level are not enforced and produce
    no note at all.
    """
    thresholds = GATES[level]
    notes: list[str] = []
    passed = True

    # TIER 1 — per-role hard vetoes (extreme repetition / truncation only).
    loop_vetoed = False
    if seat_code is not None:
        vetoes = ROLE_HARD_VETOS.get(seat_code)
        if vetoes is not None:
            loop_max_veto = vetoes.get("loop_max")
            if loop_max_veto is not None and report.loop_max is not None:
                if report.loop_max >= loop_max_veto:
                    passed = False
                    loop_vetoed = True
                    notes.append(
                        f"hard veto: worst-response loop repetition "
                        f"{report.loop_max:.3f} >= {loop_max_veto:.3f}"
                    )
            truncation_veto = vetoes.get("truncation_max")
            if truncation_veto is not None and report.truncation_rate is not None:
                if report.truncation_rate > truncation_veto:
                    passed = False
                    notes.append(
                        f"hard veto: truncation rate "
                        f"{report.truncation_rate:.3f} exceeds {truncation_veto:.3f}"
                    )

    # TIER 2 — level-based gate checks (unchanged from before).
    checks = [
        (
            "loop repetition (mean)",
            report.loop_mean,
            thresholds.loop_mean_max,
            lambda v, t: v > t,
            "exceeds",
        ),
        (
            "truncation rate",
            report.truncation_rate,
            thresholds.truncation_max,
            lambda v, t: v > t,
            "exceeds",
        ),
    ]
    if thresholds.unanimity_min is not None:
        checks.append(
            (
                "consistency unanimity",
                report.consistency_unanimity_pct,
                thresholds.unanimity_min,
                lambda v, t: v < t,
                "below",
            )
        )
    if thresholds.completion_min is not None:
        checks.append(
            (
                "answer completion",
                report.answer_completion_rate,
                thresholds.completion_min,
                lambda v, t: v < t,
                "below",
            )
        )

    for label, value, threshold, violated, relation in checks:
        assert threshold is not None
        if value is None:
            notes.append(f"{label} not measured")
            continue
        if violated(value, threshold):
            passed = False
            notes.append(f"{label} {value:.3f} {relation} {threshold:.3f}")

    # Worst single response is informational only — never a failure.
    if not loop_vetoed and report.loop_max is not None and report.loop_max >= 0.5:
        notes.append(
            f"worst-response loop repetition {report.loop_max:.3f} "
            "(informational; not gated)"
        )
    return passed, notes


def health_rank_score(
    report: HealthReport,
    seat_code: str | None = None,
) -> float:
    """Single-number health ranking key; LOWER = healthier.

    Without ``seat_code`` (backward compatible) the key is the additive
    composite of the two most decision-relevant behavioral signals:

        loop_mean (defaults to 0.0 when not measured)
        + truncation_rate (defaults to 0.0 when not measured)

    With ``seat_code`` the key is the seat-weighted composite (TIER 3):
    each penalty in ``ROLE_HEALTH_WEIGHTS[seat_code]`` scales its metric,
    returned as a lower-is-healthier sum:

        w_loop        * loop_mean (0.0 when not measured)
      + w_truncation  * truncation_rate (0.0 when not measured)
      + w_efficiency  * max(0, token_efficiency / 1000 - 1) (0 below baseline)
      + w_completion  * (1 - answer_completion_rate) (0 when not measured)

    An unknown ``seat_code`` (not in ``ROLE_HEALTH_WEIGHTS``) falls back to
    the unweighted composite rather than inventing weights, keeping ranker
    behavior stable for ad-hoc seats.

    Used ONLY as a tie-break inside the ranker (never to overturn a
    separated capability lead); documentable and trivially interpretable.
    """
    if seat_code is None:
        return (report.loop_mean or 0.0) + (report.truncation_rate or 0.0)
    weights = ROLE_HEALTH_WEIGHTS.get(seat_code)
    if weights is None:
        return (report.loop_mean or 0.0) + (report.truncation_rate or 0.0)
    loop_penalty = weights["loop"] * (report.loop_mean or 0.0)
    truncation_penalty = weights["truncation"] * (report.truncation_rate or 0.0)
    efficiency_penalty = weights["efficiency"] * max(
        0.0, (report.token_efficiency or 0.0) / 1000.0 - 1.0
    )
    completion_penalty = weights["completion"] * (
        1.0 - (report.answer_completion_rate or 1.0)
    )
    return loop_penalty + truncation_penalty + efficiency_penalty + completion_penalty


__all__ = [
    "GateLevel",
    "HealthThresholds",
    "GATES",
    "SEAT_HEALTH_GATE",
    "ROLE_HEALTH_WEIGHTS",
    "ROLE_HARD_VETOS",
    "evaluate_gate",
    "health_rank_score",
]