from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hr.graders.base import GradeResult
from hr.items.schema import ItemEnvelope, ItemType
from hr.stage0_selection import _AdapterFacade


def _build_messages(envelope: ItemEnvelope) -> list[dict[str, Any]]:
    """Build the chat messages for an envelope — same as calibrate.build_messages."""
    from hr.calibrate import build_messages

    return build_messages(envelope)


def _maybe_vision_image(
    envelope: ItemEnvelope, item_repo: Path
) -> list[dict[str, Any]] | None:
    from hr.calibrate import maybe_vision_image

    return maybe_vision_image(envelope, item_repo)


def _build_grading_params(envelope: ItemEnvelope) -> dict[str, Any]:
    from hr.calibrate import build_grading_params

    return build_grading_params(envelope)


# ---------------------------------------------------------------------------
# Model call + grading
# ---------------------------------------------------------------------------
@dataclass
class SingleCallResult:
    """Result of calling one (model, item) pair."""

    score: float
    passed: bool
    detail: dict[str, Any]
    tokens_in: int
    tokens_out: int
    latency_ms: int
    infra_failure: str | None = None  # FailureCode name or None
    error: str | None = None
    response_text: str | None = None
    thinking_text: str | None = None
    #: False when no score-bearing observation was produced (infra failure,
    #: no routing, grader lookup/grading error). Consumers must not persist
    #: or aggregate such results as measurements (audit bug 4).
    scored: bool = True


def call_and_grade(
    adapter: _AdapterFacade,
    model_id: str,
    envelope: ItemEnvelope,
    item_repo: Path,
    registry,
) -> tuple[bool, SingleCallResult]:
    """Call the model with ``envelope`` and grade the response.

    Returns ``(ok, result)`` where ``ok`` indicates whether a clean
    response was obtained (i.e. no infra failure); ``result`` always
    carries a score (0.0 on infra failure).
    """
    messages = _build_messages(envelope)
    images = _maybe_vision_image(envelope, item_repo) if envelope.type == ItemType.VISION else None
    tools = envelope.payload.get("tools") if envelope.type == ItemType.TOOL_A else None

    # Probe capabilities (best-effort — don't fail on this).
    try:
        cap = adapter.probe_capabilities(model_id)
        supports_thinking = cap.supports_thinking
    except Exception:
        supports_thinking = False

    thinking_budget = 8192 if supports_thinking else None
    try:
        resp = adapter.chat(
            model_id,
            messages,
            images=images,
            tools=tools,
            thinking_budget=thinking_budget,
            max_output=16384,
            timeout_s=600,
        )
    except Exception as e:
        # Classify as infra failure; score = 0.

        # Best-effort classification from exception type.
        err_str = f"{type(e).__name__}: {e}".lower()
        infra = "rate_limit" if "429" in err_str else "timeout" if "timeout" in err_str else "unknown"
        return False, SingleCallResult(
            score=0.0,
            passed=False,
            detail={"infra_failure": infra, "error": str(e)},
            tokens_in=0,
            tokens_out=0,
            latency_ms=0,
            infra_failure=infra,
            error=str(e),
            scored=False,
        )

    # Grade.
    from hr.calibrate import _ROUTING

    routing = _ROUTING.get(envelope.type)
    if routing is None:
        return True, SingleCallResult(
            score=0.0,
            passed=False,
            detail={"no_routing": True},
            tokens_in=getattr(resp, "tokens_in", 0) or 0,
            tokens_out=getattr(resp, "tokens_out", 0) or 0,
            latency_ms=getattr(resp, "latency_ms", 0) or 0,
            scored=False,
        )
    grader_spec, _builder = routing
    try:
        grader = registry.get(grader_spec)
    except Exception as e:
        return True, SingleCallResult(
            score=0.0,
            passed=False,
            detail={"grader_error": str(e)},
            tokens_in=getattr(resp, "tokens_in", 0) or 0,
            tokens_out=getattr(resp, "tokens_out", 0) or 0,
            latency_ms=getattr(resp, "latency_ms", 0) or 0,
            scored=False,
        )
    params = _build_grading_params(envelope)
    try:
        g: GradeResult = grader.grade(envelope.payload, params, resp)
    except Exception as e:
        return True, SingleCallResult(
            score=0.0,
            passed=False,
            detail={"grader_error": str(e)},
            tokens_in=getattr(resp, "tokens_in", 0) or 0,
            tokens_out=getattr(resp, "tokens_out", 0) or 0,
            latency_ms=getattr(resp, "latency_ms", 0) or 0,
            scored=False,
        )
    return True, SingleCallResult(
        score=float(g.score),
        passed=bool(getattr(g, "passed", False)),
        detail=dict(g.detail) if isinstance(g.detail, dict) else {"detail": g.detail},
        tokens_in=getattr(resp, "tokens_in", 0) or 0,
        tokens_out=getattr(resp, "tokens_out", 0) or 0,
        latency_ms=getattr(resp, "latency_ms", 0) or 0,
        response_text=getattr(resp, "text", "") or None,
        thinking_text=getattr(resp, "thinking", "") or None,
    )


# ---------------------------------------------------------------------------
# Pool hash
# ---------------------------------------------------------------------------
