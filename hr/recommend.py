"""Read-only recommendations derived from the canonical verdict inputs."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, TypeVar

import numpy as np
import psycopg2
import yaml

from hr.config import config_path, load_yaml
from hr.db import connect as get_connection
from hr.deployable import load_deployable
from hr.seats.health_gates import health_rank_score
from hr.seats.health_gates import evaluate_gate
from hr.recommendation_constraints import (
    ConstraintVerdict,
    CostConstraint,
    FreshnessConstraint,
    LatencyConstraint,
    RecommendationConstraints,
    ReliabilityConstraint,
    UncertaintyConstraint,
)

log = logging.getLogger(__name__)

# Type parameter for ``_read_evidence`` so evidence readers keep their
# declared return type instead of being erased to object.
T = TypeVar("T")


_REFERENCE_PRIOR = 70.0

# Fallback per-request token estimate for task costing when the model's
# battery profile is absent from configs/models.yaml `tokens_per_call:`.
# Mirrors EST_TOKENS_PER_CALL in hr/calibration_items.py (which lives in the
# untracked calibration module and may not be imported from here).
_FALLBACK_TOKENS_PER_CALL = 5_000

_SCORE_CAVEAT = (
    "score is the capability mean across task batteries minus the health penalty"
)
_INTERVAL_CAVEAT = (
    "interval is a heuristic +/-0.05*(1-p) band around the score; "
    "stage-1 anytime-valid intervals are authoritative during sweeps"
)

_TASK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "code_gen": ("code", "program", "function", "debug", "fix bug"),
    "reasoning": ("reason", "math", "logic", "analyze"),
    "livebench_speed": ("fast", "quick", "simple", "short"),
    "vision": ("image", "screenshot", "see", "visual", "ui"),
    "tool_a": ("tool", "api", "mcp", "function call"),
    "livebench_long_context": ("context", "long", "large file", "repo"),
    "instruction_follow": ("write", "document", "explain"),
    "long_horizon": ("plan", "project", "schedule", "multi-step", "dependency"),
}

_DOMAIN_CATEGORY: dict[str, str] = {
    "reasoning": "reasoning",
    "code": "code_gen",
    "writing": "instruction_follow",
    "creative": "reasoning",
    "frontend": "vision",
    "vision": "vision",
    "support": "livebench_speed",
    "planning": "reasoning",
    "search": "livebench_speed",
    "research": "reasoning",
    "general": "livebench_speed",
    "tool": "tool_a",
}


def _blend_value(
    live: float | None,
    reference: tuple[float, float | None] | None,
) -> float:
    if live is None and reference is None:
        return 0.0
    if reference is None:
        return 0.0 if live is None else float(live)
    score, confidence = reference
    weight = float(confidence) if confidence is not None else 1.0
    effective = weight * float(score) + (1.0 - weight) * _REFERENCE_PRIOR
    return effective if live is None else min(float(live), effective)


def load_seat_specs() -> list[dict]:
    """Return the authoritative seat definitions from seats.yaml."""
    try:
        data = load_yaml("seats.yaml")
    except FileNotFoundError:
        return []
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid seats.yaml at {config_path('seats.yaml')}: {exc}") from exc
    return list(data.get("seats", []))


def _seat_capability_weights(seat: dict) -> dict[str, float]:
    primaries = seat.get("primary_capabilities") or ()
    if not isinstance(primaries, list) or not primaries:
        primaries = (_DOMAIN_CATEGORY.get(seat.get("domain", "general"), "livebench_speed"),)
    weight = 1.0 / len(primaries)
    return {str(capability): weight for capability in primaries}


_CAPABILITY_GATES: dict[str, str] = {"vision": "vision", "tools": "tool_use"}


def _seat_gates_ok(seat: dict, scores: dict[str, float]) -> bool:
    return all(
        scores.get(_CAPABILITY_GATES.get(str(capability), str(capability)), 0.0) > 0.0
        for capability in seat.get("required_capabilities") or []
    )


def _load_pricing() -> dict[str, float]:
    """Per-model USD per 1M output tokens from configs/models.yaml."""
    try:
        data = load_yaml("models.yaml")
    except (FileNotFoundError, yaml.YAMLError):
        return {}
    pricing = data.get("pricing") or {}
    return {str(model_id): float(price) for model_id, price in pricing.items()}


def _load_tokens_per_call() -> dict[str, int]:
    """Per-battery output-token profile from configs/models.yaml."""
    try:
        data = load_yaml("models.yaml")
    except (FileNotFoundError, yaml.YAMLError):
        return {}
    profile = data.get("tokens_per_call") or {}
    return {str(battery): int(tokens) for battery, tokens in profile.items()}


def _estimate_task_tokens(
    batteries: tuple[str, ...], profile: dict[str, int]
) -> int:
    matched = [profile[battery] for battery in batteries if battery in profile]
    if not matched:
        return _FALLBACK_TOKENS_PER_CALL
    return max(matched)


def _estimate_cost(
    model_id: str, task_tokens: int, pricing: dict[str, float]
) -> float | None:
    price = pricing.get(model_id)
    if price is None and "/" in model_id:
        price = pricing.get(model_id.split("/", 1)[1])
    if price is None:
        return None
    return float(price) * task_tokens / 1_000_000.0


def default_constraints() -> RecommendationConstraints:
    """Default operational policy — every constraint enabled with documented,
    revisable thresholds (freshness 30d, cost $0.10/request, latency
    60s/120s p50/p95, reliability >= 95%, uncertainty >= 0.80 separation)."""
    return RecommendationConstraints(
        freshness=FreshnessConstraint(max_age_days=30),
        cost=CostConstraint(max_cost_per_request_usd=0.10),
        latency=LatencyConstraint(
            max_latency_p50_ms=60_000, max_latency_p95_ms=120_000
        ),
        reliability=ReliabilityConstraint(min_success_rate=0.95),
        uncertainty=UncertaintyConstraint(min_separation_confidence=0.80),
    )


def _policy_summary(constraints: RecommendationConstraints) -> dict[str, object]:
    return {
        "freshness_max_age_days": (
            constraints.freshness.max_age_days if constraints.freshness else None
        ),
        "cost_max_per_request_usd": (
            constraints.cost.max_cost_per_request_usd if constraints.cost else None
        ),
        "latency_max_p50_ms": (
            constraints.latency.max_latency_p50_ms if constraints.latency else None
        ),
        "latency_max_p95_ms": (
            constraints.latency.max_latency_p95_ms if constraints.latency else None
        ),
        "reliability_min_success_rate": (
            constraints.reliability.min_success_rate
            if constraints.reliability
            else None
        ),
        "uncertainty_min_separation_confidence": (
            constraints.uncertainty.min_separation_confidence
            if constraints.uncertainty
            else None
        ),
        "health_gate": "moderate",
    }


def _heuristic_interval(score: float, separation_prob: float) -> tuple[float, float]:
    """Heuristic +/-0.05*(1-p) band around ``score``, clamped to the score's
    natural scale: [0, 1] for normalized scores, [0, 100] for 0-100 scores.
    The band never leaves the domain and lo <= score <= hi always holds."""
    half_width = 0.05 * (1.0 - separation_prob)
    scale_max = 100.0 if score > 1.0 else 1.0
    return (max(0.0, score - half_width), min(scale_max, score + half_width))


def _sweep_age_days(sweep_created_at: datetime | None) -> float | None:
    if sweep_created_at is None:
        return None
    ts = sweep_created_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)  # naive timestamps are treated as UTC
    return (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0


@dataclass(frozen=True)
class RecommendationItem:
    """One model's tri-state verdict under the operational policy."""

    model_id: str
    status: str  # "eligible" | "excluded" | "indeterminate"
    score: float | None
    interval: tuple[float, float] | None
    thresholds: dict[str, object]
    caveats: tuple[str, ...]
    reasons: tuple[str, ...]
    cost_estimate_usd: float | None


