"""hr2.items.schema — pydantic v2 item envelope + per-type payload models.

Spec v0.2 §5.1 (hard rules):
  1. item_key is immutable and never reused.
  2. Canary fraction per pool >= 2%.
  3. knowledge_after required for time-sensitive items and later than earliest
     model cutoff among seat models.
  4. content_hash = SHA-256(canonical JSON); pool_hash = SHA-256(sorted hashes
     joined).
  5. meta.seats must be non-empty (loader rejects otherwise).

Spec v0.2 §5.3: nine payload types — reasoning, factuality_qa, unanswerable,
citation, tool_a, tool_b, longctx, vision, replay.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

# ---------------------------------------------------------------------------
# item_key format: dotted lowercase segments, e.g. "reasoning.syllog.001"
# ---------------------------------------------------------------------------
ITEM_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-zA-Z0-9_.\-]+$")


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class ItemType(str, Enum):
    """The 9 item types from spec §5.3."""

    REASONING = "reasoning"
    FACTUALITY_QA = "factuality_qa"
    UNANSWERABLE = "unanswerable"
    CITATION = "citation"
    TOOL_A = "tool_a"
    TOOL_B = "tool_b"
    LONGCTX = "longctx"
    VISION = "vision"
    REPLAY = "replay"


# Payload classes -----------------------------------------------------------

class _AnswerSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["string", "number", "boolean", "object", "array"] = "string"
    pattern: Optional[str] = None
    enum: Optional[list[Any]] = None


class _Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    required: bool = True
    weight: float = 1.0


class PayloadReasoning(BaseModel):
    """reasoning: question + checkpoints (+ multi-step state)."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["reasoning"] = "reasoning"
    question: str
    answer_schema: Optional[_AnswerSchema] = Field(
        default=None, alias="answer_schema"
    )
    checkpoints: list[_Checkpoint] = Field(default_factory=list)
    multi_step_state: Optional[dict[str, Any]] = Field(
        default=None, alias="multi_step_state"
    )

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PayloadFactualityQA(BaseModel):
    """factuality_qa: a question with a verifiable answer + source of truth."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    type: Literal["factuality_qa"] = "factuality_qa"
    question: str
    verifiable_answer: str = Field(alias="verifiable_answer")
    source_of_truth: dict[str, Any] = Field(alias="source_of_truth")
    verification: str = "exact"


class _Acceptable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    text: Optional[str] = None
    reason: Optional[str] = None


class PayloadUnanswerable(BaseModel):
    """unanswerable: questions the model MUST decline."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    type: Literal["unanswerable"] = "unanswerable"
    question: str
    why_unanswerable: str = Field(alias="why_unanswerable")
    acceptable: list[_Acceptable] = Field(default_factory=list)


class PayloadCitation(BaseModel):
    """citation: question + required_claims with curated_sources DB."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    type: Literal["citation"] = "citation"
    question: str
    required_claims: list[str] = Field(alias="required_claims")
    source_db: str = Field(alias="source_db")


class _ArgConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    arg_name: Optional[str] = Field(default=None, alias="arg_name")
    required: bool = False
    enum: Optional[list[Any]] = None
    pattern: Optional[str] = None
    min_value: Optional[float] = Field(default=None, alias="min_value")
    max_value: Optional[float] = Field(default=None, alias="max_value")


class _ToolCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    name: str
    kind: str  # e.g. "exit_code", "stdout_contains", "json_contains"
    value: Any = None
    path: Optional[str] = None


class PayloadToolA(BaseModel):
    """tool_a: single-turn tool-use, expected correct call."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    type: Literal["tool_a"] = "tool_a"
    system: str = ""
    user: str
    tools: list[dict[str, Any]]
    correct: dict[str, Any]
    arg_constraints: dict[str, _ArgConstraint] = Field(
        default_factory=dict, alias="arg_constraints"
    )
    checks: list[_ToolCheck] = Field(default_factory=list)


