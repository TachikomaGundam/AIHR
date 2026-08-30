"""Ranker per spec §10.4.

Algorithm:
  1. Hard gates: filter out models that fail capability/capacity requirements.
     - vision seats require capabilities.vision == true
     - hephaestus seats require context_window >= seat.ctx_p95
  2. Fitness ranking: subtract seat-specific health penalties from capability
     scores, then sort the resulting decision scores (descending).
  3. Separation-driven primary:
     - If top1 vs top2 are separated (p>=0.95) → top1 is primary.
     - If tie → fallback to cost-per-solved-task ordering.
  4. Fallbacks 1..3: next candidates annotated with separation vs primary.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Tuple
import numpy as np

from ..health import HealthReport
from ..seats.health_gates import GateLevel, evaluate_gate, health_rank_score
from ..stats import bootstrap as _boot


@dataclass
class CandidateModel:
    """Model candidate with its metadata and per-battery scores."""
    model_id: str
    provider_id: str
    capabilities: Dict[str, Any]  # e.g. {"vision": true, "context_window": 128000}
    ctx_p95_tokens: int            # seat's p95 context requirement
    scores: Dict[str, np.ndarray]  # {battery_code: item-level scores}
    cost_per_task: float = 0.0     # for tiebreaking
    health: Optional[HealthReport] = None  # zero-cost behavioral health (tie-break + gate)


@dataclass
class RankerResult:
    """Output of ranker.rank."""
    primary: str
    fallbacks: List[Tuple[str, str]]  # (model_id, separation_label)
    eliminated: List[Tuple[str, str]]  # (model_id, gate_reason)
    scores_table: Dict[str, float]     # {model_id: health-adjusted fitness}


def _apply_hard_gate(c: CandidateModel, seat_required_capabilities: List[str],
                      seat_ctx_p95: Optional[int]) -> Optional[str]:
    """Return None if gate passes, else reason string."""
    for cap in (seat_required_capabilities or []):
        val = c.capabilities.get(cap, False)
        if not val:
            return f"missing capability: {cap}"
    if seat_ctx_p95 is not None:
        ctx = c.capabilities.get("context_window", 0)
        if isinstance(ctx, (int, float)) and ctx < seat_ctx_p95:
            return f"context_window({ctx}) < seat p95 ({seat_ctx_p95})"
    return None


def _weighted_score(scores: Dict[str, np.ndarray],
                     battery_weights: Dict[str, float]) -> float:
    """Compute weighted mean across batteries.

    Weights are per-battery; scores are per-battery item-level arrays.
    """
    total = 0.0
    w_sum = 0.0
    for bc, w in battery_weights.items():
        s = scores.get(bc)
        if s is not None and len(s) > 0:
            total += w * float(np.mean(s))
            w_sum += w
    return total / w_sum if w_sum > 0 else 0.0


def rank(
    candidates: List[CandidateModel],
    seat: Dict[str, Any],
    battery_weights: Dict[str, float],
    separation_pairs: Optional[Dict[Tuple[str, str], float]] = None,
    gate_level: Optional[GateLevel] = None,
) -> RankerResult:
    """
    Rank candidates for a seat per spec §10.4.

    Parameters
    ----------
    candidates : List[CandidateModel]
    seat : Dict
        Must include: seat_code, required_capabilities (list), ctx_p95 (int or None).
    battery_weights : Dict[str, float]
        {battery_code: weight} for weighted scoring.
    separation_pairs : Dict[(model_a, model_b), p_value]
        Pairwise separation confidence. Optional — if missing, use top1 by score.
    gate_level : GateLevel | None
        Behavioral-health gate for the seat. When set, candidates carrying a
        ``health`` report are evaluated against the gate's thresholds and
        failing candidates are eliminated (reason: ``health_gate:<violation>``).
        When None (or a candidate has no health report) behavior is unchanged.

    Returns
    -------
    RankerResult with primary, fallbacks, eliminated.
    """
    seat_caps = seat.get("required_capabilities", []) or []
    seat_ctx_p95 = seat.get("ctx_p95")
    seat_code = seat.get("seat_code")

    # Step 1: hard gates
    eliminated: List[Tuple[str, str]] = []
    survivors: List[CandidateModel] = []
    for c in candidates:
        reason = _apply_hard_gate(c, seat_caps, seat_ctx_p95)
        if reason is not None:
            eliminated.append((c.model_id, reason))
            continue
        # Step 1b: behavioral-health gate (only when both the seat's level and
        # the candidate's health report are known).
        if gate_level is not None and c.health is not None:
            passed, notes = evaluate_gate(c.health, gate_level, seat_code)
            if not passed:
                # First actual violation (informational "not measured" notes
                # never fail a candidate, so skip them when picking the reason).
                first_violation = next(
                    (n for n in notes if "not measured" not in n),
                    "gate failed",
                )
                eliminated.append((c.model_id, f"health_gate:{first_violation}"))
                continue
        survivors.append(c)

    if not survivors:
        raise ValueError(f"no candidates pass hard gates for seat {seat.get('seat_code')}")

    # Step 2: measured adverse factors are part of fitness, not report-only
    # metadata. Missing health data remains neutral rather than speculative.
    scores_table = {
        c.model_id: _weighted_score(c.scores, battery_weights)
        - (health_rank_score(c.health, seat_code) if c.health is not None else 0.0)
        for c in survivors
    }
    ranked = sorted(survivors, key=lambda c: scores_table[c.model_id], reverse=True)

    # Step 3: separation-driven primary
    primary = ranked[0].model_id
    if len(ranked) >= 2 and separation_pairs is not None:
        top1_id = ranked[0].model_id
        top2_id = ranked[1].model_id
        key = (top1_id, top2_id)
        rkey = (top2_id, top1_id)
        if key in separation_pairs:
            p = separation_pairs[key]
            supported = top1_id
        elif rkey in separation_pairs:
            p = separation_pairs[rkey]
            supported = top2_id
        else:
            p = 0.5
            supported = top1_id
        label = _boot.classify(p)
        if label == "separated":
            primary = supported
        elif label == "tie":
            # The fitness score already includes measured health. For a
            # statistically tied top pair, health remains the deterministic
            # first tie-break before cost.
            tied = [ranked[0], ranked[1]]

            def _tie_key(c):
                return (
                    0 if c.health is not None else 1,
                    health_rank_score(c.health, seat_code) if c.health is not None else 0.0,
                    c.cost_per_task,
                )

            tied_sorted = sorted(tied, key=_tie_key)
            primary = tied_sorted[0].model_id

    # Step 4: fallbacks 1..3
    fallbacks: List[Tuple[str, str]] = []
    fallback_candidates = [c for c in ranked if c.model_id != primary][:3]
    for c in fallback_candidates:
        if separation_pairs is not None:
            key = (primary, c.model_id)
            rkey = (c.model_id, primary)
            if key in separation_pairs:
                p = separation_pairs[key]
            elif rkey in separation_pairs:
                p = separation_pairs[rkey]
            else:
                p = 0.5
            fallbacks.append((c.model_id, _boot.classify(p)))
        else:
            fallbacks.append((c.model_id, "unknown"))

    return RankerResult(
        primary=primary,
        fallbacks=fallbacks,
        eliminated=eliminated,
        scores_table=scores_table,
    )


__all__ = ["rank", "CandidateModel", "RankerResult"]
