"""Battery x field acceptance matrix — permanent vaccine (2026-10-15 order).

0.6.5 shipped "finish_reason full chain" while only 2 of 11 batteries actually
carried the field (reasoning/code_gen were rebuilt in the same commit; the nine
common-path runners dropped it at assembly). Unit gates were all green — the
missing test was exactly this file: every battery end-to-end through the real
runner stack, asserting the declared per-battery column is non-None on the
outcome. A runner that drops a wiring field now fails here, not on the customer
testbed at 3am.
"""
from __future__ import annotations

from typing import Any

import pytest

from hr.adapters.base import Capabilities
from hr.bench.livebench import LIVEBENCH_BATTERIES
from hr.bench.engine import LivebenchEngine
from hr.graders.base import ModelResponse
import hr.bench.engine as engine_mod

WIRE = "stop"


class _WireAdapter:
    """Answers anything with a compliant-length reply and a real wire
    finish_reason; capabilities open so no battery skips as not_applicable."""

    def probe_capabilities(self, model_id: str) -> Capabilities:
        return Capabilities(
            model_id=model_id,
            provider="fake",
            supports_thinking=True,
            supports_vision=True,
        )

    def chat(self, model_id: str, messages: list[dict[str, Any]], **kw: Any):
        return ModelResponse(
            text="42",
            latency_ms=50,
            tokens_in=10,
            tokens_out=10,
            finish_reason=WIRE,
        )


def test_battery_finish_reason_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_mod, "adapter_for", lambda model_id: _WireAdapter())
    engine = LivebenchEngine()
    for battery in LIVEBENCH_BATTERIES:
        out = engine.run_battery("fake/model", battery)
        if out.status == "not_applicable":
            continue  # capability-gated skip is legal; capabilities are open here
        assert out.finish_reason == WIRE, (
            f"battery {battery.name}: run-level finish_reason lost — runner or "
            f"assembly dropped the wire field (0.6.5 bug class, "
            f".omo/evidence/p5-retired-20261014/ FR-NULL-CASE.md)"
        )
