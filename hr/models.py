from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


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
