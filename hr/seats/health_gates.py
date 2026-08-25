"""Behavioral-health gates per seat level.

Wires the zero-cost health metrics (``hr.health``, mined from already-run
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

from ..health import HealthReport
from .health_policy import (
    GATES,
    ROLE_HARD_VETOS,
    ROLE_HEALTH_WEIGHTS,
    SEAT_HEALTH_GATE,
    GateLevel,
    HealthThresholds,
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
    # token_efficiency can be a Decimal from live NUMERIC rows — normalize
    # once so the / 1000.0 division never sees a Decimal.
    efficiency = float(report.token_efficiency or 0.0)
    efficiency_penalty = weights["efficiency"] * max(
        0.0, efficiency / 1000.0 - 1.0
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
