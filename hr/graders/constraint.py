"""Constraint DSL grader.

DSL shape (checks[]):
  - name: str
  - extract: { jsonpath?: str, regex?: str }
      jsonpath — minimal subset (only $.dotted.path, $.a[*].b, $.a[0].b).
      regex — applied to the extracted text (or response text if no jsonpath).
  - assert: one of
      numeric_eq:  { value: num, tolerance: num }
      contains_all: [str, ...]
      not_contains: [str, ...]
      regex_match: str  (regex to match against the extracted text)
  - weight: float (default 1.0)

Per-checkpoint scoring: each check is 0 or 1 (pass/fail). Final score is the
weighted average over checkpoints with non-zero weight (no penalty for
skipped weights).
"""

from __future__ import annotations

import re
from typing import Any

from hr.graders.base import (
    GradeResult,
    GraderError,
    ModelResponse,
)
from hr.graders.constraint_dsl import _apply_assert, _extract, _parse_path, _walk


# ---------------------------------------------------------------------------
# ConstraintGrader
# ---------------------------------------------------------------------------
class ConstraintGrader:
    """Spec 附录A: DSL check list over extraction + assertion.

    Tolerant of three item-side check shapes (reconciled with actual itemrepo
    contents — framework-only alignment; items are read-only):

    1. **Full form** (legacy): ``{name, extract, assert, weight}``.
    2. **Simplified form** (reasoning items): ``{kind, value}``, where kind
       is one of ``numeric_eq``, ``contains_all``, ``regex_match``.
       Translated internally to ``{name: <kind>, extract: {}, assert: {<kind>: ...}}``.
    3. **Named form** (unanswerable / factuality_qa / citation): a bare
       string like ``"must_not_fabricate"`` or
       ``"required_claims_present_in_curated_db"``, dispatched to
       ``_evaluate_named_check`` with payload-aware heuristics.
    """

    name = "constraint"
    version = "1.0"

    def grade(
        self,
        item_payload: dict[str, Any],
        grading_params: dict[str, Any],
        response: ModelResponse,
    ) -> GradeResult:
        checks = grading_params.get("checks")
        if not isinstance(checks, list):
            raise GraderError(
                "constraint grader expects params.checks to be a list"
            )

        per_check: list[dict[str, Any]] = []
        total_weight = 0.0
        weighted_score = 0.0
        for c in checks:
            evaluated = self._evaluate_one(c, item_payload, response)
            p = bool(evaluated["passed"])
            w = float(evaluated.get("weight", 1.0))
            per_check.append({
                "name": evaluated.get("name", ""),
                "passed": p,
                "weight": w,
            })
            total_weight += w
            if p:
                weighted_score += w

        if total_weight <= 0:
            score = 0.0
        else:
            score = weighted_score / total_weight

        return GradeResult(
            score=score,
            passed=score == 1.0,
            detail={"checks": per_check, "total_weight": total_weight},
        )

    # ------------------------------------------------------------------
    # Single-check dispatcher
    # ------------------------------------------------------------------
    def _evaluate_one(
        self,
        check: Any,
        item_payload: dict[str, Any],
        response: ModelResponse,
    ) -> dict[str, Any]:
        if isinstance(check, str):
            return self._evaluate_named_check(
                check, item_payload, response
            )
        if not isinstance(check, dict):
            raise GraderError(f"check must be str or dict: {check!r}")

        name = check.get("name") or check.get("kind") or ""
        weight = float(check.get("weight", 1.0))

        # Simplified {kind, value} form (reasoning items).
        if "kind" in check and "assert" not in check:
            kind = check["kind"]
            value = check.get("value")
            if kind == "numeric_eq":
                assertion: dict[str, Any] = {
                    "numeric_eq": {"value": value}
                }
            elif kind == "contains_all":
                assertion = {"contains_all": list(value or [])}
            elif kind == "regex_match":
                assertion = {"regex_match": str(value or "")}
            else:
                assertion = {kind: value}

            extract_spec: dict[str, Any] = check.get("extract") or {}
            # Default extraction: response.text when no explicit extract.
            if not extract_spec and kind != "numeric_eq":
                # Numeric path is tolerant of raw text containing a number.
                pass
            extracted = _extract(response, item_payload, extract_spec)
            passed = _apply_assert(assertion, extracted)
            return {"name": name or kind, "passed": passed, "weight": weight}

        # Full form {name, extract, assert, weight}.
        extract_spec = check.get("extract") or {}
        full_assertion = check.get("assert")
        if not isinstance(full_assertion, dict) or not full_assertion:
            raise GraderError(f"check {name!r}: missing 'assert'")
        extracted = _extract(response, item_payload, extract_spec)
        passed = _apply_assert(full_assertion, extracted)
        return {"name": name, "passed": passed, "weight": weight}

    # ------------------------------------------------------------------
    # Named-check handlers (unanswerable / factuality_qa / citation)
    # ------------------------------------------------------------------
    # The exact phrasing set used to detect "the model is declining /
    # refusing". These are case-folded; both English and Chinese hints are
    # included because the item banks include CJK phrasing.
    _REFUSAL_PHRASES: tuple[str, ...] = (
        "cannot answer",
        "can't answer",
        "does not exist",
        "不存在",
        "没有这段",
        "无法回答",
        "没有提及",
        "没有明确",
        "前提不成立",
        "虚构",
        "不真实",
        "无法确认",
        "不确定",
        "no evidence",
        "not mentioned",
        "无此记载",
        "书中没有",
        "小说中不存在",
        "从未",
    )

    def _evaluate_named_check(
        self,
        name: str,
        item_payload: dict[str, Any],
        response: ModelResponse,
    ) -> dict[str, Any]:
        text = (response.text or "").strip()
        text_fold = text.casefold()

        if name in ("must_not_fabricate", "must_identify_unanswerability"):
            acceptable = [
                str(a).casefold()
                for a in (item_payload.get("acceptable") or [])
                if str(a).strip()
            ]
            refusal_hit = any(p in text_fold for p in self._REFUSAL_PHRASES)
            acceptable_hit = bool(
                acceptable and any(a in text_fold for a in acceptable)
            )
            # "Must identify": explicit refusal OR mention of an acceptable frame.
            if name == "must_identify_unanswerability":
                passed = refusal_hit or acceptable_hit
                return {"name": name, "passed": passed, "weight": 1.0}
            # "Must not fabricate": same gate — if it declines, no fabrication.
            # If it asserts a positive factual answer without declining, fail.
            passed = refusal_hit or acceptable_hit or not text
            return {"name": name, "passed": passed, "weight": 1.0}

        if name == "answer_exact_or_semantic":
            expected = str(item_payload.get("verifiable_answer", ""))
            exp_fold = expected.casefold().strip()
            if not exp_fold:
                return {"name": name, "passed": not text, "weight": 1.0}
            if exp_fold in text_fold:
                passed = True
            else:
                # Token-level semantic fallback: split on '/', commas, spaces.
                tokens = [
                    t.strip()
                    for t in re.split(r"[/,，、\s]+", expected)
                    if len(t.strip()) >= 2
                ]
                passed = bool(tokens) and all(t in text_fold for t in tokens)
            return {"name": name, "passed": passed, "weight": 1.0}

        if name == "required_claims_present_in_curated_db":
            claims = list(item_payload.get("required_claims") or [])
            if not claims:
                return {"name": name, "passed": True, "weight": 1.0}
            all_present = all(
                str(c).casefold().strip() in text_fold for c in claims
            )
            return {"name": name, "passed": all_present, "weight": 1.0}

        # Unknown named check — report as failed but don't raise; the
        # calibration report will surface the miss cleanly.
        return {"name": name, "passed": False, "weight": 1.0}


# Exported helpers for tests.
__all__ = [
    "ConstraintGrader",
    "_parse_path",
    "_walk",
    "_apply_assert",
    "_extract",
]