@dataclass(frozen=True)
class RecommendationResult:
    """Full tri-state outcome of one task recommendation request."""

    task: str
    batteries: tuple[str, ...]
    sweep_id: str | None
    sweep_age_days: float | None
    eligible: tuple[RecommendationItem, ...]
    excluded: tuple[RecommendationItem, ...]
    indeterminate: tuple[RecommendationItem, ...]


def _item_to_dict(item: RecommendationItem) -> dict[str, object]:
    return {
        "model_id": item.model_id,
        "status": item.status,
        "score": item.score,
        "interval": list(item.interval) if item.interval is not None else None,
        "thresholds": item.thresholds,
        "caveats": list(item.caveats),
        "reasons": list(item.reasons),
        "cost_estimate_usd": item.cost_estimate_usd,
    }


def _result_to_dict(result: RecommendationResult) -> dict[str, object]:
    return {
        "task": result.task,
        "batteries": list(result.batteries),
        "sweep_id": result.sweep_id,
        "sweep_age_days": result.sweep_age_days,
        "eligible": [_item_to_dict(item) for item in result.eligible],
        "excluded": [_item_to_dict(item) for item in result.excluded],
        "indeterminate": [_item_to_dict(item) for item in result.indeterminate],
    }


def _first_thresholds(result: RecommendationResult) -> dict[str, object]:
    for bucket in (result.eligible, result.excluded, result.indeterminate):
        if bucket:
            return bucket[0].thresholds
    return {}


