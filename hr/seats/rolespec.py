"""Seat profile schema + inferred-marking logic (spec §8.2).

Metadata-only: no raw content, no tool arguments, no file paths, no task text.
"""

from __future__ import annotations

import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


SEAT_CODES: tuple[str, ...] = (
    "oracle",
    "ultrabrain",
    "deep",
    "quick",
    "writing",
    "artistry",
    "visual_engineering",
    "metis",
    "momus",
    "sisyphus_junior",
    "multimodal_looker",
    "atlas",
    "hephaestus",
    "prometheus",
    "explore",
    "librarian",
    "unspecified_low",
    "unspecified_high",
)

# Local opencode agent-value → seat code. Hyphen/underscore/case insensitive.
# Keys lowercased with hyphens replaced by underscores. Values in SEAT_CODES.
AGENT_TO_SEAT: dict[str, str] = {
    # Sisyphus-Junior (primary worker)
    "sisyphus_junior": "sisyphus_junior",
    "sisyphus_junior": "sisyphus_junior",
    # Sisyphus - ultraworker (legacy alias for spec seat 'ultrabrain')
    "sisyphus_ultraworker": "ultrabrain",
    "ultraworker": "ultrabrain",
    # Prometheus planner
    "prometheus_plan_builder": "prometheus",
    "prometheus - plan builder": "prometheus",
    "prometheus": "prometheus",
    # Oracle critic
    "oracle": "oracle",
    # Hephaestus deep
    "hephaestus_deep_agent": "hephaestus",
    "hephaestus - deep agent": "hephaestus",
    "hephaestus": "hephaestus",
    # Momus critic
    "momus_plan_critic": "momus",
    "momus - plan critic": "momus",
    "momus": "momus",
    # Metis consultant
    "metis_plan_consultant": "metis",
    "metis - plan consultant": "metis",
    "metis": "metis",
    # Multimodal
    "multimodal_looker": "multimodal_looker",
    "multimodal-looker": "multimodal_looker",
    # Explore / librarian (subagents)
    "explore": "explore",
    "librarian": "librarian",
}


def normalize_agent(raw: str | None) -> str | None:
    """Lowercase + collapse hyphens/spaces to underscores, trim."""
    if not raw:
        return None
    return re.sub(r"[\s\-]+", "_", raw.strip().lower()).strip("_")


def map_agent_to_seat(raw_agent: str) -> str | None:
    """Map raw agent string to seat code, or None if unmappable."""
    normalized = normalize_agent(raw_agent)
    if normalized is None:
        return None
    if normalized in AGENT_TO_SEAT:
        return AGENT_TO_SEAT[normalized]
    # Direct SEAT_CODES match
    if normalized in SEAT_CODES:
        return normalized
    return None


MIN_TASK_COUNT_FOR_LOG_DERIVED = 30  # spec §8.2: fewer -> inferred


class SeatProfile(BaseModel):
    """Schema per spec §8.2."""

    model_config = ConfigDict(extra="forbid")

    code: str
    source: Literal["logs", "inferred"] = "inferred"
    task_count: int = Field(ge=0)
    context_p50: int = 0
    context_p95: int = 0
    tool_usage: dict[str, float] = Field(default_factory=dict)
    output_form: dict[str, float] = Field(default_factory=dict)
    battery_weights: dict[str, float] = Field(default_factory=dict)
    inferred: bool = False
    notes: str = ""

    @field_validator("code")
    @classmethod
    def _check_code(cls, v: str) -> str:
        if v not in SEAT_CODES:
            raise ValueError(f"unknown seat code: {v}")
        return v


# ---------------------------------------------------------------------------
# Default, role-description-based weights for inferred seats (spec §8.2 fallback)
# ---------------------------------------------------------------------------
# Heuristic: battery_weights keys = {top_tool_fraction, longctx, reasoning,
# speed_cost, coverage} — five knobs. Defaults reflect the spirit of each role.
# Values are normalized so they sum to 1.0.


def _norm(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.values()) or 1.0
    return {k: round(v / total, 3) for k, v in weights.items()}


