"""Scoring engine and role assignment for the HR agent."""
from __future__ import annotations

import datetime as dt
import statistics

import psycopg2.extras
import yaml

from hr.config import config_path, load_settings, load_yaml
from hr.database import get_connection, save_report
from hr.models import (
    BenchmarkCategory as BC,
    EvaluationReport,
    ModelProfile,
    ResearchFinding,
    RoleType,
)
from hr.reference import get_reference_scores

BENCHMARK_WEIGHTS: dict[str, float] = {
    BC.code_gen: 0.20, BC.reasoning: 0.15, BC.instruction_follow: 0.10,
    BC.speed: 0.15, BC.vision: 0.10, BC.tool_use: 0.15, BC.long_context: 0.05,
    BC.long_horizon: 0.10,
}
# Output cost USD per 1M output tokens (estimates marked; relative basis for ranking).
# Single source of truth: configs/models.yaml `pricing:` — unknown models contribute
# no cost and must never crash a caller.
def _load_model_costs() -> dict[str, float]:
    try:
        return load_yaml("models.yaml").get("pricing", {})
    except FileNotFoundError:
        return {}


MODEL_COST: dict[str, float] = _load_model_costs()

_TASK_KW: dict[str, list[str]] = {
    "code_gen": ["code", "program", "function", "debug", "fix bug"],
    "reasoning": ["reason", "math", "logic", "analyze"],
    "speed": ["fast", "quick", "simple", "short"],
    "vision": ["image", "screenshot", "see", "visual", "ui"],
    "tool_use": ["tool", "api", "mcp", "function call"],
    "long_context": ["context", "long", "large file", "repo"],
    "instruction_follow": ["write", "document", "explain"],
    "long_horizon": ["plan", "project", "schedule", "multi-step", "critical path", "dependency"],
}
_DEFAULT_W: dict[str, float] = {
    BC.code_gen: 0.30, BC.reasoning: 0.20, BC.speed: 0.20,
    BC.tool_use: 0.15, BC.instruction_follow: 0.15,
}


# Conservative baseline an unproven (low-confidence) model regresses toward.
_REFERENCE_PRIOR = 70.0


# Capability for one (model, category) = the more conservative of the model's
# live-measured result and its confidence-shrunk authoritative reference.
#
# Live graded tests saturate near 100 for frontier models, so they cannot rank
# the leaders; the published reference is the differentiator, shrunk toward a
# conservative prior by its confidence so a real leaderboard score beats an
# optimistic low-confidence estimate:  eff_ref = c*ref + (1-c)*_REFERENCE_PRIOR.
# We then cap at the live result, so a model that cannot reproduce its reputation
# through our own endpoint is held to what we measured (a live 0 forces a 0).
def _blend_value(
    live: float | None, ref: tuple[float, float] | None,
) -> float:
    if live is None and ref is None:
        return 0.0
    if ref is None:
        return float(live)  # no published score (e.g. instruction_follow): live alone
    ref_score, conf = ref
    c = float(conf) if conf is not None else 1.0
    eff_ref = c * float(ref_score) + (1.0 - c) * _REFERENCE_PRIOR
    if live is None:
        return eff_ref
    return min(float(live), eff_ref)


# Seat specs: configs/seats.yaml ONLY (no code tables).

_DOMAIN_CATEGORY: dict[str, str] = {
    "reasoning": BC.reasoning,
    "code": BC.code_gen,
    "writing": BC.instruction_follow,
    "creative": BC.reasoning,
    "frontend": BC.vision,
    "vision": BC.vision,
    "support": BC.speed,
    "planning": BC.reasoning,
    "search": BC.speed,
    "research": BC.reasoning,
    "general": BC.speed,
    "tool": BC.tool_use,
}


def load_seat_specs() -> list[dict]:
    """The authoritative seat list — ``configs/seats.yaml`` ONLY.

    The 18 seat definitions (code/name/domain/tiers/ctx_p95/hard gates) are
    data; no seat table exists in code. An invalid YAML raises ValueError
    naming the resolved file; a missing file yields ``[]`` (callers print
    the seat list as-is, never crash).
    """
    try:
        data = load_yaml("seats.yaml")
    except FileNotFoundError:
        return []
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid seats.yaml at {config_path('seats.yaml')}: {exc}") from exc
    return list(data.get("seats", []))


def _seat_capability_weights(seat: dict) -> dict[str, float]:
    """Flat weight over the seat's ``primary_capabilities`` from seats.yaml.

    Seats without a ``primary_capabilities`` list fall back to a translation
    of their ``domain`` attribute from seats.yaml; one equal weight per
    capability.
    """
    primaries = seat.get("primary_capabilities") or ()
    if not isinstance(primaries, list) or not primaries:
        primaries = (_DOMAIN_CATEGORY.get(seat.get("domain", "general"), BC.speed),)
    weights = {str(c): 1.0 for c in primaries}
    total = sum(weights.values())
    return {c: w / total for c, w in weights.items()}