def _format_item_line(item: RecommendationItem) -> list[str]:
    line = f"  {item.model_id}  score {item.score:.4f}" if item.score is not None \
        else f"  {item.model_id}  score -"
    if item.interval is not None:
        line += f"  interval [{item.interval[0]:.4f}, {item.interval[1]:.4f}]"
    if item.cost_estimate_usd is not None:
        line += f"  est ${item.cost_estimate_usd:.4f}/request"
    lines = [line]
    lines.extend(f"      - {reason}" for reason in item.reasons)
    lines.extend(f"      ~ {caveat}" for caveat in item.caveats)
    return lines


def format_recommendation_result(
    result: RecommendationResult, fmt: str = "table"
) -> str:
    """Render a RecommendationResult as a deterministic table or JSON string."""
    if fmt == "json":
        return json.dumps(_result_to_dict(result), indent=2)

    lines = [
        f"# Task: {result.task}",
        f"# Batteries: {', '.join(result.batteries)}",
        f"# Sweep: {result.sweep_id if result.sweep_id is not None else '(none)'}"
        + (
            f" (age {result.sweep_age_days:.1f} days)"
            if result.sweep_age_days is not None
            else ""
        ),
        "# Policy: " + json.dumps(_first_thresholds(result), sort_keys=True),
        "",
    ]
    for label, bucket in (
        ("ELIGIBLE", result.eligible),
        ("EXCLUDED", result.excluded),
        ("INDETERMINATE", result.indeterminate),
    ):
        lines.append(f"{label} ({len(bucket)}):")
        for item in bucket:
            lines.extend(_format_item_line(item))
        lines.append("")
    return "\n".join(lines)


