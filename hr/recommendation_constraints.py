"""Operational constraints for model recommendations.

This module provides constraint-based filtering and ranking for model
recommendations, ensuring that recommended models meet operational
requirements beyond pure capability scores.

Constraint categories:
- Freshness: data recency requirements
- Cost: budget limits per request or time period
- Latency: response time requirements (p50/p95)
- Reliability: uptime and success rate thresholds
- Uncertainty: statistical confidence requirements
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal


@dataclass(frozen=True)
class ConstraintVerdict:
    """Tri-state result of evaluating one constraint against evidence.

    ``status`` is one of:
      * ``pass`` — the constraint is enabled and its evidence satisfies it;
      * ``fail`` — the constraint is enabled, evidence exists, and it is
        violated (the candidate is excluded);
      * ``missing`` — the constraint is enabled but the evidence does not
        exist (the candidate is indeterminate, never silently passed).
    """

    constraint: str
    status: Literal["pass", "fail", "missing"]
    detail: str


@dataclass(frozen=True)
class FreshnessConstraint:
    """Require data to be collected within a time window."""

    max_age_days: int = 30

    def check(self, sweep_created_at: datetime | None) -> bool:
        if sweep_created_at is None:
            return False
        now = datetime.now(timezone.utc)
        age = now - sweep_created_at
        return age <= timedelta(days=self.max_age_days)

    def evaluate(self, sweep_created_at: datetime | None) -> ConstraintVerdict:
        if sweep_created_at is None:
            return ConstraintVerdict(
                "freshness", "missing",
                "freshness: no sweep data (missing evidence)",
            )
        ts = sweep_created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)  # psycopg2/naive guards: assume UTC
        age = datetime.now(timezone.utc) - ts
        if age > timedelta(days=self.max_age_days):
            return ConstraintVerdict(
                "freshness", "fail",
                f"freshness: data older than {self.max_age_days} days "
                f"(age {age.total_seconds() / 86400.0:.1f}d)",
            )
        return ConstraintVerdict(
            "freshness", "pass",
            f"freshness: data age {age.total_seconds() / 86400.0:.1f} days "
            f"<= {self.max_age_days} days",
        )


@dataclass(frozen=True)
class CostConstraint:
    """Budget limits for model usage (prices in USD, per 1M tokens)."""

    max_cost_per_request_usd: float | None = None
    max_cost_per_month_usd: float | None = None

    def check_request_cost(self, estimated_cost: float) -> bool:
        if self.max_cost_per_request_usd is None:
            return True
        return estimated_cost <= self.max_cost_per_request_usd

    def evaluate(self, estimated_cost: float | None) -> ConstraintVerdict:
        if estimated_cost is None:
            return ConstraintVerdict(
                "cost", "missing",
                "cost: no price data for model (missing evidence)",
            )
        if self.max_cost_per_request_usd is not None and \
                estimated_cost > self.max_cost_per_request_usd:
            return ConstraintVerdict(
                "cost", "fail",
                f"cost: est ${estimated_cost:.4f} per request exceeds "
                f"${self.max_cost_per_request_usd:.2f} budget",
            )
        return ConstraintVerdict(
            "cost", "pass",
            f"cost: est ${estimated_cost:.4f} per request within budget",
        )


@dataclass(frozen=True)
class LatencyConstraint:
    """Response time requirements."""

    max_latency_p50_ms: int | None = None
    max_latency_p95_ms: int | None = None

    def check(self, latency_stats: dict[str, float] | None) -> bool:
        """Check latency constraints using pre-computed statistics.
        
        Args:
            latency_stats: Dict with 'p50' and 'p95' keys (milliseconds)
        """
        if latency_stats is None:
            return True  # No data means no constraint violation
        
        if self.max_latency_p50_ms is not None:
            p50 = latency_stats.get("p50")
            if p50 is not None and p50 > self.max_latency_p50_ms:
                return False
        
        if self.max_latency_p95_ms is not None:
            p95 = latency_stats.get("p95")
            if p95 is not None and p95 > self.max_latency_p95_ms:
                return False
        
        return True

    def evaluate(
        self, latency_stats: dict[str, float] | None
    ) -> ConstraintVerdict:
        if latency_stats is None:
            return ConstraintVerdict(
                "latency", "missing",
                "latency: no measured latency (missing evidence)",
            )
        if self.max_latency_p50_ms is not None:
            p50 = latency_stats.get("p50")
            if p50 is None:
                return ConstraintVerdict(
                    "latency", "missing",
                    "latency: no p50 measurement (missing evidence)",
                )
            if p50 > self.max_latency_p50_ms:
                return ConstraintVerdict(
                    "latency", "fail",
                    f"latency: p50 {p50:.0f}ms exceeds "
                    f"{self.max_latency_p50_ms}ms",
                )
        if self.max_latency_p95_ms is not None:
            p95 = latency_stats.get("p95")
            if p95 is None:
                return ConstraintVerdict(
                    "latency", "missing",
                    "latency: no p95 measurement (missing evidence)",
                )
            if p95 > self.max_latency_p95_ms:
                return ConstraintVerdict(
                    "latency", "fail",
                    f"latency: p95 {p95:.0f}ms exceeds "
                    f"{self.max_latency_p95_ms}ms",
                )
        return ConstraintVerdict(
            "latency", "pass", "latency: within limits",
        )


@dataclass(frozen=True)
class ReliabilityConstraint:
    """Uptime and success rate requirements."""

    min_success_rate: float = 0.95
    max_failure_rate: float = 0.05

    def check(self, success_rate: float) -> bool:
        return success_rate >= self.min_success_rate

    def evaluate(self, success_rate: float | None) -> ConstraintVerdict:
        if success_rate is None:
            return ConstraintVerdict(
                "reliability", "missing",
                "reliability: no success-rate evidence (missing evidence)",
            )
        if success_rate < self.min_success_rate:
            return ConstraintVerdict(
                "reliability", "fail",
                f"reliability: success rate {success_rate:.1%} below "
                f"{self.min_success_rate:.1%}",
            )
        return ConstraintVerdict(
            "reliability", "pass",
            f"reliability: success rate {success_rate:.1%} >= "
            f"{self.min_success_rate:.1%}",
        )


@dataclass(frozen=True)
class UncertaintyConstraint:
    """Statistical confidence requirements."""

    min_separation_confidence: float = 0.8
    require_statistical_significance: bool = True

    def check(self, separation_probability: float | None) -> bool:
        if separation_probability is None:
            return not self.require_statistical_significance
        return separation_probability >= self.min_separation_confidence

    def evaluate(
        self, separation_probability: float | None
    ) -> ConstraintVerdict:
        if separation_probability is None:
            return ConstraintVerdict(
                "uncertainty", "missing",
                "uncertainty: no separation evidence vs rival "
                "(missing evidence)",
            )
        if separation_probability < self.min_separation_confidence:
            return ConstraintVerdict(
                "uncertainty", "fail",
                f"uncertainty: separation probability "
                f"{separation_probability:.2f} below "
                f"{self.min_separation_confidence:.2f} vs rival",
            )
        return ConstraintVerdict(
            "uncertainty", "pass",
            f"uncertainty: separation probability "
            f"{separation_probability:.2f} >= "
            f"{self.min_separation_confidence:.2f} vs rival",
        )


@dataclass(frozen=True)
class RecommendationConstraints:
    """Complete set of operational constraints for recommendations."""

    freshness: FreshnessConstraint | None = None
    cost: CostConstraint | None = None
    latency: LatencyConstraint | None = None
    reliability: ReliabilityConstraint | None = None
    uncertainty: UncertaintyConstraint | None = None

    def filter_models(
        self,
        model_id: str,
        sweep_created_at: datetime | None,
        latency_stats: dict[str, float] | None,
        success_rate: float | None,
        separation_prob: float | None,
    ) -> tuple[bool, list[str]]:
        """Check if a model satisfies all constraints.

        Returns (passes, reasons) where reasons lists failed constraints.
        """
        reasons: list[str] = []

        if self.freshness is not None:
            if not self.freshness.check(sweep_created_at):
                reasons.append(f"data older than {self.freshness.max_age_days} days")

        if self.latency is not None:
            if not self.latency.check(latency_stats):
                reasons.append("latency exceeds requirements")

        if self.reliability is not None:
            if success_rate is not None:
                if not self.reliability.check(success_rate):
                    reasons.append(f"success rate {success_rate:.1%} below {self.reliability.min_success_rate:.1%}")

        if self.uncertainty is not None:
            if not self.uncertainty.check(separation_prob):
                reasons.append("insufficient statistical confidence")

        return (len(reasons) == 0, reasons)

    def evaluate(
        self,
        *,
        sweep_created_at: datetime | None,
        latency_stats: dict[str, float] | None,
        success_rate: float | None,
        separation_prob: float | None,
        estimated_cost: float | None,
    ) -> list[ConstraintVerdict]:
        """Evaluate every ENABLED constraint against the given evidence.

        A constraint that is disabled (None) is skipped entirely. An enabled
        constraint whose evidence is absent yields a ``missing`` verdict —
        it never silently passes.
        """
        verdicts: list[ConstraintVerdict] = []
        if self.freshness is not None:
            verdicts.append(self.freshness.evaluate(sweep_created_at))
        if self.cost is not None:
            verdicts.append(self.cost.evaluate(estimated_cost))
        if self.latency is not None:
            verdicts.append(self.latency.evaluate(latency_stats))
        if self.reliability is not None:
            verdicts.append(self.reliability.evaluate(success_rate))
        if self.uncertainty is not None:
            verdicts.append(self.uncertainty.evaluate(separation_prob))
        return verdicts


__all__ = [
    "ConstraintVerdict",
    "FreshnessConstraint",
    "CostConstraint",
    "LatencyConstraint",
    "ReliabilityConstraint",
    "UncertaintyConstraint",
    "RecommendationConstraints",
]
