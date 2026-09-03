"""Stage 0 tests using a fake adapter and no live API calls."""

from __future__ import annotations

import json

from dataclasses import dataclass, field

from pathlib import Path

from typing import Any

import pytest

import hr.stage0 as stage0  # noqa: F401 (re-export; consumed by sibling test modules)

from hr.adapters.base import Capabilities

from hr.fleet import fleet_models  # noqa: F401 (re-export; consumed by sibling test modules)

from hr.graders.base import ModelResponse

from hr.stage0 import (
    STAGE0_BATTERIES,  # noqa: F401 (re-export; consumed by sibling test modules)
    STAGE0_SUBSET_SIZES,  # noqa: F401 (re-export; consumed by sibling test modules)
    CallPlan,  # noqa: F401 (re-export; consumed by sibling test modules)
    SweepState,  # noqa: F401 (re-export; consumed by sibling test modules)
    build_call_plan,  # noqa: F401 (re-export; consumed by sibling test modules)
    call_and_grade,  # noqa: F401 (re-export; consumed by sibling test modules)
    compute_pool_hash,  # noqa: F401 (re-export; consumed by sibling test modules)
    run_sweep,  # noqa: F401 (re-export; consumed by sibling test modules)
    select_subsets,  # noqa: F401 (re-export; consumed by sibling test modules)
    _bootstrap_separation_from_state,  # noqa: F401 (re-export; consumed by sibling test modules)
    _ensure_provider_model_records,  # noqa: F401 (re-export; consumed by sibling test modules)
    _print_matrix,  # noqa: F401 (re-export; consumed by sibling test modules)
)

ITEM_REPO = Path(__file__).resolve().parents[1] / "itemrepo"

@pytest.fixture
def fleet_env(hr_sandbox: dict) -> None:
    """Isolate the dynamic fleet: fake opencode config, empty extras tree.

    fleet_models() derives from OPENCODE_CONFIG_DIR + HR_HOME at call time —
    without this fixture the REAL ~/.config/opencode would be read.
    """
    config_dir = hr_sandbox["config_dir"]
    (config_dir / "opencode.jsonc").write_text(
        json.dumps(
            {
                "provider": {
                    "acme-ai": {
                        "npm": "@ai-sdk/openai-compatible",
                        "models": {"flash": {}, "pro": {}, "plus": {}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

@dataclass
class FakeAdapter:
    canned_text: str = ""
    canned_thinking: str = ""
    canned_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    canned_tokens_in: int = 100
    canned_tokens_out: int = 50
    canned_latency_ms: int = 10
    thinking_models: set[str] = field(default_factory=set)
    call_log: list[dict[str, Any]] = field(default_factory=list)
    raise_: Exception | None = None

    def probe_capabilities(self, model_id: str) -> Capabilities:
        base = model_id.split("/", 1)[-1] if "/" in model_id else model_id
        provider = model_id.split("/", 1)[0] if "/" in model_id else ""
        return Capabilities(
            model_id=model_id,
            provider=provider,
            supports_thinking=base in self.thinking_models,
            supports_vision=True,
        )

    def chat(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        images: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        thinking_budget: int | None = None,
        max_output: int = 16384,
        timeout_s: int = 600,
    ) -> ModelResponse:
        if self.raise_ is not None:
            raise self.raise_
        self.call_log.append(
            {
                "model_id": model_id,
                "messages": messages,
                "images": images,
                "tools": tools,
                "thinking_budget": thinking_budget,
                "max_output": max_output,
            }
        )
        return ModelResponse(
            text=self.canned_text,
            thinking=self.canned_thinking,
            tool_calls=list(self.canned_tool_calls),
            latency_ms=self.canned_latency_ms,
            tokens_in=self.canned_tokens_in,
            tokens_out=self.canned_tokens_out,
        )
