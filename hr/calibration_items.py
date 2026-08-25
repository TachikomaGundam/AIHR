from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from hr.config import load_yaml
from hr.items.loader import pool_hash
from hr.items.schema import ItemEnvelope, ItemType

TOKEN_CAP = 8_000_000
EST_TOKENS_PER_CALL = 5_000
CONCURRENCY_PER_PROVIDER = 8


def load_anchors() -> dict[str, str]:
    """Calibration anchors — ``configs/seats.yaml`` ``calibration_anchors:``.

    Anchor model ids are per-seat assignment policy (which anchor model each
    seat tier calibrates against), so they live in the seat file —
    ``configs/thresholds.yaml`` carries numeric sweep thresholds only. A
    missing file or section FAILS LOUD naming the file: anchors are
    experiment design, never silently defaulted.
    """
    try:
        data = load_yaml("seats.yaml")
    except FileNotFoundError as exc:
        raise RuntimeError(
            "calibration anchors unavailable: configs/seats.yaml not found — "
            "add a 'calibration_anchors:' map with cheap/mid/expensive keys"
        ) from exc
    anchors = data.get("calibration_anchors")
    if not isinstance(anchors, dict) or not anchors:
        raise RuntimeError(
            "calibration anchors not found in configs/seats.yaml: add a "
            "'calibration_anchors:' map with cheap/mid/expensive keys -> "
            "'provider/model_id'"
        )
    return {str(key): str(value) for key, value in anchors.items()}

# Battery -> list of ItemType entries that are in this battery.
BATTERY_TYPES: dict[str, list[ItemType]] = {
    "reasoning": [ItemType.REASONING],
    "hallucination": [
        ItemType.FACTUALITY_QA,
        ItemType.UNANSWERABLE,
        ItemType.CITATION,
    ],
    "tool_a": [ItemType.TOOL_A],
    "vision": [ItemType.VISION],
    "tool_b": [ItemType.TOOL_B],
}

# Per-item-type grader routing + how to build grading_params when the
# item's own ``grading.params`` does not fully describe the expected
# shape (see grader/item reconciliation notes on constraint + exact_match).
_ROUTING: dict[ItemType, tuple[str, str]] = {
    ItemType.REASONING: ("constraint@1.0", "passthrough"),
    ItemType.FACTUALITY_QA: ("exact_match@1.0", "verifiable_answer"),
    ItemType.UNANSWERABLE: ("constraint@1.0", "unanswerable_named"),
    ItemType.CITATION: ("exact_match@1.0", "first_required_claim"),
    ItemType.TOOL_A: ("schema_valid@1.0", "passthrough"),
    ItemType.VISION: ("exact_match@1.0", "passthrough"),
    # tool_b items spec their own primary grader (unit_test@1.0); multi-turn
    # success_checks orchestration is future grader work.
    ItemType.TOOL_B: ("unit_test@1.0", "passthrough"),
}

# §11 acceptance bands (hard acceptance).
ACCEPTANCE_BANDS: dict[int, tuple[str, float, float]] = {
    1: ("tier1", 0.90, 1.00),
    3: ("tier3", 0.40, 0.60),
    6: ("tier6", 0.05, 0.25),
}
def build_messages(envelope: ItemEnvelope) -> list[dict[str, Any]]:
    """Build the Anthropic Messages payload for an item.

    Vision items carry the image as a separate base64 attachment (returned
    alongside via :func:`maybe_vision_image`); this function only shapes
    the text content.
    """
    p = envelope.payload

    if envelope.type == ItemType.REASONING:
        return [{"role": "user", "content": p.get("question", "")}]

    if envelope.type in (
        ItemType.FACTUALITY_QA,
        ItemType.UNANSWERABLE,
        ItemType.CITATION,
    ):
        return [{"role": "user", "content": p.get("question", "")}]

    if envelope.type == ItemType.TOOL_A:
        msgs: list[dict[str, Any]] = []
        system = p.get("system")
        if system:
            msgs.append({"role": "user", "content": f"SYSTEM: {system}"})
            msgs.append({"role": "assistant", "content": "Understood."})
        msgs.append({"role": "user", "content": p.get("user", "")})
        return msgs

    if envelope.type == ItemType.TOOL_B:
        # Turn-based tool scenario: the first user instruction is the
        # task prompt; env is surfaced as a SYSTEM-ish preamble.
        msgs: list[dict[str, Any]] = []
        env = p.get("env")
        if env:
            msgs.append({"role": "user", "content": f"SYSTEM: sandbox={env}"})
            msgs.append({"role": "assistant", "content": "Understood."})
        for turn in p.get("turns") or []:
            msgs.append({"role": "user", "content": turn.get("user", "")})
        return msgs

    if envelope.type == ItemType.VISION:
        return [{"role": "user", "content": p.get("question", "")}]

    return [{"role": "user", "content": str(p.get("question", ""))}]