# Role description → default battery_weights
_DEFAULT_RAW = {
    # Long-context reasoning; reads a lot, emits focused judgment.
    "oracle": {"top_tool_fraction": 30, "longctx": 90, "reasoning": 85, "speed_cost": 40, "coverage": 50},
    # Ultraworker / heavy all-rounder; long runs, broad tool use.
    "ultrabrain": {"top_tool_fraction": 70, "longctx": 85, "reasoning": 70, "speed_cost": 60, "coverage": 80},
    # Deep worker: code-edit-heavy, long reasoning, slow.
    "deep": {"top_tool_fraction": 75, "longctx": 75, "reasoning": 75, "speed_cost": 70, "coverage": 70},
    # Quick: low-latency, short context, fast + cheap.
    "quick": {"top_tool_fraction": 40, "longctx": 20, "reasoning": 30, "speed_cost": 15, "coverage": 50},
    # Writing: prose output, medium reasoning, low tool.
    "writing": {"top_tool_fraction": 20, "longctx": 50, "reasoning": 55, "speed_cost": 30, "coverage": 40},
    # Artistry: frontend/design; look_at heavy + edit heavy.
    "artistry": {"top_tool_fraction": 65, "longctx": 40, "reasoning": 50, "speed_cost": 55, "coverage": 60},
    # Visual engineering: look_at, playwright, screenshots.
    "visual_engineering": {"top_tool_fraction": 60, "longctx": 45, "reasoning": 55, "speed_cost": 60, "coverage": 65},
    # Metis plan consultant: reasoning + read heavy.
    "metis": {"top_tool_fraction": 40, "longctx": 55, "reasoning": 85, "speed_cost": 30, "coverage": 45},
    # Momus critic: reasoning + critique, low tool breadth.
    "momus": {"top_tool_fraction": 30, "longctx": 50, "reasoning": 90, "speed_cost": 25, "coverage": 35},
    # Sisyphus-Junior: executor, broad tools, high tool density.
    "sisyphus_junior": {"top_tool_fraction": 85, "longctx": 70, "reasoning": 65, "speed_cost": 65, "coverage": 90},
    # Multimodal looker: look_at dominant.
    "multimodal_looker": {"top_tool_fraction": 50, "longctx": 30, "reasoning": 40, "speed_cost": 35, "coverage": 30},
    # Atlas (if existed): geography/data, moderate.
    "atlas": {"top_tool_fraction": 40, "longctx": 40, "reasoning": 55, "speed_cost": 40, "coverage": 50},
    # Hephaestus deep coder.
    "hephaestus": {"top_tool_fraction": 75, "longctx": 70, "reasoning": 75, "speed_cost": 65, "coverage": 75},
    # Prometheus planner: reasoning + librarian/explore delegation.
    "prometheus": {"top_tool_fraction": 45, "longctx": 65, "reasoning": 80, "speed_cost": 35, "coverage": 55},
    # Explore: grep/glob/read heavy.
    "explore": {"top_tool_fraction": 55, "longctx": 35, "reasoning": 40, "speed_cost": 30, "coverage": 60},
    # Librarian: websearch/webfetch/context7 heavy.
    "librarian": {"top_tool_fraction": 55, "longctx": 45, "reasoning": 45, "speed_cost": 40, "coverage": 55},
    # Unspecified low: minimal workload.
    "unspecified_low": {"top_tool_fraction": 25, "longctx": 25, "reasoning": 35, "speed_cost": 20, "coverage": 30},
    # Unspecified high: moderate workload.
    "unspecified_high": {"top_tool_fraction": 50, "longctx": 50, "reasoning": 55, "speed_cost": 40, "coverage": 55},
}

DEFAULT_BATTERY_BY_SEAT: dict[str, dict[str, float]] = {
    k: _norm(v) for k, v in _DEFAULT_RAW.items()
}

# Default output_form distributions per seat (from role description intuition).
# output_form keys: 'code', 'search', 'docs', 'mixed'.
_DEFAULT_OUTPUT_FORM_RAW = {
    "oracle":            {"code": 5, "search": 10, "docs": 80, "mixed": 5},
    "ultrabrain":        {"code": 40, "search": 15, "docs": 30, "mixed": 15},
    "deep":              {"code": 55, "search": 15, "docs": 20, "mixed": 10},
    "quick":             {"code": 30, "search": 15, "docs": 45, "mixed": 10},
    "writing":           {"code": 5, "search": 10, "docs": 80, "mixed": 5},
    "artistry":          {"code": 45, "search": 10, "docs": 25, "mixed": 20},
    "visual_engineering":{"code": 45, "search": 10, "docs": 20, "mixed": 25},
    "metis":             {"code": 10, "search": 15, "docs": 70, "mixed": 5},
    "momus":             {"code": 5, "search": 10, "docs": 80, "mixed": 5},
    "sisyphus_junior":   {"code": 55, "search": 15, "docs": 15, "mixed": 15},
    "multimodal_looker": {"code": 5, "search": 30, "docs": 55, "mixed": 10},
    "atlas":             {"code": 30, "search": 25, "docs": 35, "mixed": 10},
    "hephaestus":        {"code": 60, "search": 10, "docs": 20, "mixed": 10},
    "prometheus":        {"code": 10, "search": 10, "docs": 75, "mixed": 5},
    "explore":           {"code": 10, "search": 50, "docs": 35, "mixed": 5},
    "librarian":         {"code": 5, "search": 50, "docs": 40, "mixed": 5},
    "unspecified_low":   {"code": 20, "search": 10, "docs": 60, "mixed": 10},
    "unspecified_high":  {"code": 40, "search": 20, "docs": 30, "mixed": 10},
}

DEFAULT_OUTPUT_FORM_BY_SEAT: dict[str, dict[str, float]] = {
    k: _norm(v) for k, v in _DEFAULT_OUTPUT_FORM_RAW.items()
}


def build_inferred_profile(code: str, notes: str = "") -> SeatProfile:
    """Build a profile purely from the role-description defaults (no log data)."""
    if code not in SEAT_CODES:
        raise ValueError(f"unknown seat: {code}")
    return SeatProfile(
        code=code,
        source="inferred",
        task_count=0,
        context_p50=0,
        context_p95=0,
        tool_usage={},
        output_form=dict(DEFAULT_OUTPUT_FORM_BY_SEAT[code]),
        battery_weights=dict(DEFAULT_BATTERY_BY_SEAT[code]),
        inferred=True,
        notes=notes or "inferred from role description; no log data meeting min-count threshold",
    )
