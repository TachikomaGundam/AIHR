from __future__ import annotations

from pathlib import Path
from typing import Any

from hr.calibration_items import ACCEPTANCE_BANDS, BATTERY_TYPES, EST_TOKENS_PER_CALL, TOKEN_CAP, _ROUTING, build_grading_params, build_messages, load_anchors, load_item_repo, maybe_vision_image
from hr.calibration_models import _AdapterFacade, BatteryVerdict, CalibrationReport, CalibrationStatus, Measurement, TierBandVerdict
from hr.calibration_persistence import CalibrationPersistenceMixin
from hr.graders import build_default_registry
from hr.graders.base import GradeResult, ModelResponse
from hr.items.schema import ItemEnvelope, ItemType
from hr.stage0_storage import resolve_scorer_identity

class CalibrationRunner(CalibrationPersistenceMixin):
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
        self._recorded_measurements: list[Measurement] = []

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
        total_tokens = sum(
            measurement.tokens_in + measurement.tokens_out
            for measurement in self._recorded_measurements
        )
        stopped_at_cap = total_tokens >= self.token_cap

        for battery in self.batteries:
            if stopped_at_cap:
                break
            items = items_by_battery.get(battery, [])
            for anchor_key, anchor_id in self.anchors.items():
                for env in items:
                    if self._recorded(anchor_key, env.item_key):
                        continue
                    scorer_name, scorer_version = resolve_scorer_identity(
                        env.type.value
                    )
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
                                scorer_name=scorer_name,
                                scorer_version=scorer_version,
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
                                scorer_name=scorer_name,
                                scorer_version=scorer_version,
                            )
                        )
                    if total_tokens >= self.token_cap:
                        stopped_at_cap = True
                        break
                if stopped_at_cap:
                    break
            if stopped_at_cap:
                break

        report_measurements = [*self._recorded_measurements, *measurements]
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
                    if tier not in tier_counts:
                        continue
                    expected_keys = {
                        env.item_key for env in items if env.tier == tier
                    }
                    tier_measurements = [
                        measurement
                        for measurement in measurements
                        if measurement.battery == battery
                        and measurement.anchor == anchor_key
                        and measurement.tier == tier
                    ]
                    measured_keys = {
                        measurement.item_key for measurement in tier_measurements
                    }
                    infra_failed = any(
                        measurement.infra_failure is not None
                        for measurement in tier_measurements
                    )
                    count = len(expected_keys)
                    passed = sum(
                        measurement.passed for measurement in tier_measurements
                    )
                    rate = passed / count if count else 0.0
                    if measured_keys != expected_keys or infra_failed:
                        status = "inconclusive"
                    elif lo <= rate <= hi:
                        status = "pass"
                    else:
                        status = "fail"
                    tier_verdicts.append(
                        TierBandVerdict(
                            battery=battery,
                            tier=tier,
                            anchor=anchor_key,
                            pass_rate=rate,
                            band_lo=lo,
                            band_hi=hi,
                            passed=status == "pass",
                            status=status,
                        )
                    )
                statuses = {verdict.status for verdict in tier_verdicts}
                if not tier_verdicts or "invalid" in statuses:
                    battery_status: CalibrationStatus = "invalid"
                elif "inconclusive" in statuses:
                    battery_status = "inconclusive"
                elif "fail" in statuses:
                    battery_status = "fail"
                else:
                    battery_status = "pass"
                verdicts.append(
                    BatteryVerdict(
                        battery=battery,
                        anchor=anchor_key,
                        tier_verdicts=tier_verdicts,
                        passed=battery_status == "pass",
                        status=battery_status,
                    )
                )
        return verdicts

    # ------------------------------------------------------------------


__all__ = ["CalibrationRunner"]
