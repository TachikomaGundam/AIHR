"""hr2.graders.citation — verify source claims + curated_sources coverage.

Spec §6.2: a `citation` payload has required_claims and a source_db. The
grader checks:
  1. All required_claims[] are addressed in the response (marker-based:
     each claim string OR a simple alias must appear as an [N] citation
     pointing to a real file in curated_sources/).
  2. Every cited file [N] must exist in curated_sources/.
  3. Coverage ratio = addressed claims / total claims.

No external dependencies — marker parsing uses a regex over `[N]` tokens.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable

from hr.graders.base import (
    GradeResult,
    Grader,
    GraderError,
    ModelResponse,
)

# Matches bracketed integer citation like [1] or [3,7] or [1-3].
_CITE_RE = re.compile(r"\[(\d+(?:[\s,\-–]\d+)*)\]")


def _expand_cite(token: str) -> list[int]:
    """Expand "1,2,4-6" -> [1,2,4,5,6]."""
    out: list[int] = []
    for part in token.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part or "–" in part:
            lo, hi = re.split(r"[-–]", part, maxsplit=1)
            try:
                lo_i, hi_i = int(lo), int(hi)
            except ValueError:
                continue
            out.extend(range(lo_i, hi_i + 1))
        else:
            try:
                out.append(int(part))
            except ValueError:
                continue
    return out


def _parse_cites(text: str) -> list[int]:
    cites: list[int] = []
    for m in _CITE_RE.finditer(text):
        cites.extend(_expand_cite(m.group(1)))
    return sorted(set(cites))


class CitationGrader:
    """Spec §6.2 citation grader."""

    name = "citation"
    version = "1.0"

    def __init__(self, *, sources_dir: str | Path | None = None) -> None:
        self.sources_dir = Path(sources_dir) if sources_dir else None

    def grade(
        self,
        item_payload: dict[str, Any],
        grading_params: dict[str, Any],
        response: ModelResponse,
    ) -> GradeResult:
        required_claims = list(
            item_payload.get("required_claims") or []
        )
        text = response.text or ""
        cites = _parse_cites(text)

        # Coverage: did each claim's string (or a normalized alias) appear
        # in the response text at all? This is deterministic and simple.
        addressed = 0
        claim_breakdown: list[dict] = []
        for idx, claim in enumerate(required_claims, start=1):
            present = self._claim_addressed(claim, text, idx, cites)
            if present:
                addressed += 1
            claim_breakdown.append({"claim": claim, "addressed": present})

        # Source validity: do cited indices reference real curated sources?
        invalid_cites: list[int] = []
        if self.sources_dir is not None:
            source_files = self._list_sources()
            for c in cites:
                if c < 1 or c > len(source_files):
                    invalid_cites.append(c)

        if not required_claims:
            coverage = 1.0
        else:
            coverage = addressed / len(required_claims)

        # Final score = coverage * validity.
        validity = 1.0 if not invalid_cites else max(
            0.0, 1.0 - len(invalid_cites) / max(len(cites), 1)
        )
        score = coverage * validity

        detail: dict[str, object] = {
            "required_claims": len(required_claims),
            "addressed": addressed,
            "coverage": coverage,
            "validity": validity,
            "cites_seen": cites,
            "invalid_cites": invalid_cites,
            "claims": claim_breakdown,
        }
        return GradeResult(
            score=score,
            passed=score == 1.0,
            detail=detail,
        )

    def _claim_addressed(
        self, claim: str, text: str, idx: int, cites_seen: list[int]
    ) -> bool:
        """A claim is 'addressed' if either the literal claim string OR a
        `[idx]` citation token appears in the response.
        """
        if claim in text:
            return True
        if idx in cites_seen:
            return True
        # Cheap normalization: case-insensitive.
        if claim.lower() in text.lower():
            return True
        return False

    def _list_sources(self) -> list[Path]:
        if self.sources_dir is None:
            return []
        if not self.sources_dir.is_dir():
            return []
        return sorted(self.sources_dir.iterdir())


__all__ = ["CitationGrader", "_parse_cites"]

# Silence warning.
_ = (Grader, GraderError, Iterable, os)
