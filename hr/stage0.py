"""Stage 0 — full-pool reduced-subset sweep.

Runs the registered model pool through Stage-0 reduced batteries (reasoning 20,
hallucination 25, tool_a 30, vision-lite 15) with sequential-n, records
to the hr2 DB, and produces a per-battery paired-bootstrap separation
matrix.

CLI:
    python3 -m hr2.stage0 --dry-run       print the call plan (no API calls)
    python3 -m hr2.stage0 --pilot         run n=3 pilot for all models
    python3 -m hr2.stage0 --separation    read separation matrix from DB

The full-pool sweep is fired separately by the orchestrator.
This module is the RUNNER; the orchestrator just invokes ``run_sweep``
with the defaults and lets it execute to completion.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from hr.adapters.base import AdapterError, Capabilities
from hr.config import itemrepo_path, load_yaml
from hr.fleet import fleet_models
from hr.graders import build_default_registry
from hr.graders.base import GradeResult, GraderRegistry, ModelResponse
from hr.items.schema import ItemEnvelope, ItemType, content_hash


# ---------------------------------------------------------------------------
# Model pool
# ---------------------------------------------------------------------------
# The sweep pool is the model fleet derived at RUNTIME from the opencode
# config + configs/deployable.yaml extra_deployable (hr.fleet.fleet_models,
# resolved at CALL time so tests can redirect OPENCODE_CONFIG_DIR). Model
# names live in the opencode config only.

STAGE0_BATTERIES: tuple[str, ...] = ("reasoning", "hallucination", "tool_a", "vision", "tool_b")

#: Spec §5.4 v0.2 — exact Stage-0 subset sizes.
STAGE0_SUBSET_SIZES: dict[str, int] = {
    "reasoning": 20,
    "hallucination": 25,
    "tool_a": 30,
    "vision": 15,
    "tool_b": 10,
}

#: Item-type grouping used by the item loader (mirrors calibrate.BATTERY_TYPES).
#: Hallucination = factuality_qa + unanswerable + citation.
BATTERY_ITEM_TYPES: dict[str, tuple[ItemType, ...]] = {
    "reasoning": (ItemType.REASONING,),
    "hallucination": (ItemType.FACTUALITY_QA, ItemType.UNANSWERABLE, ItemType.CITATION),
    "tool_a": (ItemType.TOOL_A,),
    "vision": (ItemType.VISION,),
    "tool_b": (ItemType.TOOL_B,),
}

#: Stage 0 budget cap fallback (spec §9.1 v0.3 set 30M, which the 34-model
#: fleet already exceeds: pilot = 34 models x 100 items x n_initial 3 x
#: 5,000 tokens = 51.0M). The OPERATIONAL value lives in
#: configs/thresholds.yaml ``stage0.token_cap`` (60M, ~18% headroom) and is
#: resolved at CALL time via :func:`_stage0_token_cap` so tests can redirect
#: configs; this constant is the offline fallback. Both values moved together.
STAGE0_TOKEN_CAP: int = 60_000_000

#: Estimated tokens per call for dry-run budgeting.
EST_TOKENS_PER_CALL: int = 5_000


def _stage0_token_cap() -> int:
    """Resolve the Stage-0 token cap: ``thresholds.yaml stage0.token_cap`` wins.

    Falls back to STAGE0_TOKEN_CAP when the config is missing or the key is
    absent/non-positive (config-wins pattern, mirrors cli._KNOB_TO_BATTERY).
    """
    try:
        section = load_yaml("thresholds.yaml").get("stage0") or {}
    except FileNotFoundError:
        return STAGE0_TOKEN_CAP
    value = section.get("token_cap") if isinstance(section, dict) else None
    if isinstance(value, int) and value > 0:
        return value
    return STAGE0_TOKEN_CAP

#: Sentinel seat upserted specifically for Stage 0.
STAGE0_SEAT_CODE: str = "_stage0_sweep"


# ---------------------------------------------------------------------------
# Adapter facade (protocol-compatible with hr2.adapters.base.Adapter)
# ---------------------------------------------------------------------------
class _AdapterFacade(Protocol):
    def probe_capabilities(self, model_id: str) -> Capabilities: ...

    def chat(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        images: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        thinking_budget: int | None = None,
        max_output: int = 16384,
        timeout_s: int = 600,
    ) -> ModelResponse: ...


# ---------------------------------------------------------------------------
# Item subset selection
# ---------------------------------------------------------------------------
def _tier_items(items: list[ItemEnvelope]) -> dict[int, list[ItemEnvelope]]:
    """Group items by tier (1-6)."""
    out: dict[int, list[ItemEnvelope]] = {}
    for env in items:
        out.setdefault(env.tier, []).append(env)
    for k, v in out.items():
        v.sort(key=lambda e: e.item_key)
    return out


def select_reasoning_subset(
    items: list[ItemEnvelope], n: int = STAGE0_SUBSET_SIZES["reasoning"]
) -> list[ItemEnvelope]:
    """Pick ``n`` reasoning items, ~3-4 per tier across t1-t6.

    Deterministic: sort within tier by item_key, then round-robin across
    tiers in order, wrapping as needed to hit ``n`` exactly.
    """
    by_tier = _tier_items(items)
    tiers = sorted(by_tier.keys())
    # Target ~n//len(tiers) per tier; remainder spread across early tiers.
    base = n // len(tiers) if tiers else 0
    remainder = n - base * len(tiers)
    selected: list[ItemEnvelope] = []
    for i, tier in enumerate(tiers):
        k = base + (1 if i < remainder else 0)
        selected.extend(by_tier[tier][:k])
    selected.sort(key=lambda e: e.item_key)
    return selected


def select_hallucination_subset(
    items_by_type: dict[ItemType, list[ItemEnvelope]],
    n: int = STAGE0_SUBSET_SIZES["hallucination"],
) -> list[ItemEnvelope]:
    """Pick ``n`` hallucination items — a mix of qa/unanswerable/citation."""
    qa = sorted(items_by_type.get(ItemType.FACTUALITY_QA, []), key=lambda e: e.item_key)
    ua = sorted(items_by_type.get(ItemType.UNANSWERABLE, []), key=lambda e: e.item_key)
    cit = sorted(items_by_type.get(ItemType.CITATION, []), key=lambda e: e.item_key)
    # Split roughly proportionally but ensure all 3 subtypes appear.
    # Target 15 qa + 7 unanswerable + 3 citation = 25.
    qa_n = int(round(n * 0.6))
    ua_n = int(round(n * 0.28))
    cit_n = n - qa_n - ua_n
    out = qa[:qa_n] + ua[:ua_n] + cit[:cit_n]
    out.sort(key=lambda e: e.item_key)
    return out


def select_tool_subset(
    items: list[ItemEnvelope], n: int = STAGE0_SUBSET_SIZES["tool_a"]
) -> list[ItemEnvelope]:
    """Pick ``n`` tool_a items, stratified by sub-kind when possible."""
    by_subkind: dict[str, list[ItemEnvelope]] = {}
    for env in items:
        # tool_a item_keys: "tool_a.<subkind>.<name>"
        parts = env.item_key.split(".", 2)
        subkind = parts[1] if len(parts) >= 2 else "unknown"
        by_subkind.setdefault(subkind, []).append(env)
    for k, v in by_subkind.items():
        v.sort(key=lambda e: e.item_key)
    # Round-robin across sub-kinds then top up.
    subkinds = sorted(by_subkind.keys())
    selected: list[ItemEnvelope] = []
    seen: set[str] = set()
    for _ in range(10):  # up to 10 rounds (enough for any realistic n)
        for sk in subkinds:
            for env in by_subkind[sk]:
                if env.item_key in seen:
                    continue
                selected.append(env)
                seen.add(env.item_key)
                if len(selected) >= n:
                    break
            if len(selected) >= n:
                break
        if len(selected) >= n:
            break
    selected.sort(key=lambda e: e.item_key)
    return selected[:n]


def select_vision_subset(
    items: list[ItemEnvelope], n: int = STAGE0_SUBSET_SIZES["vision"]
) -> list[ItemEnvelope]:
    """Pick ``n`` vision items — 5 per kind (ui_read / chart_extract / schematic).

    Spec §5.4 v0.2: UI/schematic/chart × 5 each = 15. Falls back to
    proportional allocation if kinds are missing.
    """
    kinds = ("ui_read", "chart_extract", "schematic")
    by_kind: dict[str, list[ItemEnvelope]] = {k: [] for k in kinds}
    for env in items:
        # vision item_key like "vision.ui_read.X"
        parts = env.item_key.split(".", 2)
        kind = parts[1] if len(parts) >= 2 else "unknown"
        if kind in by_kind:
            by_kind[kind].append(env)
    for k in kinds:
        by_kind[k].sort(key=lambda e: e.item_key)
    # 5 per kind when available; otherwise take what's there.
    per_kind = n // len(kinds)
    remainder = n - per_kind * len(kinds)
    selected: list[ItemEnvelope] = []
    for i, kind in enumerate(kinds):
        take = per_kind + (1 if i < remainder else 0)
        selected.extend(by_kind[kind][:take])
    # Fill if any kind was short — spread across the rest.
    if len(selected) < n:
        remaining = [
            e for e in items if e.item_key not in {s.item_key for s in selected}
        ]
        remaining.sort(key=lambda e: e.item_key)
        i = 0
        while len(selected) < n and i < len(remaining):
            selected.append(remaining[i])
            i += 1
    selected.sort(key=lambda e: e.item_key)
    return selected


def select_subsets(
    items_by_battery: dict[str, list[ItemEnvelope]],
) -> dict[str, list[ItemEnvelope]]:
    """Apply Stage-0 subset selection to each battery's items.

    For hallucination, ``items_by_battery["hallucination"]`` is a flattened
    list; we must re-group by type.
    """
    out: dict[str, list[ItemEnvelope]] = {}

    # reasoning
    reasoning_items = items_by_battery.get("reasoning", [])
    out["reasoning"] = select_reasoning_subset(reasoning_items)

    # hallucination (mixed types)
    halluc_items_by_type: dict[ItemType, list[ItemEnvelope]] = {}
    for env in items_by_battery.get("hallucination", []):
        halluc_items_by_type.setdefault(env.type, []).append(env)
    out["hallucination"] = select_hallucination_subset(halluc_items_by_type)

    # tool_a
    tool_items = items_by_battery.get("tool_a", [])
    out["tool_a"] = select_tool_subset(tool_items)

    # vision
    vision_items = items_by_battery.get("vision", [])
    out["vision"] = select_vision_subset(vision_items)

    # tool_b
    tool_b_items = items_by_battery.get("tool_b", [])
    out["tool_b"] = select_tool_subset(tool_b_items)

    return out


# ---------------------------------------------------------------------------
# Adapter wiring helpers
# ---------------------------------------------------------------------------
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
        from hr.scheduler.taxonomy import classify_failure

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
def compute_pool_hash(subsets: dict[str, list[ItemEnvelope]]) -> str:
    """Deterministic sha256 of the Stage-0 subset contents."""
    digests: list[str] = []
    for battery in STAGE0_BATTERIES:
        for env in subsets.get(battery, []):
            h = content_hash(env)
            if h:
                digests.append(h)
            else:
                digests.append(compute_content_hash_default(env))
    digests.sort()
    payload = "|".join(digests).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def compute_content_hash_default(env: ItemEnvelope) -> str:
    """Fallback content hash using canonical JSON serialization."""
    canonical = json.dumps(
        {"item_key": env.item_key, "type": env.type.value, "tier": env.tier, "payload": env.payload},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


# ---------------------------------------------------------------------------
# Dry-run plan
# ---------------------------------------------------------------------------
@dataclass
class CallPlan:
    """Summary of the planned calls."""

    models: list[str]
    battery_item_counts: dict[str, int]
    n_initial: int
    n_max: int
    estimated_tokens: int
    budget_cap: int
    within_budget: bool


def build_call_plan(
    subsets: dict[str, list[ItemEnvelope]],
    models: tuple[str, ...] | None = None,
    n_initial: int = 3,
    budget_cap: int | None = None,
) -> CallPlan:
    battery_counts = {b: len(items) for b, items in subsets.items()}
    if models is None:
        models = fleet_models()
    if budget_cap is None:
        budget_cap = _stage0_token_cap()
    total_items = sum(battery_counts.values())
    est_total_calls = len(models) * total_items * n_initial
    est_tokens = est_total_calls * EST_TOKENS_PER_CALL
    return CallPlan(
        models=list(models),
        battery_item_counts=battery_counts,
        n_initial=n_initial,
        n_max=10,
        estimated_tokens=est_tokens,
        budget_cap=budget_cap,
        within_budget=est_tokens <= budget_cap,
    )


def print_call_plan(plan: CallPlan) -> None:
    print("=== Stage 0 Call Plan ===")
    print(f"Models ({len(plan.models)}):")
    for m in plan.models:
        print(f"  - {m}")
    print(f"Batteries:")
    for b, count in plan.battery_item_counts.items():
        print(f"  {b}: {count} items")
    total_items = sum(plan.battery_item_counts.values())
    n_calls = len(plan.models) * total_items * plan.n_initial
    print(f"Pilot n={plan.n_initial}, max n={plan.n_max}")
    print(f"Pilot call plan: {len(plan.models)} models × {total_items} items × {plan.n_initial} = {n_calls} calls")
    print(f"Estimated tokens (pilot): {plan.estimated_tokens:,}")
    print(f"Stage-0 budget cap: {plan.budget_cap:,} tokens")
    if plan.within_budget:
        print("✓ Estimated tokens are within cap.")
    else:
        print(f"✗ OVER budget by {plan.estimated_tokens - plan.budget_cap:,} tokens.")
        print("  (Stage 0 runner will halt when the cap is reached.)")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def _init_db() -> None:
    """Initialize the hr2 schema in the wiki DB."""
    from hr.db import init_schema

    init_schema()


def _connect():
    from hr.db import connect

    return connect()


def _upsert_provider(conn, provider_id: str, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hr.provider (provider_id, name) VALUES (%s, %s) "
            "ON CONFLICT (provider_id) DO NOTHING",
            (provider_id, name),
        )
    conn.commit()


def _upsert_model(conn, model_id: str, provider_id: str, model_name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hr.model (model_id, provider_fk, model_name) VALUES (%s, %s, %s) "
            "ON CONFLICT (model_id) DO NOTHING",
            (model_id, provider_id, model_name),
        )
    conn.commit()


def _upsert_battery(conn, battery_code: str, description: str) -> str:
    battery_id = f"battery-{battery_code}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hr.battery (battery_id, battery_code, version, description) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (battery_id) DO NOTHING",
            (battery_id, battery_code, "v1", description),
        )
    conn.commit()
    return battery_id


def _upsert_seat(conn, seat_code: str, seat_name: str) -> None:
    """Upsert one seat row through the shared yaml-driven seat path.

    Real seats (codes present in ``configs/seats.yaml``) get their full typed
    values from the yaml via ``hr.seats.seed`` (``ON CONFLICT DO UPDATE``);
    sweep pseudo-seats absent from the yaml (``_stage0_sweep``,
    ``_stage1_finals``) keep the legacy generic fallback shape
    (``ON CONFLICT DO NOTHING``) so Stage-0/1 scheduling rows are unchanged.
    """
    from hr.seats.seed import upsert_seat

    upsert_seat(conn, seat_code, seat_name)


def _upsert_item_pool(conn, env: ItemEnvelope) -> None:
    """Upsert the item_pool row for an envelope (item_id = item_key)."""
    domain = env.item_key.split(".")[0] if "." in env.item_key else "general"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hr.item_pool (item_id, item_code, version, domain, kind, json_meta) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb) ON CONFLICT (item_id) DO NOTHING",
            (
                env.item_key,
                env.item_key,
                "v1",
                domain,
                env.type.value,
                json.dumps({"tier": env.tier, "seats": list(env.meta.seats or [])}),
            ),
        )
    conn.commit()


def _upsert_battery_item(conn, battery_id: str, item_id: str, position: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hr.battery_item (battery_id, item_id, weight, position) "
            "VALUES (%s, %s, 1.0, %s) ON CONFLICT DO NOTHING",
            (battery_id, item_id, position),
        )
    conn.commit()


def _upsert_seat_battery(
    conn, seat_code: str, battery_id: str, n_initial: int = 3, n_max: int = 10
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hr.seat_battery (seat_code, battery_id, n_initial, n_max) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (seat_code, battery_id, n_initial, n_max),
        )
    conn.commit()


def _insert_sweep(conn, sweep_id: str, seat_code: str, purpose: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hr.sweep (sweep_id, seat_code, purpose, created_at) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (sweep_id, seat_code, purpose, datetime.now(timezone.utc)),
        )
    conn.commit()


def _insert_run(
    conn,
    run_id: str,
    sweep_id: str,
    model_id: str,
    battery_id: str,
    round_num: int,
    total_tokens: int,
    total_cost_cny: float,
    infra_ok: bool,
) -> None:
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hr.run (run_id, sweep_id, model_id, battery_id, round, "
            "started_at, finished_at, total_tokens, total_cost_cny, infra_ok) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (
                run_id,
                sweep_id,
                model_id,
                battery_id,
                round_num,
                now,
                now,
                total_tokens,
                total_cost_cny,
                infra_ok,
            ),
        )
    conn.commit()


def _sanitize_db_text(text: str | None) -> str | None:
    """Strip characters PostgreSQL ``text`` rejects before DB insert.

    Models occasionally emit NUL (0x00) and other raw control bytes in their
    output; psycopg2 raises "A string literal cannot contain NUL (0x00)
    characters" when such bytes reach an INSERT. Remove NUL and the other
    disallowed C0 controls (keeping \t \n \r), since they carry no answer content.
    """
    if text is None:
        return None
    return "".join(
        ch for ch in text if ch in ("\t", "\n", "\r") or ord(ch) >= 0x20
    )


def _insert_measurement(
    conn,
    measurement_id: str,
    run_id: str,
    item_id: str,
    repetition: int,
    score: float,
    tokens_in: int,
    tokens_out: int,
    latency_ms: int,
    response_text: str | None = None,
    thinking_text: str | None = None,
    requested_max_output: int | None = None,
) -> None:
    """Insert one measurement row (ON CONFLICT DO NOTHING).

    ``requested_max_output`` records the output cap that was ACTUALLY sent on
    the wire (e.g. the livebench bench writer sets it from ChatRequest.
    max_output); hr2.health judges truncation against this per-row cap when
    present. Existing callers omit it -> NULL (legacy-proxy fallback).
    """
    response_text = _sanitize_db_text(response_text)
    thinking_text = _sanitize_db_text(thinking_text)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hr.measurement (measurement_id, run_id, item_id, repetition, "
            "score, tokens_in, tokens_out, latency_ms, created_at, "
            "response_text, thinking_text, requested_max_output) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (
                measurement_id,
                run_id,
                item_id,
                repetition,
                score,
                tokens_in,
                tokens_out,
                latency_ms,
                datetime.now(timezone.utc),
                response_text,
                thinking_text,
                requested_max_output,
            ),
        )
    conn.commit()


def _insert_infra_incident(conn, run_id: str, kind: str, details: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hr.infra_incident (incident_id, run_id, kind, details_json, recorded_at) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (
                f"inc-{uuid.uuid4()}",
                run_id,
                kind,
                json.dumps(details),
                datetime.now(timezone.utc),
            ),
        )
    conn.commit()


def _insert_separation(
    conn,
    separation_id: str,
    sweep_id: str,
    battery_id: str,
    model_a: str,
    model_b: str,
    p_separated: float,
    p_weak: float,
    p_tie: float,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hr.separation (separation_id, sweep_id, battery_id, model_a, model_b, "
            "p_separated, p_weak, p_tie, estimated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (
                separation_id,
                sweep_id,
                battery_id,
                model_a,
                model_b,
                p_separated,
                p_weak,
                p_tie,
                datetime.now(timezone.utc),
            ),
        )
    conn.commit()


def _ensure_provider_model_records(conn, models: tuple[str, ...]) -> dict[str, str]:
    """Return a map of model_id -> provider_id, inserting records as needed."""
    # Provider display names come from the opencode config's `name` field
    # (dynamic derivation); providers absent from the config fall back to
    # their provider id.
    provider_names = fleet.provider_display_names()
    model_to_provider: dict[str, str] = {}
    for model_id in models:
        if "/" in model_id:
            provider_id, slug = model_id.split("/", 1)
        else:
            provider_id, slug = "unknown", model_id
        provider_names.setdefault(provider_id, provider_id)
        _upsert_provider(conn, provider_id, provider_names[provider_id])
        _upsert_model(conn, model_id, provider_id, slug)
        model_to_provider[model_id] = provider_id
    return model_to_provider


# ---------------------------------------------------------------------------
# Sweep core
# ---------------------------------------------------------------------------
@dataclass
class SweepState:
    sweep_id: str
    total_tokens: int = 0
    total_calls: int = 0
    stopped_at_cap: bool = False
    stopped_reason: str = ""
    measurements_by_model_battery: dict[str, dict[str, list[dict]]] = field(default_factory=dict)
    # key: f"{model_id}|{battery_code}" -> { item_key: [scores] }
    # We keep a per-(model,battery) dict of item_key -> [scores across repetitions]


def _key(model_id: str, battery: str) -> str:
    return f"{model_id}|{battery}"


def should_exclude_zero(infra_failure: str | None) -> bool:
    """Whether a 0-score caused by infra failure should be excluded from stats.

    Stage 0 policy: only exclude if corroboration exists. For full rigor we
    defer to the stats module's ``should_exclude_zero``. Here we return
    conservatively (exclude only retryable classes).
    """
    if infra_failure is None:
        return False
    from hr.scheduler.taxonomy import retryable

    return retryable(infra_failure)


def _bootstrap_separation_from_state(
    state: SweepState,
) -> dict[str, list[dict]]:
    """Run paired bootstrap separation within each battery.

    Returns: dict of battery_code -> list of {model_a, model_b, p_separated, p_weak, p_tie}.
    """
    from hr.stats.bootstrap import classify, paired_bootstrap_separation

    result: dict[str, list[dict]] = {}
    per_battery: dict[str, dict[str, list[float]]] = {}
    # Group scores by per (battery, model) — mean over items per round.
    for key_str, per_item in state.measurements_by_model_battery.items():
        model_id, battery = key_str.split("|", 1)
        per_battery.setdefault(battery, {})
        # Per-model scores: average over all item scores in this battery.
        all_scores: list[float] = []
        for item_scores in per_item.values():
            all_scores.extend(item_scores)
        if not all_scores:
            continue
        per_battery[battery][model_id] = all_scores

    for battery_code, model_scores in per_battery.items():
        pairs: list[dict] = []
        model_ids = sorted(model_scores.keys())
        for i, ma in enumerate(model_ids):
            for mb in model_ids[i + 1 :]:
                sa = model_scores[ma]
                sb = model_scores[mb]
                # Use both directions to compute weak / separated / tie.
                p_a = paired_bootstrap_separation(sa, sb)
                p_b = paired_bootstrap_separation(sb, sa)
                # spec §10.2: p = P(mean(A) > mean(B)); weak = max(p, 1-p) when not separated.
                raw = max(p_a, p_b)
                classified = classify(raw)
                if classified == "separated":
                    pairs.append(
                        {
                            "model_a": ma,
                            "model_b": mb,
                            "p_separated": raw,
                            "p_weak": 1.0 - raw,
                            "p_tie": 0.0,
                        }
                    )
                elif classified == "weak":
                    pairs.append(
                        {
                            "model_a": ma,
                            "model_b": mb,
                            "p_separated": 0.0,
                            "p_weak": raw,
                            "p_tie": 1.0 - raw,
                        }
                    )
                else:
                    pairs.append(
                        {
                            "model_a": ma,
                            "model_b": mb,
                            "p_separated": 0.0,
                            "p_weak": 0.0,
                            "p_tie": 1.0,
                        }
                    )
        result[battery_code] = pairs
    return result


def print_separation_matrix(state: SweepState | None = None, sweep_id: str | None = None) -> None:
    """Print the per-battery separation matrix from DB (or live state)."""
    if state is None and sweep_id is None:
        print("Provide sweep_id or live state.")
        return

    if state is not None:
        sep = _bootstrap_separation_from_state(state)
    else:
        # Load from DB.
        from hr.db import connect

        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT battery_id, model_a, model_b, p_separated, p_weak, p_tie "
                    "FROM hr.separation WHERE sweep_id = %s ORDER BY battery_id, model_a, model_b",
                    (sweep_id,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        sep: dict[str, list[dict]] = {}
        for battery_id, a, b, ps, pw, pt in rows:
            battery_code = battery_id.replace("battery-", "")
            sep.setdefault(battery_code, []).append(
                {
                    "model_a": a,
                    "model_b": b,
                    "p_separated": float(ps),
                    "p_weak": float(pw),
                    "p_tie": float(pt),
                }
            )

    _print_matrix(sep)


def _print_matrix(sep: dict[str, list[dict]]) -> None:
    """Pretty-print the separation matrix."""
    print()
    print("=== Stage 0 Separation Matrix ===")

    for battery_code, pairs in sep.items():
        print(f"\n--- Battery: {battery_code} ---")
        if not pairs:
            print("  (no pairs recorded)")
            continue
        # Collect model ids and classify.
        classifications: dict[str, dict[str, str]] = {}
        all_models: set[str] = set()
        for p in pairs:
            ma, mb = p["model_a"], p["model_b"]
            all_models.add(ma)
            all_models.add(mb)
            label = "sep" if p["p_separated"] > 0 else "weak" if p["p_weak"] > 0 else "tie"
            classifications.setdefault(ma, {})[mb] = label
            classifications.setdefault(mb, {})[ma] = label

        sorted_models = sorted(all_models)
        print(f"  Models ({len(sorted_models)}): {', '.join(sorted_models)}")
        # Summary counts.
        n_sep = sum(1 for _, m in classifications.items() for _, v in m.items() if v == "sep") // 2
        n_weak = sum(1 for _, m in classifications.items() for _, v in m.items() if v == "weak") // 2
        n_tie = sum(1 for _, m in classifications.items() for _, v in m.items() if v == "tie") // 2
        print(f"  Separated: {n_sep} pairs | Weak: {n_weak} pairs | Tie: {n_tie} pairs")

        # Show a matrix.
        header = "     " + "".join([f"{m[-12:]:>13}" for m in sorted_models])
        print(header)
        for ma in sorted_models:
            row = f"{ma[-20:]:>21} "
            for mb in sorted_models:
                if ma == mb:
                    row += f"{'--':>13}"
                else:
                    label = classifications.get(ma, {}).get(mb, "?")
                    row += f"{label:>13}"
            print(row)


# ---------------------------------------------------------------------------
# Main sweep runner
# ---------------------------------------------------------------------------
def run_sweep(
    adapter: _AdapterFacade,
    item_repo: Path | None = None,
    models: tuple[str, ...] | None = None,
    batteries: tuple[str, ...] = STAGE0_BATTERIES,
    *,
    n_initial: int = 3,
    token_cap: int | None = None,
    dry_run: bool = False,
    init_db: bool = True,
    record_to_db: bool = True,
    registry: GraderRegistry | None = None,
) -> tuple[CallPlan, SweepState | None]:
    """Run the Stage 0 sweep.

    Returns ``(plan, state)`` where ``state`` is ``None`` only on ``dry_run``.
    ``item_repo`` defaults to :func:`hr.config.itemrepo_path` (HR_ITEMREPO
    env or HR_HOME/itemrepo). ``registry`` may be injected (tests supply a
    stub to avoid spawning real grader subprocesses); defaults to
    :func:`build_default_registry`. ``token_cap`` None resolves the config
    value (``stage0.token_cap``).
    """
    if item_repo is None:
        item_repo = itemrepo_path()
    if token_cap is None:
        token_cap = _stage0_token_cap()

    # 1. Load items + select subsets.
    from hr.calibrate import load_item_repo

    if models is None:
        models = fleet_models()

    item_bundles = load_item_repo(item_repo, batteries=list(batteries))
    # load_item_repo returns dict[battery, list[ItemEnvelope]]
    # But the hallucination battery groups FACTUALITY_QA+UNANSWERABLE+CITATION
    # under one key — we need to flatten for subset selection.
    subsets = select_subsets(item_bundles)

    plan = build_call_plan(subsets, models=models, n_initial=n_initial, budget_cap=token_cap)
    if dry_run:
        print_call_plan(plan)
        return plan, None

    pool_hash = compute_pool_hash(subsets)

    # 2. Init DB.
    if init_db or record_to_db:
        _init_db()
        conn = _connect()
    else:
        conn = None

    try:
        # 3. Upsert reference rows.
        if conn is not None:
            _upsert_seat(conn, STAGE0_SEAT_CODE, "Stage 0 full-pool sweep")
            _ensure_provider_model_records(conn, models)
            battery_ids: dict[str, str] = {}
            for bcode in batteries:
                battery_ids[bcode] = _upsert_battery(conn, bcode, f"Stage-0 {bcode} battery")
            for bcode in batteries:
                b_id = battery_ids[bcode]
                _upsert_seat_battery(conn, STAGE0_SEAT_CODE, b_id)
                for pos, env in enumerate(subsets.get(bcode, [])):
                    _upsert_item_pool(conn, env)
                    _upsert_battery_item(conn, b_id, env.item_key, pos)

            # 4. Create sweep.
            sweep_id = f"stage0-{uuid.uuid4()}"
            purpose = (
                f"Stage 0 full-pool sweep\n"
                f"pool_hash: {pool_hash}\n"
                f"models: {len(models)}\n"
                f"n_initial: {n_initial}\n"
                f"token_cap: {token_cap}\n"
                f"subsets: { {b: len(items) for b, items in subsets.items()} }"
            )
            _insert_sweep(conn, sweep_id, STAGE0_SEAT_CODE, purpose)
        else:
            sweep_id = f"stage0-{uuid.uuid4()}"
            battery_ids = {b: f"battery-{b}" for b in batteries}

        state = SweepState(sweep_id=sweep_id)
        registry = registry or build_default_registry()

        # 5. Run sweep.
        try:
            _run_sweep_loop(
                adapter=adapter,
                item_repo=item_repo,
                models=models,
                subsets=subsets,
                batteries=batteries,
                battery_ids=battery_ids,
                n_initial=n_initial,
                token_cap=token_cap,
                state=state,
                registry=registry,
                conn=conn,
                sweep_id=sweep_id,
                record_to_db=record_to_db and conn is not None,
            )
        except KeyboardInterrupt:
            if conn is not None:
                print(f"\nSweep interrupted at {state.total_tokens:,} tokens.")
            raise

        if state.stopped_at_cap:
            print(f"\n⚠ Stage 0 halted at {state.total_tokens:,} / {token_cap:,} tokens.")
            print(f"Reason: {state.stopped_reason}")
        else:
            print(f"\n✓ Stage 0 complete. Total tokens: {state.total_tokens:,} / {token_cap:,}")

        # 6. Compute separation and record.
        sep = _bootstrap_separation_from_state(state)
        if conn is not None:
            for battery_code, pairs in sep.items():
                if battery_code not in battery_ids:
                    continue
                b_id = battery_ids[battery_code]
                for p in pairs:
                    _insert_separation(
                        conn,
                        separation_id=f"sep-{uuid.uuid4()}",
                        sweep_id=sweep_id,
                        battery_id=b_id,
                        model_a=p["model_a"],
                        model_b=p["model_b"],
                        p_separated=p["p_separated"],
                        p_weak=p["p_weak"],
                        p_tie=p["p_tie"],
                    )

        print(f"Sweep ID: {sweep_id}")
        print(f"Pool hash: {pool_hash}")
        _print_matrix(sep)
        return plan, state
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _run_sweep_loop(
    *,
    adapter: _AdapterFacade,
    item_repo: Path,
    models: tuple[str, ...],
    subsets: dict[str, list[ItemEnvelope]],
    batteries: tuple[str, ...],
    battery_ids: dict[str, str],
    n_initial: int,
    token_cap: int,
    state: SweepState,
    registry,
    conn,
    sweep_id: str,
    record_to_db: bool,
) -> None:
    """Core nested loop: models × batteries × rounds."""
    active_subsets = {b: subsets[b] for b in batteries if b in subsets}
    for model_id in models:
        if state.stopped_at_cap:
            break
        for battery_code, items in active_subsets.items():
            if state.stopped_at_cap:
                break
            b_id = battery_ids[battery_code]
            for round_num in range(1, n_initial + 1):
                if state.stopped_at_cap:
                    state.stopped_reason = (
                        f"Token cap reached during {model_id}/{battery_code}/{round_num}."
                    )
                    break
                round_id = f"run-{uuid.uuid4()}"
                if record_to_db and conn is not None:
                    _insert_run(
                        conn,
                        run_id=round_id,
                        sweep_id=sweep_id,
                        model_id=model_id,
                        battery_id=b_id,
                        round_num=round_num,
                        total_tokens=0,
                        total_cost_cny=0.0,
                        infra_ok=True,
                    )
                round_total_tokens = 0
                round_infra_ok = True
                for rep, env in enumerate(items, start=1):
                    ok, result = call_and_grade(adapter, model_id, env, item_repo, registry)
                    state.total_calls += 1
                    call_tokens = result.tokens_in + result.tokens_out
                    state.total_tokens += call_tokens
                    round_total_tokens += call_tokens
                    if not ok:
                        round_infra_ok = False
                        if result.infra_failure and conn is not None:
                            _insert_infra_incident(
                                conn,
                                round_id,
                                kind=result.infra_failure or "unknown",
                                details=result.detail or {},
                            )
                    if record_to_db and conn is not None:
                        _insert_measurement(
                            conn,
                            measurement_id=f"m-{uuid.uuid4()}",
                            run_id=round_id,
                            item_id=env.item_key,
                            repetition=rep,
                            score=result.score,
                            tokens_in=result.tokens_in,
                            tokens_out=result.tokens_out,
                            latency_ms=result.latency_ms,
                            response_text=result.response_text,
                            thinking_text=result.thinking_text,
                        )
                    key_str = _key(model_id, battery_code)
                    per_item = state.measurements_by_model_battery.setdefault(key_str, {})
                    per_item.setdefault(env.item_key, []).append(result.score)

                    if state.total_tokens >= token_cap:
                        state.stopped_at_cap = True
                        state.stopped_reason = (
                            f"Token cap reached at call {state.total_calls} "
                            f"(tokens: {state.total_tokens:,})."
                        )
                        break
                # Update the run row with final token totals.
                if record_to_db and conn is not None:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE hr.run SET finished_at = %s, total_tokens = %s, "
                            "infra_ok = %s WHERE run_id = %s",
                            (datetime.now(timezone.utc), round_total_tokens, round_infra_ok, round_id),
                        )
                    conn.commit()
                mean_score = sum(r for items_scores in per_item.values() for r in items_scores) / max(1, state.total_calls) if round_total_tokens else 0.0
                print(
                    f"  [{state.total_calls}] {model_id} / {battery_code} / round {round_num} "
                    f"tokens_this_round={round_total_tokens:,} total={state.total_tokens:,}"
                )


# ---------------------------------------------------------------------------
# Read back from DB (for --separation)
# ---------------------------------------------------------------------------
def read_separation_from_db(sweep_id: str) -> dict[str, list[dict]]:
    """Load the persisted separation matrix for a sweep."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT battery_id, model_a, model_b, p_separated, p_weak, p_tie "
                "FROM hr.separation WHERE sweep_id = %s ORDER BY battery_id, model_a, model_b",
                (sweep_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    sep: dict[str, list[dict]] = {}
    for battery_id, a, b, ps, pw, pt in rows:
        battery_code = battery_id.replace("battery-", "")
        sep.setdefault(battery_code, []).append(
            {
                "model_a": a,
                "model_b": b,
                "p_separated": float(ps),
                "p_weak": float(pw),
                "p_tie": float(pt),
            }
        )
    return sep


def list_sweeps() -> list[tuple[str, str, str]]:
    """Return list of (sweep_id, purpose, created_at) from the DB."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sweep_id, purpose, created_at FROM hr.sweep "
                "WHERE seat_code = %s ORDER BY created_at DESC",
                (STAGE0_SEAT_CODE,),
            )
            return cur.fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hr2.stage0", description="Stage 0 full-pool sweep runner.")
    p.add_argument("--dry-run", action="store_true", help="Print call plan (no API calls)")
    p.add_argument("--pilot", action="store_true", help="Run pilot n=3 for all models")
    p.add_argument(
        "--separation",
        action="store_true",
        help="Read separation matrix from DB for the most recent stage0 sweep",
    )
    p.add_argument(
        "--sweep-id",
        default=None,
        help="Sweep ID to query for --separation (default: latest stage0 sweep)",
    )
    p.add_argument("--token-cap", type=int, default=None, help="Token budget cap (default: configs/thresholds.yaml stage0.token_cap)")
    p.add_argument("--n-initial", type=int, default=3, help="Number of pilot repetitions")
    p.add_argument(
        "--models",
        default=None,
        help="Comma-separated model ids to sweep (subset of the pool; e.g. for appending new models)",
    )
    p.add_argument(
        "--item-repo",
        default=None,
        help="Path to the item repository (default: HR_ITEMREPO env or HR_HOME/itemrepo)",
    )
    p.add_argument(
        "--no-db",
        action="store_true",
        help="Do not record to the DB (for testing)",
    )
    return p


def _cli_main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if args.token_cap is None:
        args.token_cap = _stage0_token_cap()

    if args.dry_run:
        # Load items and print plan.
        from hr.calibrate import load_item_repo

        models = fleet_models()
        if args.models:
            wanted = [m.strip() for m in args.models.split(",") if m.strip()]
            unknown = [m for m in wanted if m not in models]
            if unknown:
                print(f"Unknown model ids: {unknown}", file=sys.stderr)
                return 1
            models = tuple(wanted)
        item_repo = Path(args.item_repo) if args.item_repo else itemrepo_path()
        items_by_battery = load_item_repo(item_repo, batteries=list(STAGE0_BATTERIES))
        subsets = select_subsets(items_by_battery)
        plan = build_call_plan(
            subsets, models=models, n_initial=args.n_initial, budget_cap=args.token_cap
        )
        print_call_plan(plan)
        return 0

    if args.separation:
        sweep_id = args.sweep_id
        if sweep_id is None:
            try:
                sweeps = list_sweeps()
            except Exception as e:
                print(f"DB not available: {e}", file=sys.stderr)
                return 1
            if not sweeps:
                print("No Stage 0 sweeps recorded yet.", file=sys.stderr)
                return 1
            sweep_id = sweeps[0][0]
        sep = read_separation_from_db(sweep_id)
        print(f"Sweep ID: {sweep_id}")
        _print_matrix(sep)
        return 0

    if not (args.pilot or args.separation or args.dry_run):
        # Default: run the full sweep (with live adapter).
        pass

    # Build live adapter. Pool may now include multiple provider families
    # (bailian-token-plan + kimi-for-coding via Anthropic, deepseek via OpenAI),
    # so use the routed adapter that dispatches per model_id.
    from hr.adapters import RoutedAdapter

    models = fleet_models()
    if args.models:
        wanted = [m.strip() for m in args.models.split(",") if m.strip()]
        unknown = [m for m in wanted if m not in models]
        if unknown:
            print(f"Unknown model ids: {unknown}", file=sys.stderr)
            return 1
        models = tuple(wanted)

    adapter = RoutedAdapter()
    item_repo = Path(args.item_repo) if args.item_repo else itemrepo_path()
    try:
        run_sweep(
            adapter=adapter,
            item_repo=item_repo,
            models=models,
            n_initial=args.n_initial,
            token_cap=args.token_cap,
            record_to_db=not args.no_db,
        )
    except Exception as e:
        print(f"Stage 0 failed: {e}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    return _cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
