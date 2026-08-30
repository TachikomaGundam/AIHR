"""Adapter protocol and common types (spec §7).

Defines the Adapter contract consumed by :mod:`hr.calibrate`:

  - :class:`Capabilities`: per-model capability profile (thinking, vision).
  - :class:`ChatRequest`: a structured chat request.
  - :class:`Adapter` protocol: endpoint resolution, capability probing,
    and a single ``chat()`` method returning a :class:`ModelResponse`.

Concrete adapters live alongside — the only one in use at this stage is
:class:`hr.adapters.anthropic_compat.AnthropicCompatAdapter`, which
wraps the two ``x-api-key`` + ``anthropic-version`` gateways used in
v1 (bailian-token-plan and kimi-for-coding).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from hr.graders.base import ModelResponse


@dataclass(frozen=True)
class Capabilities:
    """Per-model capability profile.

    Mirrors the minimal shape needed by calibration: does the model
    accept a ``thinking`` block, and does it take images (vision).
    ``supports_thinking`` false means the adapter MUST NOT inject a
    ``thinking`` block, regardless of caller request.
    """

    model_id: str
    provider: str
    api_base_url: str | None = None
    supports_thinking: bool = False
    supports_vision: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatRequest:
    """A structured chat request (kept minimal; adapters may ignore extras).

    ``tools`` carries tool definitions in the shared item-bank shape
    (``{"name", "description", "schema"|"input_schema"}``); each concrete
    adapter translates them to its own wire format. There is deliberately NO
    temperature field — every consumer (bench, stage0/1) runs temperature-free.
    """

    model_id: str
    messages: list[dict[str, Any]]
    images: list[dict[str, Any]] | None = None
    tools: list[dict[str, Any]] | None = None
    thinking_budget: int | None = None
    max_output: int = 16384
    timeout_s: int = 600


@runtime_checkable
class Adapter(Protocol):
    """Spec §7: adapter contract.

    Implementations are responsible for translating ``model_id`` into
    endpoint + headers, honouring rate limits, and streaming where
    required. They must classify transport failures via
:func:`hr.scheduler.taxonomy.classify_failure` and raise a
    :class:`AdapterError` carrying the descriptor on terminal failure.
    """

    def probe_capabilities(self, model_id: str) -> Capabilities:
        ...

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
        ...


class AdapterError(RuntimeError):
    """Raised when an adapter exhausts retries for a request.

    Carries the classifier output in ``failure`` so callers (notably
    calibration) can record structured failure rows without having to
    re-classify from a raw exception string.
    """

    def __init__(
        self,
        message: str,
        *,
        failure: Any | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.failure = failure
        self.status_code = status_code


__all__ = [
    "Adapter",
    "AdapterError",
    "Capabilities",
    "ChatRequest",
    "ModelResponse",
]