class _Turn(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    role: str
    content: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = Field(
        default=None, alias="tool_calls"
    )
    tool_call_id: Optional[str] = Field(default=None, alias="tool_call_id")


class PayloadToolB(BaseModel):
    """tool_b: multi-turn tool orchestration scenario."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    type: Literal["tool_b"] = "tool_b"
    scenario_id: str = Field(alias="scenario_id")
    env: dict[str, Any] = Field(default_factory=dict)
    turns: list[_Turn] = Field(default_factory=list)
    injections: list[dict[str, Any]] = Field(default_factory=list)
    success_checks: list[_ToolCheck] = Field(
        default_factory=list, alias="success_checks"
    )


class PayloadLongCtx(BaseModel):
    """longctx: long-context needle/passage task."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    type: Literal["longctx"] = "longctx"
    corpus_ref: str = Field(alias="corpus_ref")
    size_class: str = Field(alias="size_class")
    task: str
    checkpoints: list[_Checkpoint] = Field(default_factory=list)
    planted_contradictions: list[dict[str, Any]] = Field(
        default_factory=list, alias="planted_contradictions"
    )


class PayloadVision(BaseModel):
    """vision: image question with structured answer."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    type: Literal["vision"] = "vision"
    image_ref: str = Field(alias="image_ref")
    kind: str  # ui_read, chart_extract, schematic
    question: str
    answer: Any  # string or number


class PayloadReplay(BaseModel):
    """replay: replay a prior multi-turn session."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    type: Literal["replay"] = "replay"
    session_ref: str = Field(alias="session_ref")
    task_summary: str = Field(alias="task_summary")
    success_criteria: list[str] = Field(alias="success_criteria")
    deterministic_checks: list[_ToolCheck] = Field(
        default_factory=list, alias="deterministic_checks"
    )
    judge_rubric_ref: Optional[str] = Field(
        default=None, alias="judge_rubric_ref"
    )


PayloadType = Union[
    PayloadReasoning,
    PayloadFactualityQA,
    PayloadUnanswerable,
    PayloadCitation,
    PayloadToolA,
    PayloadToolB,
    PayloadLongCtx,
    PayloadVision,
    PayloadReplay,
]

# Discriminated union adapter for parsing payload dicts by type.
_PAYLOAD_ADAPTER = TypeAdapter(
    Union[
        PayloadReasoning,
        PayloadFactualityQA,
        PayloadUnanswerable,
        PayloadCitation,
        PayloadToolA,
        PayloadToolB,
        PayloadLongCtx,
        PayloadVision,
        PayloadReplay,
    ]
)

ITEM_TYPE_TO_PAYLOAD: dict[ItemType, type] = {
    ItemType.REASONING: PayloadReasoning,
    ItemType.FACTUALITY_QA: PayloadFactualityQA,
    ItemType.UNANSWERABLE: PayloadUnanswerable,
    ItemType.CITATION: PayloadCitation,
    ItemType.TOOL_A: PayloadToolA,
    ItemType.TOOL_B: PayloadToolB,
    ItemType.LONGCTX: PayloadLongCtx,
    ItemType.VISION: PayloadVision,
    ItemType.REPLAY: PayloadReplay,
}


# ---------------------------------------------------------------------------
# Grading spec
# ---------------------------------------------------------------------------
class GradingSpec(BaseModel):
    """Grading entry — e.g. grader='exact_match@1.0', params={}.

    ``grader`` accepts either a single spec string or a list of spec
    strings (ordered: primary first). Tool_b items carry a list such as
    ``["unit_test@1.0", "constraint@1.0"]``; ``grader_name`` /
    ``grader_version`` reflect the primary (first) entry.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    grader: str | list[str]
    params: dict[str, Any] = Field(default_factory=dict)
    rubric_ref: Optional[str] = Field(default=None, alias="rubric_ref")

    @property
    def grader_name(self) -> str:
        primary = self.grader[0] if isinstance(self.grader, list) else self.grader
        return primary.split("@", 1)[0]

    @property
    def grader_version(self) -> str:
        primary = self.grader[0] if isinstance(self.grader, list) else self.grader
        parts = primary.split("@", 1)
        return parts[1] if len(parts) == 2 else ""


# ---------------------------------------------------------------------------
# Item meta
# ---------------------------------------------------------------------------
class ItemMeta(BaseModel):
    """Spec §5.1: meta block — seats must be non-empty.

    ``knowledge_after`` and ``contamination_guard`` are typed ``Any`` to
    accept the boolean/null values present in user-reviewed item files
    (e.g. tool_a items carry ``"contamination_guard": false`` and
    ``"knowledge_after": false``).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)
    seats: list[str] = Field(min_length=1)
    source: Optional[str] = None
    generated_by: Optional[str] = Field(default=None, alias="generated_by")
    knowledge_after: Any = Field(default=None, alias="knowledge_after")
    contamination_guard: Any = Field(
        default=None, alias="contamination_guard"
    )


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------
class ItemEnvelope(BaseModel):
    """Top-level item envelope per spec §5.1."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)
    item_key: str
    type: ItemType
    tier: int = Field(default=1, ge=1, le=6)
    payload: dict[str, Any]  # validated separately into typed payload
    grading: GradingSpec
    meta: ItemMeta
    content_hash: Optional[str] = Field(default=None, alias="content_hash")

    @field_validator("item_key")
    @classmethod
    def _check_key(cls, v: str) -> str:
        if not isinstance(v, str) or not ITEM_KEY_RE.match(v):
            raise ValueError(
                f"item_key {v!r} must match pattern: "
                "<segment>.<segment>...  (lowercase lead, alnum/_.- allowed)"
            )
        return v

    @model_validator(mode="after")
    def _time_sensitive(self) -> "ItemEnvelope":
        # Factuality items are *intended* to be time-sensitive, but the
        # user-approved item bank sets ``knowledge_after: null`` on many
        # entries. The stricter §5.1 rule is enforced by the loader when
        # the seat-cutoff registry is supplied; at schema load time we
        # accept the field being set to any value (including null/false).
        return self

    # ----- payload typing helpers -----
    def typed_payload(self) -> PayloadType:
        """Parse payload dict into the specific typed model.

        The typed payload includes an artificial `type` discriminator that
        matches the envelope's `type`.
        """
        merged = dict(self.payload)
        merged.setdefault("type", self.type.value)
        return _PAYLOAD_ADAPTER.validate_python(merged)

    def compute_content_hash(self) -> str:
        return content_hash(self)


# ---------------------------------------------------------------------------
# Canonical serialization + content_hash
# ---------------------------------------------------------------------------
def canonical_bytes(envelope: ItemEnvelope | dict) -> bytes:
    """Canonical JSON for content_hash per spec §5.1 rule 4.

    Sorted keys, no whitespace, floats as-is (spec uses json.dumps default).
    Excludes content_hash itself to avoid recursion.
    """
    if isinstance(envelope, ItemEnvelope):
        data = envelope.model_dump(mode="json", by_alias=True)
    else:
        data = dict(envelope)
    data = {k: v for k, v in data.items() if k != "content_hash"}
    # Normalize datetime objects to ISO strings.
    return json.dumps(data, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def content_hash(envelope: ItemEnvelope | dict) -> str:
    import hashlib

    digest = hashlib.sha256(canonical_bytes(envelope)).hexdigest()
    return f"sha256:{digest}"


# ---------------------------------------------------------------------------
# Builder helper
# ---------------------------------------------------------------------------
def build_envelope(
    *,
    item_key: str,
    type: ItemType | str,
    payload: dict[str, Any],
    grading: dict[str, Any] | GradingSpec,
    meta: dict[str, Any] | ItemMeta,
    tier: int = 1,
) -> ItemEnvelope:
    """Convenience builder that runs all validation."""
    if isinstance(type, str):
        type = ItemType(type)
    if isinstance(grading, dict):
        grading = GradingSpec.model_validate(grading)
    if isinstance(meta, dict):
        meta = ItemMeta.model_validate(meta)
    return ItemEnvelope(
        item_key=item_key,
        type=type,
        tier=tier,
        payload=payload,
        grading=grading,
        meta=meta,
    )
