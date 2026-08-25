"""Declarative seat health thresholds, weights, and hard vetoes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .rolespec import SEAT_CODES


GateLevel = Literal["strict", "moderate", "lenient"]


class HealthThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loop_mean_max: float | None = None
    truncation_max: float | None = None
    unanimity_min: float | None = None
    completion_min: float | None = None


GATES: dict[GateLevel, HealthThresholds] = {
    "strict": HealthThresholds(
        loop_mean_max=0.10,
        truncation_max=0.05,
        unanimity_min=0.90,
        completion_min=0.80,
    ),
    "moderate": HealthThresholds(
        loop_mean_max=0.15,
        truncation_max=0.08,
        completion_min=0.70,
    ),
    "lenient": HealthThresholds(loop_mean_max=0.20, truncation_max=0.10),
}

SEAT_HEALTH_GATE: dict[str, GateLevel] = {
    "oracle": "strict",
    "ultrabrain": "strict",
    "metis": "strict",
    "momus": "strict",
    "writing": "strict",
    "librarian": "strict",
    "prometheus": "strict",
    "deep": "moderate",
    "unspecified_high": "moderate",
    "visual_engineering": "moderate",
    "artistry": "moderate",
    "multimodal_looker": "moderate",
    "hephaestus": "moderate",
    "sisyphus_junior": "moderate",
    "explore": "lenient",
    "quick": "lenient",
    "atlas": "lenient",
    "unspecified_low": "lenient",
}

ROLE_HEALTH_WEIGHTS: dict[str, dict[str, float]] = {
    "oracle": {"loop": 0.20, "truncation": 0.10, "efficiency": 0.10, "completion": 0.15},
    "ultrabrain": {"loop": 0.20, "truncation": 0.10, "efficiency": 0.10, "completion": 0.15},
    "metis": {"loop": 0.25, "truncation": 0.10, "efficiency": 0.10, "completion": 0.15},
    "momus": {"loop": 0.25, "truncation": 0.10, "efficiency": 0.10, "completion": 0.15},
    "prometheus": {"loop": 0.20, "truncation": 0.10, "efficiency": 0.10, "completion": 0.15},
    "writing": {"loop": 0.20, "truncation": 0.05, "efficiency": 0.10, "completion": 0.30},
    "librarian": {"loop": 0.15, "truncation": 0.10, "efficiency": 0.30, "completion": 0.15},
    "deep": {"loop": 0.20, "truncation": 0.25, "efficiency": 0.15, "completion": 0.15},
    "hephaestus": {"loop": 0.20, "truncation": 0.25, "efficiency": 0.15, "completion": 0.15},
    "sisyphus_junior": {"loop": 0.20, "truncation": 0.25, "efficiency": 0.15, "completion": 0.15},
    "artistry": {"loop": 0.20, "truncation": 0.10, "efficiency": 0.15, "completion": 0.15},
    "visual_engineering": {"loop": 0.20, "truncation": 0.10, "efficiency": 0.15, "completion": 0.15},
    "multimodal_looker": {"loop": 0.20, "truncation": 0.10, "efficiency": 0.15, "completion": 0.15},
    "unspecified_high": {"loop": 0.30, "truncation": 0.10, "efficiency": 0.20, "completion": 0.15},
    "explore": {"loop": 0.20, "truncation": 0.05, "efficiency": 0.50, "completion": 0.05},
    "quick": {"loop": 0.20, "truncation": 0.05, "efficiency": 0.50, "completion": 0.05},
    "atlas": {"loop": 0.20, "truncation": 0.05, "efficiency": 0.50, "completion": 0.05},
    "unspecified_low": {"loop": 0.20, "truncation": 0.05, "efficiency": 0.50, "completion": 0.05},
}

ROLE_HARD_VETOS: dict[str, dict[str, float]] = {
    "oracle": {"loop_max": 0.15},
    "ultrabrain": {"loop_max": 0.15},
    "metis": {"loop_max": 0.15, "truncation_max": 0.15},
    "momus": {"loop_max": 0.15, "truncation_max": 0.15},
    "prometheus": {"loop_max": 0.15, "truncation_max": 0.15},
    "writing": {"loop_max": 0.15},
    "librarian": {"loop_max": 0.15, "truncation_max": 0.20},
    "deep": {"loop_max": 0.15, "truncation_max": 0.10},
    "hephaestus": {"loop_max": 0.15, "truncation_max": 0.10},
    "sisyphus_junior": {"loop_max": 0.15, "truncation_max": 0.10},
    "artistry": {"loop_max": 0.15},
    "visual_engineering": {"loop_max": 0.15},
    "multimodal_looker": {"loop_max": 0.15},
    "unspecified_high": {"loop_max": 0.15},
    "explore": {"loop_max": 0.15},
    "quick": {"loop_max": 0.15},
    "atlas": {"loop_max": 0.15},
    "unspecified_low": {"loop_max": 0.15},
}

assert set(SEAT_HEALTH_GATE) == set(SEAT_CODES)
assert set(ROLE_HEALTH_WEIGHTS) == set(SEAT_CODES)
assert set(ROLE_HARD_VETOS) == set(SEAT_CODES)
