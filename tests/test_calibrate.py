"""Adapter smoke and fake-adapter tests for calibration.

The FakeAdapter returns canned :class:`ModelResponse` instances so the
calibration pipeline can be exercised without any live API calls. A
separate one-shot cheap-model smoke test is exercised only when the
``HR_ADAPTER_LIVE_SMOKE`` env var is set.
"""

from __future__ import annotations


from dataclasses import dataclass, field

from pathlib import Path

from typing import Any


from hr.adapters.base import Capabilities

from hr.graders.base import ModelResponse

@dataclass
class FakeAdapter:
    """A canned-response adapter used by the calibration tests."""

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
        self.call_log.append({
            "model_id": model_id,
            "messages": messages,
            "images": images,
            "tools": tools,
            "thinking_budget": thinking_budget,
            "max_output": max_output,
        })
        return ModelResponse(
            text=self.canned_text,
            thinking=self.canned_thinking,
            tool_calls=list(self.canned_tool_calls),
            latency_ms=self.canned_latency_ms,
            tokens_in=self.canned_tokens_in,
            tokens_out=self.canned_tokens_out,
        )

ITEM_REPO = Path(__file__).resolve().parents[1] / "itemrepo"

class _CalibrationCursor:
    def __init__(self, rows: list[tuple[object, ...]] | None = None) -> None:
        self.rows = rows or []
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        self.executed.append((sql, params))

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows

    def __enter__(self) -> "_CalibrationCursor":
        return self

    def __exit__(self, *_: object) -> None:
        return None

class _CalibrationConnection:
    def __init__(self, rows: list[tuple[object, ...]] | None = None) -> None:
        self.cursor_ = _CalibrationCursor(rows)
        self.commits = 0

    def cursor(self) -> _CalibrationCursor:
        return self.cursor_

    def commit(self) -> None:
        self.commits += 1

    def __enter__(self) -> "_CalibrationConnection":
        return self

    def __exit__(self, *_: object) -> None:
        return None

class _CalibrationDatabase:
    def __init__(self, connection: _CalibrationConnection) -> None:
        self.connection = connection

    def connect(self) -> _CalibrationConnection:
        return self.connection
