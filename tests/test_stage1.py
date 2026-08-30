"""Tests for Stage 1 using a fake adapter with no live API calls."""

from __future__ import annotations

import json

import os

from dataclasses import dataclass, field

from pathlib import Path

from typing import Any

import pytest



from hr.adapters.base import Capabilities

from hr.fleet import fleet_models  # noqa: F401 (re-export; consumed by sibling test modules)

from hr.graders.base import ModelResponse

from hr.stage0 import (
    STAGE0_SEAT_CODE,  # noqa: F401 (re-export; consumed by sibling test modules)
    _key,  # noqa: F401 (re-export; consumed by sibling test modules)
)

from hr.stage1 import (
    DEFAULT_THRESHOLDS_PATH,  # noqa: F401 (re-export; consumed by sibling test modules)
    STAGE1_DECIDING_BATTERIES,  # noqa: F401 (re-export; consumed by sibling test modules)
    STAGE1_SEAT_CODE,  # noqa: F401 (re-export; consumed by sibling test modules)
    STAGE1_TOKEN_CAP,  # noqa: F401 (re-export; consumed by sibling test modules)
    FinalistSelection,  # noqa: F401 (re-export; consumed by sibling test modules)
    FinalsCallPlan,  # noqa: F401 (re-export; consumed by sibling test modules)
    Stage1SweepState,  # noqa: F401 (re-export; consumed by sibling test modules)
    _bootstrap_separation_from_stage1,  # noqa: F401 (re-export; consumed by sibling test modules)
    _rebuild_stopper_from_db,  # noqa: F401 (re-export; consumed by sibling test modules)
    _run_finals_loop,  # noqa: F401 (re-export; consumed by sibling test modules)
    build_aligned_2d,  # noqa: F401 (re-export; consumed by sibling test modules)
    build_finals_plan,  # noqa: F401 (re-export; consumed by sibling test modules)
    load_full_banks,  # noqa: F401 (re-export; consumed by sibling test modules)
    run_finals,  # noqa: F401 (re-export; consumed by sibling test modules)
    select_finalists_from_stage0,  # noqa: F401 (re-export; consumed by sibling test modules)
)

ITEM_REPO = Path(__file__).resolve().parents[1] / "itemrepo"

@pytest.fixture
def fleet_env(hr_sandbox: dict) -> None:
    """Isolate the dynamic fleet (same contract as test_stage0.fleet_env)."""
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
    canned_score: float = 0.8
    canned_tokens_in: int = 100
    canned_tokens_out: int = 50
    canned_latency_ms: int = 10
    thinking_models: set[str] = field(default_factory=set)
    call_log: list[dict[str, Any]] = field(default_factory=list)
    raise_: Exception | None = None
    per_model_score: dict[str, float] = field(default_factory=dict)

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
        self.call_log.append({"model_id": model_id})
        return ModelResponse(
            text="fake response",
            thinking=None,
            tool_calls=[],
            tokens_in=self.canned_tokens_in,
            tokens_out=self.canned_tokens_out,
            latency_ms=self.canned_latency_ms,
            raw={},
        )

class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *a, **kw):
        pass

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)

    def close(self):
        pass

_LIVE_DB_ENVS = ("HR_DSN", "HR_DB_PASSWORD", "HR_COMPOSE_FILE")

def _has_live_db_credentials() -> bool:
    return any(os.environ.get(name) for name in _LIVE_DB_ENVS)

class _ResumeCursor:
    def __init__(self, result_sets: list[list[tuple]]) -> None:
        self.result_sets = iter(result_sets)
        self.rows: list[tuple] = []

    def execute(self, *_: object, **__: object) -> None:
        self.rows = next(self.result_sets)

    def fetchall(self) -> list[tuple]:
        return self.rows

    def __enter__(self) -> "_ResumeCursor":
        return self

    def __exit__(self, *_: object) -> None:
        return None

class _ResumeConnection:
    def __init__(self, result_sets: list[list[tuple]]) -> None:
        self.cursor_ = _ResumeCursor(result_sets)

    def cursor(self) -> _ResumeCursor:
        return self.cursor_
