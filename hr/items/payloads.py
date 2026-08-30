from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class ItemType(str, Enum):
    REASONING = "reasoning"
    FACTUALITY_QA = "factuality_qa"
    UNANSWERABLE = "unanswerable"
    CITATION = "citation"
    TOOL_A = "tool_a"
    TOOL_B = "tool_b"
    LONGCTX = "longctx"
    VISION = "vision"
    REPLAY = "replay"


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
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    type: Literal["reasoning"] = "reasoning"
    question: str
    answer_schema: Optional[_AnswerSchema] = Field(default=None, alias="answer_schema")
    checkpoints: list[_Checkpoint] = Field(default_factory=list)
    multi_step_state: Optional[dict[str, Any]] = Field(
        default=None, alias="multi_step_state"
    )


class PayloadFactualityQA(BaseModel):
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
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    type: Literal["unanswerable"] = "unanswerable"
    question: str
    why_unanswerable: str = Field(alias="why_unanswerable")
    acceptable: list[_Acceptable] = Field(default_factory=list)


class PayloadCitation(BaseModel):
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
    kind: str
    value: Any = None
    path: Optional[str] = None


class PayloadToolA(BaseModel):
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
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    type: Literal["vision"] = "vision"
    image_ref: str = Field(alias="image_ref")
    kind: str
    question: str
    answer: Any


class PayloadReplay(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    type: Literal["replay"] = "replay"
    session_ref: str = Field(alias="session_ref")
    task_summary: str = Field(alias="task_summary")
    success_criteria: list[str] = Field(alias="success_criteria")
    deterministic_checks: list[_ToolCheck] = Field(
        default_factory=list, alias="deterministic_checks"
    )
    judge_rubric_ref: Optional[str] = Field(default=None, alias="judge_rubric_ref")


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

_PAYLOAD_ADAPTER = TypeAdapter(PayloadType)

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