class RecommendationEngine:
    """Project the latest canonical verdict into seat and task recommendations."""

    def __init__(self) -> None:
        from hr.decision import capability_means, latest_sweep_id
        from hr.health import sweep_health

        self._conn = get_connection()
        try:
            self._sweep_id = latest_sweep_id(self._conn)
        except ValueError:
            self._sweep_id = None
            self._means: dict[str, dict[str, float]] = {}
            self._health = {}
        else:
            self._means = capability_means(self._conn, self._sweep_id)
            self._health = sweep_health(self._conn, self._sweep_id)

    def close(self) -> None:
        """Release the underlying DB connection. Idempotent — a second call
        (or a call after __exit__) is a no-op."""
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

    def __enter__(self) -> "RecommendationEngine":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _require_conn(self) -> psycopg2.extensions.connection:
        """Return the live DB connection; fail loudly if the engine is closed.

        ``close()`` nulls ``self._conn`` so the type checker cannot prove a
        connection at the query sites below; this guard turns what would be an
        AttributeError on ``None`` into a named error instead.
        """
        conn = self._conn
        if conn is None:
            raise RuntimeError("RecommendationEngine is closed; recreate it before use")
        return conn

    def recommend_for_task(self, task_description: str) -> list[tuple[str, float]]:
        text = task_description.lower()
        batteries = {
            battery
            for battery, keywords in _TASK_KEYWORDS.items()
            if any(keyword in text for keyword in keywords)
        }
        if not batteries:
            batteries = {"reasoning", "code_gen", "livebench_speed", "tool_a"}
        weight = 1.0 / len(batteries)
        scored = []
        for model_id, means in self._means.items():
            if not batteries.issubset(means):
                continue
            report = self._health.get(model_id)
            if report is not None and not evaluate_gate(report, "moderate")[0]:
                continue
            capability = sum(means.get(battery, 0.0) * weight for battery in batteries)
            penalty = health_rank_score(report) if report is not None else 0.0
            scored.append((model_id, capability - penalty))
        return sorted(scored, key=lambda pair: pair[1], reverse=True)[:5]

    def recommend_with_constraints(
        self,
        task_description: str,
        constraints: RecommendationConstraints,
    ) -> list[tuple[str, float, list[str]]]:
        """Recommend models with operational constraint filtering.
        
        Returns list of (model_id, score, constraint_failure_reasons) tuples.
        Models that pass all constraints have empty failure_reasons list.
        """
        text = task_description.lower()
        batteries = {
            battery
            for battery, keywords in _TASK_KEYWORDS.items()
            if any(keyword in text for keyword in keywords)
        }
        if not batteries:
            batteries = {"reasoning", "code_gen", "livebench_speed", "tool_a"}
        weight = 1.0 / len(batteries)
        
        sweep_created_at = self._get_sweep_created_at()
        latency_stats = self._get_latency_stats()
        success_rates = self._get_success_rates()
        separation_probs = self._get_separation_probabilities()
        
        results = []
        for model_id, means in self._means.items():
            if not batteries.issubset(means):
                continue
            report = self._health.get(model_id)
            if report is not None and not evaluate_gate(report, "moderate")[0]:
                continue
            
            passes, reasons = constraints.filter_models(
                model_id=model_id,
                sweep_created_at=sweep_created_at,
                latency_stats=latency_stats.get(model_id),
                success_rate=success_rates.get(model_id),
                separation_prob=separation_probs.get(model_id),
            )
            
            capability = sum(means.get(battery, 0.0) * weight for battery in batteries)
            penalty = health_rank_score(report) if report is not None else 0.0
            score = capability - penalty
            
            results.append((model_id, score, [] if passes else reasons))
        
        return sorted(results, key=lambda t: t[1], reverse=True)[:10]

    def recommend(
        self,
        task_description: str,
        constraints: RecommendationConstraints | None = None,
    ) -> RecommendationResult:
        """Tri-state recommendations: eligible / excluded / indeterminate.

        Unlike ``recommend_for_task`` (a bare point-score ranking) this
        enforces the operational policy — freshness, cost, latency,
        reliability, uncertainty and the health gate — by default
        (``default_constraints()`` when ``constraints`` is None). An
        enabled constraint with ABSENT evidence marks the model
        ``indeterminate``; it never silently passes.
        """
        policy = constraints if constraints is not None else default_constraints()
        batteries = self._match_batteries(task_description)
        weight = 1.0 / len(batteries)

        sweep_created_at = self._read_evidence(self._get_sweep_created_at)
        latency_stats = self._read_evidence(self._get_latency_stats) or {}
        success_rates = self._read_evidence(self._get_success_rates) or {}
        pricing = _load_pricing()
        task_tokens = _estimate_task_tokens(batteries, _load_tokens_per_call())
        thresholds = _policy_summary(policy)

        covered: list[tuple[str, float]] = []
        for model_id, means in self._means.items():
            if set(batteries).issubset(means):
                capability = sum(means[battery] * weight for battery in batteries)
                report = self._health.get(model_id)
                penalty = health_rank_score(report) if report is not None else 0.0
                covered.append((model_id, capability - penalty))
        covered.sort(key=lambda pair: (-pair[1], pair[0]))

        rival_of: dict[str, str | None] = {}
        if len(covered) >= 2:
            rival_of[covered[0][0]] = covered[1][0]
            for model_id, _ in covered[1:]:
                rival_of[model_id] = covered[0][0]
        else:
            for model_id, _ in covered:
                rival_of[model_id] = None

        eligible: list[RecommendationItem] = []
        excluded: list[RecommendationItem] = []
        indeterminate: list[RecommendationItem] = []

        for model_id, means in self._means.items():
            estimated_cost = _estimate_cost(model_id, task_tokens, pricing)
            missing = [b for b in batteries if b not in means]
            if missing:
                indeterminate.append(
                    RecommendationItem(
                        model_id=model_id,
                        status="indeterminate",
                        score=None,
                        interval=None,
                        thresholds=thresholds,
                        caveats=(),
                        reasons=("missing battery " + ", ".join(missing),),
                        cost_estimate_usd=estimated_cost,
                    )
                )
                continue

            capability = sum(means[battery] * weight for battery in batteries)
            report = self._health.get(model_id)
            penalty = health_rank_score(report) if report is not None else 0.0
            score = capability - penalty

            separation_prob = self._separation_vs_rival(
                model_id, rival_of.get(model_id), batteries
            )
            verdicts = policy.evaluate(
                sweep_created_at=sweep_created_at,
                latency_stats=latency_stats.get(model_id),
                success_rate=success_rates.get(model_id),
                separation_prob=separation_prob,
                estimated_cost=estimated_cost,
            )
            if report is None:
                verdicts.append(
                    ConstraintVerdict(
                        "health", "missing",
                        "health: no HealthReport for model (missing evidence)",
                    )
                )
            elif not evaluate_gate(report, "moderate")[0]:
                verdicts.append(
                    ConstraintVerdict("health", "fail", "health: fails moderate gate")
                )
            else:
                verdicts.append(
                    ConstraintVerdict("health", "pass", "health: passes moderate gate")
                )

            # A measured-but-weak separation means the pair's intervals
            # overlap — the model cannot be distinguished from its rival,
            # which is indeterminacy, not a quality failure (plan contract:
            # overlapping CI with the paired rival -> indeterminate).
            verdicts = [
                ConstraintVerdict(
                    "uncertainty",
                    "missing",
                    "uncertainty: cannot separate from rival "
                    "(overlapping intervals)",
                )
                if v.constraint == "uncertainty" and v.status == "fail"
                else v
                for v in verdicts
            ]
            fails = [v for v in verdicts if v.status == "fail"]
            missing = [v for v in verdicts if v.status == "missing"]
            if fails:
                status = "excluded"
                reasons = tuple(v.detail for v in fails)
                caveats: tuple[str, ...] = ()
            elif missing:
                status = "indeterminate"
                reasons = tuple(v.detail for v in missing)
                caveats = ()
            else:
                status = "eligible"
                reasons = ()
                caveats = (_SCORE_CAVEAT,)
                if separation_prob is not None:
                    caveats = (_SCORE_CAVEAT, _INTERVAL_CAVEAT)

            interval = (
                _heuristic_interval(score, separation_prob)
                if separation_prob is not None
                else None
            )
            item = RecommendationItem(
                model_id=model_id,
                status=status,
                score=score,
                interval=interval,
                thresholds=thresholds,
                caveats=caveats,
                reasons=reasons,
                cost_estimate_usd=estimated_cost,
            )
            if status == "eligible":
                eligible.append(item)
            elif status == "excluded":
                excluded.append(item)
            else:
                indeterminate.append(item)

        eligible.sort(key=lambda item: (-(item.score or 0.0), item.model_id))
        excluded.sort(key=lambda item: item.model_id)
        indeterminate.sort(key=lambda item: item.model_id)

        return RecommendationResult(
            task=task_description,
            batteries=batteries,
            sweep_id=self._sweep_id,
            sweep_age_days=_sweep_age_days(sweep_created_at),
            eligible=tuple(eligible),
            excluded=tuple(excluded),
            indeterminate=tuple(indeterminate),
        )

    def _read_evidence(self, reader: Callable[[], T]) -> T | None:
        """Read an evidence source, degrading schema-drift failures to None.

        Some live DBs predate columns the contract-test schema has (e.g.
        ``hr.run.status``). An unreadable evidence source is treated as
        MISSING evidence — the affected constraint then rules every model
        indeterminate rather than crashing the recommendation.
        """
        try:
            return reader()
        except Exception:
            return None

    def _match_batteries(self, task_description: str) -> tuple[str, ...]:
        text = task_description.lower()
        batteries = {
            battery
            for battery, keywords in _TASK_KEYWORDS.items()
            if any(keyword in text for keyword in keywords)
        }
        if not batteries:
            batteries = {"reasoning", "code_gen", "livebench_speed", "tool_a"}
        return tuple(sorted(batteries))

    def _separation_vs_rival(
        self,
        model_id: str,
        rival_id: str | None,
        batteries: tuple[str, ...],
    ) -> float | None:
        """Mean DB separation probability of ``model_id`` vs ``rival_id``.

        Both directional orientations are folded (model_a/model_b and the
        reverse); batteries without a pair for these two models are skipped.
        Returns None when there is no rival, no sweep, or no pair evidence —
        the uncertainty constraint then rules the model indeterminate.
        """
        if rival_id is None or self._sweep_id is None:
            return None
        try:
            from hr.decision import separation_probabilities

            sep_data = separation_probabilities(self._conn, self._sweep_id)
        except Exception:
            return None
        probs: list[float] = []
        for battery in batteries:
            pairs = sep_data.get(battery, {})
            forward = pairs.get((model_id, rival_id))
            prob = forward if forward is not None else pairs.get((rival_id, model_id))
            if prob is not None:
                probs.append(float(prob))
        if not probs:
            return None
        return sum(probs) / len(probs)

    def _get_sweep_created_at(self) -> datetime | None:
        """Get the creation timestamp of the current sweep."""
        if self._sweep_id is None:
            return None
        with self._require_conn().cursor() as cur:
            cur.execute(
                "SELECT created_at FROM hr.sweep WHERE sweep_id = %s",
                (self._sweep_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def _get_latency_stats(self) -> dict[str, dict[str, float]]:
        """Calculate latency percentiles per model from measurement data."""
        if self._sweep_id is None:
            return {}
        with self._require_conn().cursor() as cur:
            cur.execute(
                """
                SELECT r.model_id, m.latency_ms
                FROM hr.measurement m
                JOIN hr.run r ON m.run_id = r.run_id
                WHERE r.sweep_id = %s AND m.latency_ms IS NOT NULL
                """,
                (self._sweep_id,),
            )
            rows = cur.fetchall()
        
        model_latencies: dict[str, list[float]] = {}
        for model_id, latency_ms in rows:
            model_latencies.setdefault(model_id, []).append(float(latency_ms))
        
        stats = {}
        for model_id, latencies in model_latencies.items():
            if not latencies:
                continue
            # linear interpolation (np.percentile default): exact percentile
            # between samples, not an int-index approximation
            stats[model_id] = {
                "p50": float(np.percentile(latencies, 50)),
                "p95": float(np.percentile(latencies, 95)),
            }
        return stats

    def _get_success_rates(self) -> dict[str, float]:
        """Calculate success rate per model from run status."""
        if self._sweep_id is None:
            return {}
        with self._require_conn().cursor() as cur:
            cur.execute(
                """
                SELECT model_id,
                       COUNT(*) as total,
                       SUM(CASE WHEN status = 'scored' THEN 1 ELSE 0 END) as successful
                FROM hr.run
                WHERE sweep_id = %s
                GROUP BY model_id
                """,
                (self._sweep_id,),
            )
            rows = cur.fetchall()
        
        return {
            model_id: successful / total if total > 0 else 0.0
            for model_id, total, successful in rows
        }

    def _get_separation_probabilities(self) -> dict[str, float]:
        """Get separation probabilities for each model vs the best.
        
        Returns a dict mapping model_id to its minimum separation probability
        across all batteries (conservative estimate).
        """
        if self._sweep_id is None:
            return {}
        try:
            from hr.decision import separation_probabilities
            sep_data = separation_probabilities(self._conn, self._sweep_id)
            
            model_probs: dict[str, list[float]] = {}
            for battery_code, pairs in sep_data.items():
                for (model_a, model_b), prob in pairs.items():
                    model_probs.setdefault(model_a, []).append(prob)
                    model_probs.setdefault(model_b, []).append(prob)
            
            return {
                model_id: min(probs) if probs else 0.0
                for model_id, probs in model_probs.items()
            }
        except Exception:
            return {}

    def seat_recommendations(self, seats: list[dict] | None = None) -> str:
        from hr.decision import (
            battery_codes,
            model_capabilities,
            seat_assignments,
            separation_probabilities,
        )

        selected_seats = seats if seats is not None else load_seat_specs()
        seat_db = {
            str(seat["seat_code"]): {
                "seat_code": str(seat["seat_code"]),
                "required_capabilities": list(seat.get("required_capabilities") or []),
                "ctx_p95": seat.get("ctx_p95"),
            }
            for seat in selected_seats
        }
        deployable = load_deployable() if self._means else set()
        pool = set(self._means) & deployable
        assignments = seat_assignments(
            pool,
            self._means,
            self._health,
            seat_db,
            model_capabilities(self._conn),
            battery_codes(self._conn),
            set(self._means) - pool,
            False,
            separation_probabilities(self._conn, self._sweep_id)
            if self._sweep_id is not None
            else {},
        )
        by_seat = {str(item["seat_code"]): item for item in assignments}
        lines = [
            f"# Seat recommendations ({len(selected_seats)} seats from configs/seats.yaml)",
            "",
            "| seat | domain | recommended model | gate |",
            "|------|--------|-------------------|------|",
        ]
        for seat in selected_seats:
            seat_code = str(seat["seat_code"])
            assignment = by_seat.get(seat_code)
            if assignment is None:
                log.warning(
                    "seat %r is not covered by measured SEAT_CODES "
                    "(custom seat via overlay?); emitting no-data row",
                    seat_code,
                )
                lines.append(f"| {seat_code} | {seat.get('domain', '—')} | — | no-data |")
                continue
            model = assignment["primary"] or "—"
            lines.append(
                f"| {seat_code} | {seat.get('domain', '—')} | {model} | "
                f"{assignment['gate_level']} |"
            )
        lines.extend(
            [
                "",
                "_Capability prior and behavioral health use the same canonical verdict pipeline; "
                "see docs/en/capability-prior.md._",
            ]
        )
        return "\n".join(lines)
