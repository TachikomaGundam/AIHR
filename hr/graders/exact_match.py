"""hr2.graders.exact_match — simple normalized exact match + tolerance.

Normalization rules (in order):
  1. str → strip, collapse whitespace, casefold (for text comparisons)
  2. numeric extraction — if both expected and actual contain a parseable
     number, compare them with `numeric_tolerance` from params.
  3. unit stripping — removes trailing SI units ("mg", "kg", "s", "ms", "%")
     when present on one side only (normalizes to the value).
  4. boolean coercion — "true"/"yes"/"1" → True
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from hr.graders.base import (
    GradeResult,
    GraderError,
    ModelResponse,
)

# Numbers with optional sign, scientific notation.
_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
# Common units to strip.
_UNIT_RE = re.compile(
    r"\s*(mg|g|kg|ml|l|s|ms|min|h|cm|m|km|mm|%|us|ns|Hz|kHz|MHz|GHz|°C|°F|K)\s*$",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")
_TRUTHY = {"true", "yes", "1", "t", "y"}
_FALSY = {"false", "no", "0", "f", "n"}


def _normalize_unit(value: Any) -> str:
    if not isinstance(value, str):
        return value
    value = value.strip()
    value = _UNIT_RE.sub("", value).strip()
    return value


def _normalize(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if value is None:
        return None
    text = str(value)
    text = unicodedata.normalize("NFKC", text)
    text = _normalize_unit(text)
    text = _WHITESPACE_RE.sub(" ", text).strip().casefold()
    if not text:
        return text

    # Boolean coercion.
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False

    # Try numeric — but only if the WHOLE string is a number.
    m = _NUMBER_RE.fullmatch(text)
    if m:
        try:
            return int(text)
        except ValueError:
            return float(text)

    return text


def _extract_number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        m = _NUMBER_RE.search(value)
        if m:
            try:
                return float(m.group(0))
            except ValueError:
                return None
    return None


def _equal(expected: Any, actual: Any, tol: float) -> bool:
    en = _normalize(expected)
    an = _normalize(actual)
    if en == an:
        return True
    # _normalize already turns a purely numeric expected into int/float, so a
    # numeric `en` means the answer IS a number. Compare it against EVERY
    # numeric literal in the response — models show working before the final
    # answer, so the first literal is rarely the answer; and never substring-
    # match a bare number ("5" would false-positive inside "5050").
    if isinstance(en, (int, float)) and not isinstance(en, bool):
        ef = float(en)
        candidates: list[float] = []
        if isinstance(an, (int, float)) and not isinstance(an, bool):
            candidates.append(float(an))
        else:
            for m in _NUMBER_RE.finditer(str(actual)):
                candidates.append(float(m.group(0)))
        denom = max(abs(ef), 1e-9)
        return any(abs(ef - af) / denom <= tol for af in candidates)
    # Text expected: containment — the model may wrap the answer in a sentence
    # ("the disabled button is Stop"), so full-string equality is too strict.
    if isinstance(en, str) and isinstance(an, str) and en:
        if en in an:
            return True
        # Short alphanumeric tokens: word-boundary containment to avoid
        # partials ("art" matching "chart").
        if len(en) <= 12 and re.search(r"(?<![a-z0-9])" + re.escape(en) + r"(?![a-z0-9])", an):
            return True
    return False


def _split_alternatives(s: str) -> list[str]:
    """``/`` or ``／`` separates ALTERNATIVE acceptable phrasings (any one)."""
    return [p.strip() for p in re.split(r"\s*/\s*|／", s) if p.strip()]


def _split_required(s: str) -> list[str]:
    """Comma / ``，`` / ``、`` / ``and`` / ``和`` / ``与`` / ``+`` separate
    REQUIRED components of one answer (all must be present)."""
    parts = re.split(r"[,，、]\s*|\s+(?:and|和|与)\s+|\s*\+\s*", s)
    return [p.strip() for p in parts if p.strip()]


def _component_present(comp: str, an_text: str, actual_raw: str, tol: float) -> bool:
    cn = _normalize(comp)
    # Numeric component: match against every numeric literal in the response.
    if isinstance(cn, (int, float)) and not isinstance(cn, bool):
        ef = float(cn)
        for m in _NUMBER_RE.finditer(actual_raw):
            if abs(ef - float(m.group(0))) / max(abs(ef), 1e-9) <= tol:
                return True
        return False
    # Text component: containment, then word-boundary for short tokens.
    if isinstance(cn, str) and cn:
        if cn in an_text:
            return True
        if len(cn) <= 12 and re.search(
            r"(?<![a-z0-9])" + re.escape(cn) + r"(?![a-z0-9])", an_text
        ):
            return True
    return False


def _score_structured(
    expected: Any, answer_keys: Any, actual: str, tol: float
) -> float:
    """Grade a possibly-structured answer by required components.

    Handles three answer-key shapes that recur across the banks:
      * ``answer_keys: [k1, k2]``            -> ALL keys required
      * ``"A, B"`` / ``"A 和 B"`` / ``"A+B"`` -> ALL components required
      * ``"X / Y / Z"``                      -> ANY one alternative suffices
      * ``"56"`` / ``"铜"``                  -> single key (numeric or text)
    Returns a fraction in [0,1] of the best-matching alternative group, so a
    partially-correct multi-part answer earns partial credit instead of 0.
    """
    an = _normalize(actual)
    an_text = an if isinstance(an, str) else str(actual)
    if isinstance(answer_keys, (list, tuple)) and answer_keys:
        groups = [list(answer_keys)]
    else:
        groups = [_split_required(alt) for alt in _split_alternatives(str(expected))]
    best = 0.0
    for group in groups:
        if not group:
            continue
        hits = sum(
            1 for comp in group if _component_present(str(comp), an_text, str(actual), tol)
        )
        best = max(best, hits / len(group))
    return best


class ExactMatchGrader:
    """Spec §6.2 grader: exact_match."""

    name = "exact_match"
    version = "1.0"

    def grade(
        self,
        item_payload: dict[str, Any],
        grading_params: dict[str, Any],
        response: ModelResponse,
    ) -> GradeResult:
        # Expected answer can live under "expected" in params or "answer"/
        # "verifiable_answer" in payload, depending on payload type.
        expected = grading_params.get("expected")
        if expected is None:
            expected = item_payload.get("answer")
        if expected is None:
            expected = item_payload.get("verifiable_answer")
        answer_keys = item_payload.get("answer_keys") or grading_params.get("answer_keys")
        if expected is None and not answer_keys:
            raise GraderError(
                "exact_match: no expected answer provided "
                "(via params or payload)"
            )

        actual = response.text or ""
        tol = float(grading_params.get("numeric_tolerance", 0.0))
        case_sensitive = bool(grading_params.get("case_sensitive", False))

        if case_sensitive:
            # Skip casefold; re-normalize without casefold.
            exp_n = _normalize_unit(str(expected))
            act_n = _normalize_unit(str(actual).strip())
            score = 1.0 if exp_n.strip() == act_n.strip() else 0.0
        else:
            score = _score_structured(expected, answer_keys, actual, tol)

        detail = {
            "expected_type": type(expected).__name__ if expected is not None else "answer_keys",
            "actual_type": type(actual).__name__,
            "tolerance": tol,
            "components_required": (
                answer_keys if answer_keys else _split_required(str(expected))
            ),
        }
        detail["expected_normalized"] = _normalize(expected) if expected is not None else None
        detail["actual_normalized"] = _normalize(actual)
        return GradeResult(
            score=score,
            passed=score >= 1.0 - 1e-9,
            detail=detail,
        )
