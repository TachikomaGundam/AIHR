from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from hr.graders.base import ModelResponse

CalibrationStatus = Literal["pass", "fail", "inconclusive", "invalid"]


class _AdapterFacade(Protocol):
    def probe_capabilities(self, model_id: str) -> Any: ...
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
    scorer_name: str | None = None
    scorer_version: str | None = None


@dataclass
class TierBandVerdict:
    battery: str
    tier: int
    anchor: str
    pass_rate: float
    band_lo: float
    band_hi: float
    passed: bool
    status: CalibrationStatus = "pass"


@dataclass
class BatteryVerdict:
    battery: str
    anchor: str
    tier_verdicts: list[TierBandVerdict]
    passed: bool
    status: CalibrationStatus = "pass"

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
def print_rendered_report(report: CalibrationReport) -> None:
    print("== hr calibration report ==")
    print(f"pool_hash: {report.pool_hash}")
    print(
        f"tokens: in={report.total_tokens_in} out={report.total_tokens_out} "
        f"total={report.total_tokens_in + report.total_tokens_out}"
    )
    if report.stopped_at_cap:
        print("WARNING: stopped at token cap mid-sweep")
    print()
    for bv in report.verdicts:
        status = bv.status.upper()
        print(f"[{status}] battery={bv.battery} anchor={bv.anchor}")
        for tv in bv.tier_verdicts:
            mark = "OK" if tv.passed else tv.status.upper()
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
                "status": bv.status,
                "tiers": [
                    {
                        "tier": tv.tier,
                        "pass_rate": tv.pass_rate,
                        "band_lo": tv.band_lo,
                        "band_hi": tv.band_hi,
                        "passed": tv.passed,
                        "status": tv.status,
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
                "scorer_name": m.scorer_name,
                "scorer_version": m.scorer_version,
            }
            for m in report.measurements
        ],
    }


__all__ = ["_AdapterFacade", "CalibrationStatus", "Measurement", "TierBandVerdict", "BatteryVerdict", "CalibrationReport", "print_rendered_report", "_report_to_dict"]
