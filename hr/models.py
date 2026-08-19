from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RoleType(StrEnum):
    oracle = "oracle"
    explore = "explore"
    librarian = "librarian"
    ultrabrain = "ultrabrain"
    deep = "deep"
    quick = "quick"
    writing = "writing"
    artistry = "artistry"
    visual_engineering = "visual_engineering"
    metis = "metis"
    momus = "momus"
    sisyphus_junior = "sisyphus_junior"
    multimodal_looker = "multimodal_looker"
    atlas = "atlas"
    hephaestus = "hephaestus"
    prometheus = "prometheus"
    unspecified_low = "unspecified_low"
    unspecified_high = "unspecified_high"


class BenchmarkCategory(StrEnum):
    code_gen = "code_gen"
    reasoning = "reasoning"
    instruction_follow = "instruction_follow"
    speed = "speed"
    vision = "vision"
    tool_use = "tool_use"
    long_context = "long_context"
    attention_probe = "attention_probe"
    attention_stress = "attention_stress"
    long_horizon = "long_horizon"


class ModelProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    model_id: str
    display_name: str = ""
    context_window: int | None = None
    max_output: int | None = None
    supports_vision: bool = False
    supports_thinking: bool = False
    api_base_url: str = ""
    notes: str = ""


class BenchmarkResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_fk: int
    benchmark_name: BenchmarkCategory
    score: float
    latency_ms: int | None = None
    tokens_per_sec: float | None = None
    raw_output: str = ""
    passed: bool = False


class ResearchFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_fk: int
    source_url: str = ""
    finding: str
    category: str = ""
    confidence: float | None = None


class RoleAssignment(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_fk: int
    role: RoleType
    fit_score: float | None = None
    rationale: str = ""
    is_active: bool = False


class EvaluationReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_fk: int
    overall_score: float | None = None
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    recommended_roles: list[RoleType] = Field(default_factory=list)
    summary: str = ""
