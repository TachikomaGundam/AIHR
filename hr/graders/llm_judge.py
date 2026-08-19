"""hr2.graders.llm_judge — LLM judge STUB per spec §6.3.

Spec rules (STUB only — no live calls in this build):
  1. Pool-external, fixed, versioned model.
  2. Blind — model does not see identity; item_key masked.
  3. Position-swap 50% 降权 for ranking disagreements on tiebreakers.
  4. rubric_ref versioned (string).
  5. 10% sample double-judge κ check (not implemented — see §6.3 note).

This grader implements the interface — `blind`, `position_swap`,
`rubric_ref`, `rubric_version`. Invoking `grade()` raises `NotConfigured`
unless a `judge_adapter` (callable (prompt, payload) -> str) is provided.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

from hr.graders.base import (
    GradeResult,
    Grader,
    GraderError,
    ModelResponse,
)


class NotConfigured(RuntimeError):
    """Raised when the LLM judge is invoked without a configured adapter."""


@runtime_checkable
class JudgeAdapter(Protocol):
    def __call__(self, *, prompt: str, payload: dict[str, Any]) -> str: ...


def _build_rubric_prompt(
    *,
    rubric_ref: str,
    rubric_version: str,
    blind: bool,
    position_swap: bool,
    item_payload: dict[str, Any],
    response: ModelResponse,
) -> str:
    """Compose the prompt text for the judge. Stubbed — kept deterministic."""
    q = item_payload.get("question") or item_payload.get("task") or ""
    a = response.text or ""
    swap_note = ""
    if position_swap:
        swap_note = (
            "\n[NOTE: this comparison is a position-swap tiebreak; "
            "disagreements are discounted 50%.]\n"
        )
    return (
        f"rubric_ref:{rubric_ref}\n"
        f"rubric_version:{rubric_version}\n"
        f"blind:{blind}\n"
        f"{swap_note}"
        f"--- QUESTION ---\n{q}\n"
        f"--- RESPONSE ---\n{a}\n"
        f"--- END ---\n"
    )


class LLMJudgeGrader:
    """LLM judge stub — raises NotConfigured unless adapter supplied."""

    name = "llm_judge"
    version = "1.0"

    def __init__(self, *, judge_adapter: JudgeAdapter | None = None) -> None:
        self._adapter = judge_adapter

    def grade(
        self,
        item_payload: dict[str, Any],
        grading_params: dict[str, Any],
        response: ModelResponse,
    ) -> GradeResult:
        if self._adapter is None:
            raise NotConfigured(
                "llm_judge requires a configured JudgeAdapter; "
                "no model-API calls are made by this stub"
            )
        rubric_ref = str(
            grading_params.get("rubric_ref") or "default_rubric"
        )
        rubric_version = str(
            grading_params.get("rubric_version") or "1.0"
        )
        blind = bool(grading_params.get("blind", True))
        position_swap = bool(grading_params.get("position_swap", False))
        prompt = _build_rubric_prompt(
            rubric_ref=rubric_ref,
            rubric_version=rubric_version,
            blind=blind,
            position_swap=position_swap,
            item_payload=item_payload,
            response=response,
        )
        verdict_text = self._adapter(prompt=prompt, payload=item_payload)
        # Attempt to parse a numeric score from the verdict text.
        import re as _re

        m = _re.search(r"\b(0(?:\.\d+)?|1(?:\.0*)?)\b", verdict_text)
        score = float(m.group(1)) if m else 0.5
        return GradeResult(
            score=score,
            passed=score >= grading_params.get("pass_threshold", 0.6),
            detail={"verdict_text": verdict_text[:1000]},
        )


__all__ = ["LLMJudgeGrader", "NotConfigured", "JudgeAdapter"]

# Silence warning.
_ = (Callable, Grader, GraderError)
