from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from hr.bench.scorers import _BenchmarkOutcome
from hr.bench.scorer_shared import BenchmarkStatus
from hr.models import BenchmarkCategory

@dataclass(frozen=True)
class ItemResult:
    """One graded unit of a battery run."""

    label: str
    item_id: str
    score: float  # 0..100 (100/0 for binary units, v1 score for graded ones)
    passed: bool


@dataclass
class BenchOutcome:
    """Result of one model and battery run, ready for storage."""

    battery: BenchmarkCategory
    model_id: str
    score: float
    passed: bool
    status: BenchmarkStatus = "scored"
    items: list[ItemResult] = field(default_factory=list)
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    raw_output: str = ""
    response_text: str = ""
    thinking_text: str = ""
    requested_max_output: int = 16384


@dataclass(frozen=True)
class _RunResult:
    """Scored outcome and response metadata for measurement rows."""

    outcome: _BenchmarkOutcome
    response_text: str = ""
    thinking_text: str = ""
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    requested_max_output: int = 16384


def make_sweep_id() -> str:
    """One sweep per bench invocation: livebench-<ts>-<rand>."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"livebench-{ts}-{uuid.uuid4().hex[:6]}"

__all__ = ["BenchOutcome", "ItemResult", "_RunResult", "make_sweep_id"]
