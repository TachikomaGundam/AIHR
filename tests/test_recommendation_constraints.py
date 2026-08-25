"""Tests for recommendation constraints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hr.recommendation_constraints import (
    CostConstraint,
    FreshnessConstraint,
    LatencyConstraint,
    RecommendationConstraints,
    ReliabilityConstraint,
    UncertaintyConstraint,
)


def test_freshness_constraint_passes_recent_data() -> None:
    constraint = FreshnessConstraint(max_age_days=30)
    recent = datetime.now(timezone.utc) - timedelta(days=10)
    assert constraint.check(recent) is True


def test_freshness_constraint_fails_old_data() -> None:
    constraint = FreshnessConstraint(max_age_days=30)
    old = datetime.now(timezone.utc) - timedelta(days=60)
    assert constraint.check(old) is False


def test_freshness_constraint_fails_none() -> None:
    constraint = FreshnessConstraint(max_age_days=30)
    assert constraint.check(None) is False


def test_latency_constraint_passes_within_limits() -> None:
    constraint = LatencyConstraint(max_latency_p50_ms=1000, max_latency_p95_ms=5000)
    stats = {"p50": 500, "p95": 3000}
    assert constraint.check(stats) is True


def test_latency_constraint_fails_p50_exceeded() -> None:
    constraint = LatencyConstraint(max_latency_p50_ms=1000)
    stats = {"p50": 1500, "p95": 3000}
    assert constraint.check(stats) is False


def test_latency_constraint_fails_p95_exceeded() -> None:
    constraint = LatencyConstraint(max_latency_p95_ms=5000)
    stats = {"p50": 500, "p95": 6000}
    assert constraint.check(stats) is False


def test_latency_constraint_passes_none_stats() -> None:
    constraint = LatencyConstraint(max_latency_p50_ms=1000)
    assert constraint.check(None) is True


def test_reliability_constraint_passes_high_success_rate() -> None:
    constraint = ReliabilityConstraint(min_success_rate=0.95)
    assert constraint.check(0.98) is True


def test_reliability_constraint_fails_low_success_rate() -> None:
    constraint = ReliabilityConstraint(min_success_rate=0.95)
    assert constraint.check(0.90) is False


def test_uncertainty_constraint_passes_high_confidence() -> None:
    constraint = UncertaintyConstraint(min_separation_confidence=0.8)
    assert constraint.check(0.95) is True


def test_uncertainty_constraint_fails_low_confidence() -> None:
    constraint = UncertaintyConstraint(min_separation_confidence=0.8)
    assert constraint.check(0.60) is False


def test_uncertainty_constraint_fails_none_when_required() -> None:
    constraint = UncertaintyConstraint(require_statistical_significance=True)
    assert constraint.check(None) is False


def test_uncertainty_constraint_passes_none_when_not_required() -> None:
    constraint = UncertaintyConstraint(require_statistical_significance=False)
    assert constraint.check(None) is True


def test_recommendation_constraints_filter_passes_all() -> None:
    constraints = RecommendationConstraints(
        freshness=FreshnessConstraint(max_age_days=30),
        latency=LatencyConstraint(max_latency_p50_ms=2000),
        reliability=ReliabilityConstraint(min_success_rate=0.90),
    )
    
    recent = datetime.now(timezone.utc) - timedelta(days=5)
    passes, reasons = constraints.filter_models(
        model_id="test-model",
        sweep_created_at=recent,
        latency_stats={"p50": 1000, "p95": 1500},
        success_rate=0.95,
        separation_prob=None,
    )
    
    assert passes is True
    assert len(reasons) == 0


def test_recommendation_constraints_filter_fails_multiple() -> None:
    constraints = RecommendationConstraints(
        freshness=FreshnessConstraint(max_age_days=30),
        reliability=ReliabilityConstraint(min_success_rate=0.95),
    )
    
    old = datetime.now(timezone.utc) - timedelta(days=60)
    passes, reasons = constraints.filter_models(
        model_id="test-model",
        sweep_created_at=old,
        latency_stats=None,
        success_rate=0.85,
        separation_prob=None,
    )
    
    assert passes is False
    assert len(reasons) == 2
    assert any("older than" in r for r in reasons)
    assert any("success rate" in r for r in reasons)


def test_cost_constraint_request_cost() -> None:
    constraint = CostConstraint(max_cost_per_request_usd=1.0)
    assert constraint.check_request_cost(0.5) is True
    assert constraint.check_request_cost(1.5) is False


def test_freshness_evaluate_missing_without_timestamp() -> None:
    verdict = FreshnessConstraint(max_age_days=30).evaluate(None)
    assert verdict.status == "missing"
    assert "freshness" in verdict.detail


def test_freshness_evaluate_fail_when_stale() -> None:
    old = datetime.now(timezone.utc) - timedelta(days=60)
    verdict = FreshnessConstraint(max_age_days=30).evaluate(old)
    assert verdict.status == "fail"


def test_freshness_evaluate_pass_when_recent() -> None:
    recent = datetime.now(timezone.utc) - timedelta(days=5)
    verdict = FreshnessConstraint(max_age_days=30).evaluate(recent)
    assert verdict.status == "pass"


def test_freshness_evaluate_handles_naive_timestamp_as_utc() -> None:
    naive_old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=60)
    verdict = FreshnessConstraint(max_age_days=30).evaluate(naive_old)
    assert verdict.status == "fail"


def test_cost_evaluate_missing_without_price() -> None:
    verdict = CostConstraint(max_cost_per_request_usd=0.10).evaluate(None)
    assert verdict.status == "missing"
    assert "cost" in verdict.detail


def test_cost_evaluate_fail_over_budget() -> None:
    verdict = CostConstraint(max_cost_per_request_usd=0.10).evaluate(0.5)
    assert verdict.status == "fail"


def test_cost_evaluate_pass_within_budget() -> None:
    verdict = CostConstraint(max_cost_per_request_usd=0.10).evaluate(0.05)
    assert verdict.status == "pass"


def test_latency_evaluate_missing_without_stats() -> None:
    verdict = LatencyConstraint(max_latency_p50_ms=60_000).evaluate(None)
    assert verdict.status == "missing"
    assert "latency" in verdict.detail


def test_latency_evaluate_pass_within_limits() -> None:
    verdict = LatencyConstraint(
        max_latency_p50_ms=60_000, max_latency_p95_ms=120_000
    ).evaluate({"p50": 500.0, "p95": 3000.0})
    assert verdict.status == "pass"


def test_latency_evaluate_fail_p50_exceeded() -> None:
    verdict = LatencyConstraint(max_latency_p50_ms=60_000).evaluate(
        {"p50": 300_000.0, "p95": 400_000.0}
    )
    assert verdict.status == "fail"


def test_reliability_evaluate_missing_without_rate() -> None:
    verdict = ReliabilityConstraint(min_success_rate=0.95).evaluate(None)
    assert verdict.status == "missing"
    assert "reliability" in verdict.detail


def test_reliability_evaluate_fail_when_below_min() -> None:
    verdict = ReliabilityConstraint(min_success_rate=0.95).evaluate(0.5)
    assert verdict.status == "fail"


def test_uncertainty_evaluate_missing_without_separation() -> None:
    verdict = UncertaintyConstraint(min_separation_confidence=0.80).evaluate(None)
    assert verdict.status == "missing"
    assert "uncertainty" in verdict.detail


def test_uncertainty_evaluate_fail_when_below_confidence() -> None:
    verdict = UncertaintyConstraint(min_separation_confidence=0.80).evaluate(0.5)
    assert verdict.status == "fail"


def test_recommendation_constraints_evaluate_reports_pass_statuses() -> None:
    constraints = RecommendationConstraints(
        freshness=FreshnessConstraint(max_age_days=30),
        cost=CostConstraint(max_cost_per_request_usd=0.10),
        latency=LatencyConstraint(
            max_latency_p50_ms=60_000, max_latency_p95_ms=120_000
        ),
        reliability=ReliabilityConstraint(min_success_rate=0.95),
        uncertainty=UncertaintyConstraint(min_separation_confidence=0.80),
    )
    verdicts = constraints.evaluate(
        sweep_created_at=datetime.now(timezone.utc) - timedelta(days=5),
        latency_stats={"p50": 500.0, "p95": 3000.0},
        success_rate=0.98,
        separation_prob=0.9,
        estimated_cost=0.01,
    )
    assert len(verdicts) == 5
    assert all(v.status == "pass" for v in verdicts)


def test_recommendation_constraints_evaluate_disabled_constraints_are_skipped() -> None:
    verdicts = RecommendationConstraints().evaluate(
        sweep_created_at=None,
        latency_stats=None,
        success_rate=None,
        separation_prob=None,
        estimated_cost=None,
    )
    assert verdicts == []


def test_cost_constraint_no_limit() -> None:
    constraint = CostConstraint()
    assert constraint.check_request_cost(100.0) is True
