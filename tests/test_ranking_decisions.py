"""Decision-level regressions for health-aware model assignment."""

from __future__ import annotations

import numpy as np

from hr import cli
from hr.assign.ranker import CandidateModel, rank
from hr.health import HealthReport


def _health(
    model_id: str,
    *,
    loop_mean: float = 0.0,
    loop_max: float = 0.0,
    truncation_rate: float = 0.0,
    token_efficiency: float = 500.0,
    completion_rate: float = 1.0,
) -> HealthReport:
    return HealthReport(
        model_id=model_id,
        sweep_id="sweep",
        n_measurements=20,
        loop_mean=loop_mean,
        loop_max=loop_max,
        truncation_rate=truncation_rate,
        token_efficiency=token_efficiency,
        consistency_unanimity_pct=1.0,
        answer_completion_rate=completion_rate,
    )


def _candidate(model_id: str, capability: float, health: HealthReport) -> CandidateModel:
    return CandidateModel(
        model_id=model_id,
        provider_id="provider",
        capabilities={},
        ctx_p95_tokens=0,
        scores={"reasoning": np.array([capability])},
        health=health,
    )


def test_rank_prefers_healthier_model_when_penalty_outweighs_capability_lead() -> None:
    # Given: both models pass the moderate gate, but the capability leader is
    # substantially less efficient and less reliable.
    candidates = [
        _candidate(
            "capability-leader",
            0.90,
            _health(
                "capability-leader",
                loop_mean=0.14,
                loop_max=0.14,
                truncation_rate=0.07,
                token_efficiency=2500.0,
                completion_rate=0.75,
            ),
        ),
        _candidate("healthy", 0.75, _health("healthy")),
    ]

    # When: candidates are ranked for a health-aware seat without bootstrap data.
    result = rank(
        candidates,
        {"seat_code": "deep", "required_capabilities": [], "ctx_p95": None},
        {"reasoning": 1.0},
        gate_level="moderate",
    )

    # Then: measured adverse factors change the decision, not only the report.
    assert result.primary == "healthy"


def test_seat_assignments_applies_health_penalty_to_verdict_primary() -> None:
    # Given: an explore candidate with a small capability lead but severe token waste.
    means = {
        "wasteful": {"reasoning": 0.90},
        "healthy": {"reasoning": 0.75},
    }
    reports = {
        "wasteful": _health("wasteful", token_efficiency=2500.0),
        "healthy": _health("healthy"),
    }

    # When: the production verdict/apply assignment path ranks the sweep.
    assignments = cli.seat_assignments(
        pool=set(means),
        means=means,
        reports=reports,
        seat_db={},
        caps_db={},
        codes=["reasoning"],
        retired_set=set(),
        include_retired=False,
    )

    # Then: the negative efficiency signal changes the deployed explore model.
    explore = next(row for row in assignments if row["seat_code"] == "explore")
    assert explore["primary"] == "healthy"


def test_seat_assignments_uses_directional_separation_for_tied_fitness() -> None:
    from hr.decision import seat_assignments

    # Given: equal capability and health, with bootstrap evidence favoring m_b.
    means = {
        "m_a": {"reasoning": 0.5},
        "m_b": {"reasoning": 0.5},
    }
    separations = {"reasoning": {("m_b", "m_a"): 0.99}}

    # When: the canonical seat decision is computed.
    assignments = seat_assignments(
        {"m_a", "m_b"},
        means,
        {},
        {},
        {},
        ["reasoning"],
        set(),
        False,
        separations,
    )

    # Then: the statistically supported winner becomes primary.
    oracle = next(item for item in assignments if item["seat_code"] == "oracle")
    assert oracle["primary"] == "m_b"


def test_fallbacks_include_displaced_leader_and_exclude_primary() -> None:
    # Given: equal capability scores where health displaces the first candidate.
    candidates = [
        _candidate("unhealthy", 0.5, _health("unhealthy", loop_mean=0.10)),
        _candidate("healthy", 0.5, _health("healthy")),
        _candidate("third", 0.4, _health("third")),
    ]

    # When: bootstrap confidence marks the top pair as tied.
    result = rank(
        candidates,
        {"seat_code": "deep", "required_capabilities": [], "ctx_p95": None},
        {"reasoning": 1.0},
        separation_pairs={("unhealthy", "healthy"): 0.5},
    )

    # Then: the displaced capability leader remains the first fallback.
    assert result.primary == "healthy"
    assert [model_id for model_id, _ in result.fallbacks] == ["unhealthy", "third"]