# Hard gates are declared per seat in seats.yaml as capability names; the
# mapping below translates them onto blended benchmark categories.
_CAPABILITY_GATES: dict[str, str] = {"vision": BC.vision, "tools": BC.tool_use}


def _seat_gates_ok(seat: dict, blended: dict[str, float]) -> bool:
    """Whether a model's blended capability profile satisfies the seat's
    ``required_capabilities`` hard gates from seats.yaml."""
    return all(
        blended.get(_CAPABILITY_GATES.get(cap, cap), 0.0) > 0.0
        for cap in seat.get("required_capabilities") or []
    )


class RecommendationEngine:
    def __init__(self) -> None:
        self._settings = load_settings()
        self._conn = get_connection()
        self._profile_by_pk: dict[int, ModelProfile] = {}
        self._load_models()
        self._ref_by_model: dict[str, dict[str, tuple[float, float]]] = {
            p.model_id: get_reference_scores(p.model_id)
            for p in self._profile_by_pk.values()
        }

    def _load_models(self) -> None:
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, provider, model_id, display_name, context_window, max_output,"
                " supports_vision, supports_thinking, api_base_url, notes"
                " FROM hr_models ORDER BY provider, model_id")
            for row in cur.fetchall():
                pk = row.pop("id")
                self._profile_by_pk[pk] = ModelProfile(**row)

    def _fetch_latest_benchmarks(self, model_fk: int) -> dict[str, float]:
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT DISTINCT ON (benchmark_name) benchmark_name, score"
                " FROM hr_benchmarks WHERE model_fk = %s"
                " ORDER BY benchmark_name, created_at DESC", (model_fk,))
            return {
                r["benchmark_name"]: (float(r["score"]) if r["score"] is not None else 0.0)
                for r in cur.fetchall()
            }

    def _blended_scores(self, model_fk: int) -> dict[str, float]:
        profile = self._profile_by_pk[model_fk]
        live = self._fetch_latest_benchmarks(model_fk)
        ref = self._ref_by_model.get(profile.model_id, {})
        return {
            cat: _blend_value(live.get(cat), ref.get(cat))
            for cat in BENCHMARK_WEIGHTS
        }

    def live_only_composite(self, model_fk: int) -> float:
        # Set B: pure live-measured capability — excludes BOTH the online reference
        # and the research adjustment (the accessible model's actual measurement).
        live = self._fetch_latest_benchmarks(model_fk)
        return max(0.0, min(100.0, sum(
            float(live.get(c, 0.0)) * w for c, w in BENCHMARK_WEIGHTS.items()
        )))

    def _fetch_research(self, model_fk: int) -> list[ResearchFinding]:
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT model_fk, source_url, finding, category, confidence"
                " FROM hr_research WHERE model_fk = %s", (model_fk,))
            return [ResearchFinding(**r) for r in cur.fetchall()]

    @staticmethod
    def _research_adjust(
        findings: list[ResearchFinding],
    ) -> tuple[float, list[str], list[str]]:
        pos = neg = 0.0
        pros: list[str] = []
        cons: list[str] = []
        for f in findings:
            c = float(f.confidence) if f.confidence is not None else 1.0
            cat = (f.category or "").lower()
            if cat == "strength":
                pos += 2.0 * c; pros.append(f.finding)
            elif cat == "weakness":
                neg += 2.0 * c; cons.append(f.finding)
        return min(pos, 10.0) - min(neg, 10.0), pros[:5], cons[:5]

    def score_model(self, model_fk: int) -> EvaluationReport:
        profile = self._profile_by_pk.get(model_fk)
        if profile is None:
            raise KeyError(f"model_fk {model_fk} not registered")
        bm = self._blended_scores(model_fk)
        adj, pros, cons = self._research_adjust(self._fetch_research(model_fk))
        composite = max(0.0, min(100.0, sum(
            float(bm.get(c, 0.0)) * w for c, w in BENCHMARK_WEIGHTS.items()
        ) + adj))
        # Seat assignment no longer happens here: the unified verdict engine is
        # the ONLY seat-assignment engine (v1 seat-spec systems retired).
        top: list[RoleType] = []
        bits: list[str] = []
        if pros: bits.append(f"Strengths: {pros[0]}")
        if cons: bits.append(f"Concerns: {cons[0]}")
        return EvaluationReport(
            model_fk=model_fk, overall_score=composite,
            pros=pros, cons=cons, recommended_roles=top,
            summary=" | ".join(bits) if bits else "No evaluation data yet.",
        )

    def recommend_for_task(self, task_description: str) -> list[tuple[str, float]]:
        text = task_description.lower()
        weights: dict[str, float] = {c: 0.0 for c in BENCHMARK_WEIGHTS}
        any_match = False
        for cat, kws in _TASK_KW.items():
            if not any(kw in text for kw in kws):
                continue
            any_match = True
            if cat == BC.instruction_follow:
                weights[BC.instruction_follow] += 0.4
                weights[BC.reasoning] += 0.1
            else:
                weights[cat] += 0.5
        if not any_match:
            weights = dict(_DEFAULT_W)
        else:
            total = sum(weights.values())
            if total > 0:
                weights = {k: v / total for k, v in weights.items()}
        scored: list[tuple[str, float]] = [
            (f"{p.provider}/{p.model_id}",
             sum(float(self._fetch_latest_benchmarks(pk).get(c, 0.0)) * w
                 for c, w in weights.items()))
            for pk, p in self._profile_by_pk.items()
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:5]

    def seat_recommendations(self, seats: list[dict] | None = None) -> str:
        """Per-seat recommendations: seats.yaml × the blended capability prior.

        For every seat in ``configs/seats.yaml`` pick the best eligible model
        under the blended capability prior (``_blend_value``, documented in
        docs/en/capability-prior.md). Read-only: no assignment table is ever
        written here — the unified verdict engine owns assignments.
        """
        if seats is None:
            seats = load_seat_specs()
        blended_by_model: dict[int, dict[str, float]] = {
            pk: self._blended_scores(pk) for pk in self._profile_by_pk
        }
        lines = [
            f"# Seat recommendations ({len(seats)} seats from configs/seats.yaml)",
            "",
            "| seat | domain | recommended model | blended |",
            "|------|--------|-------------------|--------:|",
        ]
        for seat in seats:
            weights = _seat_capability_weights(seat)
            best: tuple[str, float] | None = None
            for pk, profile in self._profile_by_pk.items():
                blended = blended_by_model[pk]
                if not _seat_gates_ok(seat, blended):
                    continue
                score = sum(blended.get(c, 0.0) * w for c, w in weights.items())
                if best is None or score > best[1]:
                    best = (f"{profile.provider}/{profile.model_id}", score)
            code = seat.get("seat_code", "?")
            domain = seat.get("domain", "—")
            if best is None:
                lines.append(f"| {code} | {domain} | — | — |")
            else:
                lines.append(f"| {code} | {domain} | {best[0]} | {best[1]:.1f} |")
        lines += [
            "",
            "_Capability prior per model/capability: min(live, c*ref + (1-c)*70)_"
            " — see docs/en/capability-prior.md.",
        ]
        return "\n".join(lines)

    def generate_report(self) -> str:
        ts = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        lines = ["# HR Model Evaluation Report", f"Generated: {ts}", "",
                 "## Current Role Assignments", "| Role | Model | Fit Score |",
                 "|------|-------|-----------|"]
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT a.model_fk, a.role, a.fit_score, m.provider, m.model_id"
                " FROM hr_assignments a JOIN hr_models m ON m.id = a.model_fk"
                " WHERE a.is_active = TRUE ORDER BY a.role")
            active = cur.fetchall()
        if not active:
            lines.append("| _(no active assignments)_ | | |")
        for r in active:
            lines.append(f"| {r['role']} | {r['provider']}/{r['model_id']}"
                         f" | {float(r['fit_score'] or 0.0):.2f} |")
        lines += ["", "## Individual Model Scores",
                  "_General composite uses equal weights across all benchmarks"
                  " (for ranking overall). Seat assignment does NOT use this —"
                  " it lives in the unified verdict engine._",
                  "| Model | Set A (blended, general) | Set B (live-only) | Top Seats |",
                  "|-------|-------------------------:|------------------:|-----------|"]
        for pk in sorted(self._profile_by_pk):
            profile = self._profile_by_pk[pk]
            report = self.score_model(pk)
            save_report(report)
            blended = self._blended_scores(pk)
            set_b = self.live_only_composite(pk)
            bstr = ", ".join(f"{c.value}: {blended.get(c, 0.0):.1f}"
                             for c in BENCHMARK_WEIGHTS)
            seats = ", ".join(r.value for r in report.recommended_roles) or "—"
            lines.append(f"| {profile.provider}/{profile.model_id}"
                         f" | {report.overall_score:.1f}"
                         f" | {set_b:.1f}"
                         f" | {seats} |")
            lines += ["", f"### {profile.model_id} ({profile.display_name or profile.model_id})",
                      f"Benchmarks: {bstr}",
                      f"Pros: {'; '.join(report.pros) if report.pros else '—'}",
                      f"Cons: {'; '.join(report.cons) if report.cons else '—'}"]
        return "\n".join(lines)


def main() -> None:
    engine = RecommendationEngine()
    try:
        print(engine.generate_report())
    finally:
        engine._conn.close()


if __name__ == "__main__":
    main()