def maybe_vision_image(
    envelope: ItemEnvelope, item_repo: Path
) -> list[dict[str, Any]] | None:
    """Return a list containing a single base64 PNG block if this is a
    vision item, else None.
    """
    if envelope.type != ItemType.VISION:
        return None
    image_ref = envelope.payload.get("image_ref")
    if not image_ref:
        return None
    img_path = item_repo / "vision" / image_ref
    if not img_path.exists():
        return None
    data = base64.b64encode(img_path.read_bytes()).decode("ascii")
    return [{"data": data, "media_type": "image/png"}]


def build_grading_params(
    envelope: ItemEnvelope,
) -> dict[str, Any]:
    """Build the grading params per routing.

    The item's own ``grading.params`` is the starting point, then the
    routing-specific builder augments it (adds ``expected`` for
    exact_match where the item uses a different field).
    """
    params = dict(envelope.grading.params or {})
    kind = _ROUTING.get(envelope.type, ("", "passthrough"))[1]
    p = envelope.payload

    if kind == "verifiable_answer":
        params.setdefault("expected", p.get("verifiable_answer"))
    elif kind == "first_required_claim":
        claims = p.get("required_claims") or []
        if claims:
            params.setdefault("expected", claims[0])
    elif kind == "unanswerable_named":
        # The constraint grader handles bare-string checks natively; pass
        # params through but ensure the checks list is preserved.
        pass
    return params


# ---------------------------------------------------------------------------
# Loader glue — pull a dict[ItemType] -> list[ItemEnvelope] from disk
# ---------------------------------------------------------------------------
def load_item_repo(
    item_repo: Path,
    *,
    batteries: list[str] | None = None,
) -> dict[str, list[ItemEnvelope]]:
    """Walk item repo and group loaded envelopes by battery.

    Battery groupings come from :data:`BATTERY_TYPES`. Items whose
    ``type`` doesn't belong to any requested battery are skipped (this
    is how ``longctx`` is excluded from Stage 0; ``tool_b`` is a first-
    class battery here).
    """
    wanted = set(batteries or BATTERY_TYPES.keys())
    wanted_types: set[ItemType] = set()
    for bat in wanted:
        wanted_types.update(BATTERY_TYPES.get(bat, []))

    groups: dict[str, list[ItemEnvelope]] = {b: [] for b in wanted}

    # Walk the repo without a DB — we only need the envelopes, not
    # persistence, at load time.
    for json_path in sorted(item_repo.rglob("*.json")):
        try:
            raw = json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(raw, dict) or "item_key" not in raw:
            continue
        try:
            env = ItemEnvelope.model_validate(raw)
        except Exception:
            continue
        if env.type not in wanted_types:
            continue
        for bat, types in BATTERY_TYPES.items():
            if bat in wanted and env.type in types:
                groups[bat].append(env)
                break
    return groups


# ---------------------------------------------------------------------------
# Runner
def _compute_pool_hash(
    item_repo: Path, batteries: list[str] | None
) -> str:
    wanted = set(batteries or BATTERY_TYPES.keys())
    wanted_types: set[ItemType] = set()
    for bat in wanted:
        wanted_types.update(BATTERY_TYPES.get(bat, []))

    hashes: list[str] = []
    for json_path in sorted(item_repo.rglob("*.json")):
        try:
            raw = json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(raw, dict) or "item_key" not in raw:
            continue
        try:
            env = ItemEnvelope.model_validate(raw)
        except Exception:
            continue
        if env.type not in wanted_types:
            continue
        hashes.append((env.content_hash or env.compute_content_hash()))
    return pool_hash(hashes)

__all__ = ["TOKEN_CAP", "EST_TOKENS_PER_CALL", "CONCURRENCY_PER_PROVIDER", "BATTERY_TYPES", "_ROUTING", "ACCEPTANCE_BANDS", "load_anchors", "build_messages", "maybe_vision_image", "build_grading_params", "load_item_repo", "_compute_pool_hash"]
