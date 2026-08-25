"""hr2.graders.base — ModelResponse, GradeResult, Grader Protocol (spec §6.1).

Spec §6.1 protocol:
  - ModelResponse: text, thinking, tool_calls, raw, latency_ms, tokens_in,
    tokens_out.
  - GradeResult: 0.0 <= score <= 1.0, passed boolean, detail dict, optional
    judge_verdict_fk.
  - Grader Protocol: name, version, grade(item_payload, grading_params,
    model_response) -> GradeResult.

GraderRegistry is a simple in-memory dispatcher keyed by grader name
("exact_match", "constraint", ...).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


GRADER_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class GraderError(Exception):
    """Raised when a grader cannot evaluate a response (e.g. malformed input)."""


# ---------------------------------------------------------------------------
# ModelResponse — §6.1
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ModelResponse:
    """A structured model response.

    `text` is the primary text output. `thinking` carries the model's chain-
    of-thought if surfaced. `tool_calls` is a list of tool call dicts
    (function name + args). `raw` is the optional original JSON envelope.
    Latency and token counts are recorded for diagnostics.
    """

    text: str = ""
    thinking: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] | None = None
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


# ---------------------------------------------------------------------------
# GradeResult — §6.1
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GradeResult:
    """Outcome of grading a single (item, response) pair.

    Score in [0, 1]; pass threshold is supplied by the caller at aggregation.
    Detail carries grader-specific structured metadata (e.g., per-checkpoint
    breakdowns). judge_verdict_fk is set only by the llm_judge grader and
    references an hr2.judge_verdict row.
    """

    score: float
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)
    judge_verdict_fk: int | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise GraderError(f"GradeResult.score out of range: {self.score}")
        # Normalize frozen-after-init.
        object.__setattr__(self, "score", float(self.score))


# ---------------------------------------------------------------------------
# Grader Protocol — §6.1
# ---------------------------------------------------------------------------
@runtime_checkable
class Grader(Protocol):
    """Common interface for all graders.

    Implementations MUST NOT perform model-API calls; they evaluate a response
    deterministically (or call out to a pre-configured external judge with a
    stub adapter).
    """

    name: str
    version: str

    def grade(
        self,
        item_payload: dict[str, Any],
        grading_params: dict[str, Any],
        response: ModelResponse,
    ) -> GradeResult:
        ...


# ---------------------------------------------------------------------------
# GraderRegistry — dispatcher
# ---------------------------------------------------------------------------
class GraderRegistry:
    """In-memory registry of Grader instances keyed by name."""

    def __init__(self) -> None:
        self._by_name: dict[str, Any] = {}
        self._by_fullname: dict[str, Any] = {}

    def register(self, grader: Any, name: str, version: str) -> None:
        """Register a Grader under `name` (and full `name@version`)."""
        self._by_name[name] = grader
        self._by_fullname[f"{name}@{version}"] = grader

    def get(self, grader_spec: str) -> Any:
        """Look up by 'name' or 'name@version'. Raises GraderError."""
        if grader_spec in self._by_fullname:
            return self._by_fullname[grader_spec]
        name = grader_spec.split("@", 1)[0]
        if name in self._by_name:
            return self._by_name[name]
        raise GraderError(f"unknown grader: {grader_spec!r}")

    def list(self) -> list[str]:
        return sorted(self._by_name.keys())


def build_default_registry() -> GraderRegistry:
    """Construct a GraderRegistry preloaded with all v0.2 graders."""
    from hr.graders.exact_match import ExactMatchGrader
    from hr.graders.constraint import ConstraintGrader
    from hr.graders.unit_test import UnitTestGrader
    from hr.graders.schema_valid import SchemaValidGrader
    from hr.graders.citation import CitationGrader
    from hr.graders.llm_judge import LLMJudgeGrader

    reg = GraderRegistry()
    for cls in (
        ExactMatchGrader,
        ConstraintGrader,
        UnitTestGrader,
        SchemaValidGrader,
        CitationGrader,
        LLMJudgeGrader,
    ):
        inst = cls()
        reg.register(inst, inst.name, inst.version)
    return reg
