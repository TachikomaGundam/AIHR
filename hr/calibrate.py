"""hr2.calibrate — anchor calibration runner (spec §11).

Loads the Stage-0 item banks from :mod:`hr2.items.loader`, calls each
anchor model through the adapter resolved by :func:`hr2.adapters.adapter_for`
(provider wire decided by the fleet config), grades
the responses through the per-battery grader, aggregates per-(battery,
tier, anchor) pass rates, and checks them against the §11 acceptance
bands.

Three anchors (spec/§9.1, raised to 8M tokens by §9.3):
  - cheap   = ``bailian-token-plan/deepseek-v4-flash`` (no thinking)
  - mid     = ``bailian-token-plan/qwen3.7-plus``
  - expensive = ``bailian-token-plan/glm-5.2``

Per-battery grader routing:
  - reasoning     -> ``constraint@1.0``
  - factuality_qa / unanswerable / citation -> ``exact_match@1.0`` (with
    routing-specific ``params.expected`` built from the payload)
  - tool_a        -> ``schema_valid@1.0``
  - vision        -> ``exact_match@1.0`` (the model reads a base64 PNG)

Usage:
    python3 -m hr2.calibrate --dry-run
    python3 -m hr2.calibrate --anchors cheap,mid --resume
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from hr.adapters import adapter_for
from hr.config import itemrepo_path, load_yaml
from hr.graders.base import GradeResult, ModelResponse
from hr.graders import build_default_registry
from hr.items.loader import ItemLoader
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


# ---------------------------------------------------------------------------
# Adapter facade (kept thin so tests can inject a FakeAdapter)
# ---------------------------------------------------------------------------
class _AdapterFacade(Protocol):
    def probe_capabilities(self, model_id: str) -> Any: ...
    def chat(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        images: list[dict[str, Any]] | None = None,
        thinking_budget: int | None = None,
        max_output: int = 16384,
        timeout_s: int = 600,
    ) -> ModelResponse: ...


# ---------------------------------------------------------------------------
# Measurement record (per call)
# ---------------------------------------------------------------------------
@dataclass
class Measurement:
    anchor: str
    item_key: str
    battery: str
    tier: int
    item_type: str
    score: float
    passed: bool
    latency_ms: int
    tokens_in: int
    tokens_out: int
    detail: dict[str, Any] = field(default_factory=dict)
    infra_failure: str | None = None


@dataclass
class TierBandVerdict:
    battery: str
    tier: int
    anchor: str
    pass_rate: float
    band_lo: float
    band_hi: float
    passed: bool


@dataclass
class BatteryVerdict:
    battery: str
    anchor: str
    tier_verdicts: list[TierBandVerdict]
    passed: bool

    @property
    def failing_tiers(self) -> list[TierBandVerdict]:
        return [v for v in self.tier_verdicts if not v.passed]


@dataclass
class CalibrationReport:
    pool_hash: str
    measurements: list[Measurement]
    verdicts: list[BatteryVerdict]
    stopped_at_cap: bool = False
    total_tokens_in: int = 0
    total_tokens_out: int = 0

    @property
    def all_passed(self) -> bool:
        return all(v.passed for v in self.verdicts)


# ---------------------------------------------------------------------------
# Message construction
# ---------------------------------------------------------------------------
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
# ---------------------------------------------------------------------------
class CalibrationRunner:
    """Runs the Stage-0 calibration sweep."""

    def __init__(
        self,
        adapter: _AdapterFacade,
        item_repo: Path,
        *,
        registry: Any | None = None,
        anchors: dict[str, str] | None = None,
        batteries: list[str] | None = None,
        token_cap: int = TOKEN_CAP,
        db: Any | None = None,
        pool_hash: str = "",
        resume: bool = False,
    ) -> None:
        self.adapter = adapter
        self.item_repo = item_repo
        self.registry = registry or build_default_registry()
        self.anchors = dict(anchors) if anchors else load_anchors()
        self.batteries = list(batteries or BATTERY_TYPES.keys())
        self.token_cap = token_cap
        self.db = db
        self.pool_hash = pool_hash
        self.resume = resume
        self._recorded_pairs: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------
    def _load_recorded_pairs(self) -> None:
        """If ``resume`` and a DB are present, preload (anchor, item_key)
        pairs already present in ``measurement`` for the current pool.
        """
        if not (self.resume and self.db is not None):
            return
        try:
            with self.db.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT r.model_id, m.item_key
                        FROM hr2.measurement m
                        JOIN hr2.run r ON r.id = m.run_id
                        JOIN hr2.sweep s ON s.id = r.sweep_id
                        WHERE s.pool_hash = %s
                        """,
                        (self.pool_hash,),
                    )
                    for row in cur.fetchall():
                        self._recorded_pairs.add((row[0], row[1]))
        except Exception:
            # DB not reachable — fall back to non-resumed execution.
            self._recorded_pairs = set()

    # ------------------------------------------------------------------
    def _call(self, anchor: str, envelope: ItemEnvelope) -> tuple[
        ModelResponse | None, dict[str, Any]
    ]:
        messages = build_messages(envelope)
        images = maybe_vision_image(envelope, self.item_repo)
        cap = self.adapter.probe_capabilities(anchor)
        thinking_budget = 8192 if cap.supports_thinking else None
        # tool_a items ship the offered tool schemas in the payload; without
        # forwarding them the model can only answer in prose and every
        # schema_valid grade collapses to zero.
        tools = (
            envelope.payload.get("tools")
            if envelope.type == ItemType.TOOL_A
            else None
        )
        try:
            resp = self.adapter.chat(
                anchor,
                messages,
                images=images,
                tools=tools,
                thinking_budget=thinking_budget,
                max_output=16384,
                timeout_s=600,
            )
            return resp, {"ok": True}
        except Exception as e:
            return None, {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ------------------------------------------------------------------
    def _grade(
        self, envelope: ItemEnvelope, response: ModelResponse
    ) -> GradeResult:
        routing = _ROUTING.get(envelope.type)
        if routing is None:
            return GradeResult(score=0.0, passed=False, detail={"no_routing": True})
        grader_spec, _ = routing
        try:
            grader = self.registry.get(grader_spec)
        except Exception as e:
            return GradeResult(
                score=0.0, passed=False, detail={"grader_error": str(e)}
            )
        params = build_grading_params(envelope)
        try:
            return grader.grade(envelope.payload, params, response)
        except Exception as e:
            return GradeResult(
                score=0.0, passed=False, detail={"grader_error": str(e)}
            )

    # ------------------------------------------------------------------
    def _recorded(self, anchor: str, item_key: str) -> bool:
        return (anchor, item_key) in self._recorded_pairs

    # ------------------------------------------------------------------
    def run(self) -> CalibrationReport:
        items_by_battery = load_item_repo(
            self.item_repo, batteries=self.batteries
        )
        self._load_recorded_pairs()

        measurements: list[Measurement] = []
        total_tokens = 0
        stopped_at_cap = False

        for battery in self.batteries:
            items = items_by_battery.get(battery, [])
            for anchor_key, anchor_id in self.anchors.items():
                for env in items:
                    if self._recorded(anchor_id, env.item_key):
                        continue
                    resp, meta = self._call(anchor_id, env)
                    if resp is None:
                        measurements.append(
                            Measurement(
                                anchor=anchor_key,
                                item_key=env.item_key,
                                battery=battery,
                                tier=env.tier,
                                item_type=env.type.value,
                                score=0.0,
                                passed=False,
                                latency_ms=0,
                                tokens_in=0,
                                tokens_out=0,
                                infra_failure=str(meta.get("error", "unknown")),
                            )
                        )
                        total_tokens += EST_TOKENS_PER_CALL
                    else:
                        gr = self._grade(env, resp)
                        total_tokens += resp.tokens_in + resp.tokens_out
                        measurements.append(
                            Measurement(
                                anchor=anchor_key,
                                item_key=env.item_key,
                                battery=battery,
                                tier=env.tier,
                                item_type=env.type.value,
                                score=gr.score,
                                passed=gr.passed,
                                latency_ms=resp.latency_ms,
                                tokens_in=resp.tokens_in,
                                tokens_out=resp.tokens_out,
                                detail=gr.detail,
                            )
                        )
                    if total_tokens >= self.token_cap:
                        stopped_at_cap = True
                        break
                if stopped_at_cap:
                    break
            if stopped_at_cap:
                break

        report_measurements = measurements
        total_in = sum(m.tokens_in for m in report_measurements)
        total_out = sum(m.tokens_out for m in report_measurements)

        verdicts = self._evaluate(
            report_measurements, items_by_battery
        )

        report = CalibrationReport(
            pool_hash=self.pool_hash,
            measurements=report_measurements,
            verdicts=verdicts,
            stopped_at_cap=stopped_at_cap,
            total_tokens_in=total_in,
            total_tokens_out=total_out,
        )
        self._persist(report)
        return report

    # ------------------------------------------------------------------
    def _evaluate(
        self,
        measurements: list[Measurement],
        items_by_battery: dict[str, list[ItemEnvelope]],
    ) -> list[BatteryVerdict]:
        verdicts: list[BatteryVerdict] = []
        for battery in self.batteries:
            items = items_by_battery.get(battery, [])
            tier_counts: dict[int, int] = {}
            for env in items:
                tier_counts[env.tier] = tier_counts.get(env.tier, 0) + 1

            for anchor_key in self.anchors:
                tier_verdicts: list[TierBandVerdict] = []
                for tier, band in ACCEPTANCE_BANDS.items():
                    _, lo, hi = band
                    count = tier_counts.get(tier, 0)
                    if count == 0:
                        continue
                    passed = sum(
                        1 for m in measurements
                        if m.battery == battery
                        and m.anchor == anchor_key
                        and m.tier == tier
                        and m.passed
                    )
                    rate = passed / count if count else 0.0
                    in_band = lo <= rate <= hi
                    tier_verdicts.append(
                        TierBandVerdict(
                            battery=battery,
                            tier=tier,
                            anchor=anchor_key,
                            pass_rate=rate,
                            band_lo=lo,
                            band_hi=hi,
                            passed=in_band,
                        )
                    )
                verdicts.append(
                    BatteryVerdict(
                        battery=battery,
                        anchor=anchor_key,
                        tier_verdicts=tier_verdicts,
                        passed=all(v.passed for v in tier_verdicts),
                    )
                )
        return verdicts

    # ------------------------------------------------------------------
    def _persist(self, report: CalibrationReport) -> None:
        if self.db is None:
            return
        try:
            from hr import db as hdb  # local import

            with self.db.connect() as conn:
                with conn.cursor() as cur:
                    for m in report.measurements:
                        cur.execute(
                            """
                            INSERT INTO hr2.calibration_event
                                (pool_hash, anchor, item_key, battery,
                                 tier, score, tokens_in, tokens_out,
                                 latency_ms, infra_failure)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT DO NOTHING
                            """,
                            (
                                report.pool_hash,
                                m.anchor,
                                m.item_key,
                                m.battery,
                                m.tier,
                                m.score,
                                m.tokens_in,
                                m.tokens_out,
                                m.latency_ms,
                                m.infra_failure,
                            ),
                        )
                conn.commit()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Dry-run report
# ---------------------------------------------------------------------------
def dry_run_report(
    item_repo: Path,
    *,
    anchors: dict[str, str] | None = None,
    batteries: list[str] | None = None,
    token_cap: int = TOKEN_CAP,
) -> str:
    """Human-readable dry-run plan without any API calls."""
    anchors = dict(anchors) if anchors else load_anchors()
    wanted = list(batteries or BATTERY_TYPES.keys())
    items_by_battery = load_item_repo(item_repo, batteries=wanted)

    lines: list[str] = ["== hr2 calibration --dry-run ==", ""]
    lines.append("Item counts per battery:")
    total_items = 0
    for bat in wanted:
        n = len(items_by_battery.get(bat, []))
        total_items += n
        lines.append(f"  {bat}: {n}")
    lines.append(f"  TOTAL: {total_items}")
    lines.append("")

    lines.append("Anchors:")
    for key, model_id in anchors.items():
        lines.append(f"  {key}: {model_id}")
    lines.append("")

    total_calls = total_items * len(anchors)
    est_tokens = total_calls * EST_TOKENS_PER_CALL
    est_wall = (total_calls / max(len(anchors), 1)) / CONCURRENCY_PER_PROVIDER
    lines.append(f"Total calls: {total_calls}")
    lines.append(
        f"Estimated tokens: {est_tokens} "
        f"(cap = {token_cap}, {EST_TOKENS_PER_CALL}/call est.)"
    )
    lines.append(
        f"Estimated wall-clock: ~{est_wall:0.1f}s at "
        f"{CONCURRENCY_PER_PROVIDER} concurrent/provider"
    )
    lines.append("")

    lines.append("Acceptance bands (spec §11):")
    for tier, (label, lo, hi) in ACCEPTANCE_BANDS.items():
        lines.append(f"  tier{tier} ({label}): [{lo:0.0%}, {hi:0.0%}]")
    lines.append("")
    lines.append("Batteries checked:")
    for bat in wanted:
        lines.append(f"  - {bat}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hr2.calibrate",
        description="Stage-0 anchor calibration runner",
    )
    parser.add_argument(
        "--item-repo",
        type=Path,
        default=None,
        help="Path to item repo (default: HR_ITEMREPO env or HR_HOME/itemrepo)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print call plan WITHOUT calling APIs and exit",
    )
    parser.add_argument(
        "--anchors",
        type=str,
        default=None,
        help="Comma-separated anchor keys "
        "(e.g. 'cheap,mid' or 'cheap,mid,expensive')",
    )
    parser.add_argument(
        "--batteries",
        type=str,
        default=None,
        help="Comma-separated battery names (default: all Stage-0)",
    )
    parser.add_argument(
        "--token-cap",
        type=int,
        default=TOKEN_CAP,
        help=f"Token budget cap (default: {TOKEN_CAP})",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip (anchor, item) pairs already recorded",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON report after run",
    )
    args = parser.parse_args(argv)

    item_repo = args.item_repo if args.item_repo is not None else itemrepo_path()

    anchor_keys = (
        [k.strip() for k in args.anchors.split(",")] if args.anchors else None
    )
    anchors: dict[str, str] | None = None
    if anchor_keys:
        known = load_anchors()
        anchors = {k: known[k] for k in anchor_keys if k in known}
        missing = [k for k in anchor_keys if k not in known]
        if missing:
            print(f"unknown anchor: {missing}", file=sys.stderr)
            return 2

    batteries = (
        [b.strip() for b in args.batteries.split(",")]
        if args.batteries
        else None
    )

    if args.dry_run:
        print(
            dry_run_report(
                item_repo,
                anchors=anchors,
                batteries=batteries,
                token_cap=args.token_cap,
            )
        )
        return 0

    # Live run path — route anchors through adapter_for (provider config
    # decides the wire; the current anchors all resolve to the Anthropic
    # adapter, but a future re-pointing needs no code change here).
    adapter = adapter_for(load_anchors()["cheap"])
    try:
        from hr import db as hdb
        db_conn = hdb
    except Exception:
        db_conn = None

    runner = CalibrationRunner(
        adapter=adapter,
        item_repo=item_repo,
        anchors=anchors,
        batteries=batteries,
        token_cap=args.token_cap,
        db=db_conn,
        pool_hash=_compute_pool_hash(item_repo, batteries),
        resume=args.resume,
    )
    report = runner.run()
    print_rendered_report(report)
    if args.json:
        print(json.dumps(_report_to_dict(report), indent=2, default=str))
    return 0 if report.all_passed else 1


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
    joined = "\n".join(hashes)
    return "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()


def print_rendered_report(report: CalibrationReport) -> None:
    print("== hr2 calibration report ==")
    print(f"pool_hash: {report.pool_hash}")
    print(
        f"tokens: in={report.total_tokens_in} out={report.total_tokens_out} "
        f"total={report.total_tokens_in + report.total_tokens_out}"
    )
    if report.stopped_at_cap:
        print("WARNING: stopped at token cap mid-sweep")
    print()
    for bv in report.verdicts:
        status = "PASS" if bv.passed else "FAIL"
        print(f"[{status}] battery={bv.battery} anchor={bv.anchor}")
        for tv in bv.tier_verdicts:
            mark = "OK" if tv.passed else "X"
            print(
                f"  [{mark}] tier{tv.tier}: "
                f"{tv.pass_rate:0.1%} "
                f"(band [{tv.band_lo:0.0%},{tv.band_hi:0.0%}])"
            )
        print()


def _report_to_dict(report: CalibrationReport) -> dict[str, Any]:
    return {
        "pool_hash": report.pool_hash,
        "stopped_at_cap": report.stopped_at_cap,
        "total_tokens_in": report.total_tokens_in,
        "total_tokens_out": report.total_tokens_out,
        "verdicts": [
            {
                "battery": bv.battery,
                "anchor": bv.anchor,
                "passed": bv.passed,
                "tiers": [
                    {
                        "tier": tv.tier,
                        "pass_rate": tv.pass_rate,
                        "band_lo": tv.band_lo,
                        "band_hi": tv.band_hi,
                        "passed": tv.passed,
                    }
                    for tv in bv.tier_verdicts
                ],
            }
            for bv in report.verdicts
        ],
        "measurements": [
            {
                "anchor": m.anchor,
                "item_key": m.item_key,
                "battery": m.battery,
                "tier": m.tier,
                "score": m.score,
                "passed": m.passed,
                "tokens_in": m.tokens_in,
                "tokens_out": m.tokens_out,
                "latency_ms": m.latency_ms,
                "infra_failure": m.infra_failure,
            }
            for m in report.measurements
        ],
    }


def main() -> int:
    return _cli()


if __name__ == "__main__":
    raise SystemExit(main())
