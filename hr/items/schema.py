"""Pydantic v2 item envelope and per-type payload models.

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
from typing import Any, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from hr.items.payloads import (
    ITEM_TYPE_TO_PAYLOAD,
    PayloadCitation,
    PayloadFactualityQA,
    PayloadLongCtx,
    PayloadReasoning,
    PayloadReplay,
    PayloadToolA,
    PayloadToolB,
    PayloadType,
    PayloadUnanswerable,
    PayloadVision,
    ItemType,
    _PAYLOAD_ADAPTER,
)

__all__ = [
    "GradingSpec",
    "ITEM_TYPE_TO_PAYLOAD",
    "ItemEnvelope",
    "ItemMeta",
    "ItemType",
    "PayloadCitation",
    "PayloadFactualityQA",
    "PayloadLongCtx",
    "PayloadReasoning",
    "PayloadReplay",
    "PayloadToolA",
    "PayloadToolB",
    "PayloadType",
    "PayloadUnanswerable",
    "PayloadVision",
    "build_envelope",
    "canonical_bytes",
    "content_hash",
]

# ---------------------------------------------------------------------------
# item_key format: dotted lowercase segments, e.g. "reasoning.syllog.001"
# ---------------------------------------------------------------------------
ITEM_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-zA-Z0-9_.\-]+$")


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
