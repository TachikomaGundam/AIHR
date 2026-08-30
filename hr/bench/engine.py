from __future__ import annotations

import hashlib
import os
import random
import time
from typing import Callable

from hr.adapters import adapter_for
from hr.adapters.base import Adapter, AdapterError, Capabilities, ChatRequest
from hr.bench.engine_results import BenchOutcome, ItemResult, _RunResult, make_sweep_id
from hr.bench.manifest import ExperimentManifest
from hr.bench.engine_interactive import EngineInteractiveMixin
from hr.bench.engine_runners import EngineRunnersMixin
from hr.bench.engine_storage import EngineStorageMixin, SEAT_CODE
from hr.bench.livebench import battery_code, battery_item_id, battery_item_labels
from hr.bench.scorers import _BenchmarkOutcome
from hr.graders.base import ModelResponse
from hr.models import BenchmarkCategory

_TRANSIENT_STATUS = (429, 400, 408, 500, 502, 503, 504)

class LivebenchEngine(EngineStorageMixin, EngineRunnersMixin, EngineInteractiveMixin):
    _RUNNERS = {
        BenchmarkCategory.code_gen: EngineRunnersMixin._run_code_gen,
        BenchmarkCategory.reasoning: EngineRunnersMixin._run_reasoning,
        BenchmarkCategory.instruction_follow: EngineRunnersMixin._run_instruction_follow,
        BenchmarkCategory.tool_use: EngineInteractiveMixin._run_tool_use,
        BenchmarkCategory.long_context: EngineRunnersMixin._run_long_context,
        BenchmarkCategory.attention_probe: EngineRunnersMixin._run_attention_probe,
        BenchmarkCategory.attention_stress: EngineInteractiveMixin._run_attention_stress,
        BenchmarkCategory.vision: EngineRunnersMixin._run_vision,
        BenchmarkCategory.speed: EngineRunnersMixin._run_speed,
        BenchmarkCategory.long_horizon: EngineRunnersMixin._run_long_horizon,
    }
    def __init__(
        self,
        *,
        adapter_factory: Callable[[str], Adapter] | None = None,
        timeout_s: int = 600,
        seed: int = 0,
    ) -> None:
        self._adapter_factory = adapter_factory or adapter_for
        self._timeout_s = timeout_s
        self._seed = seed

    def _rng_for(self, model_id: str, battery: BenchmarkCategory) -> random.Random:
        material = f"{self._seed}|{model_id}|{battery.value}".encode("utf-8")
        return random.Random(int.from_bytes(hashlib.sha256(material).digest()[:8]))

    def manifest(
        self, model_ids: list[str], batteries: list[BenchmarkCategory]
    ) -> ExperimentManifest:
        import sys
        from hr.graders.base import GRADER_VERSION

        runtime_info = {
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        }
        try:
            import importlib.metadata
            runtime_info["hr_agent"] = importlib.metadata.version("hr-agent")
        except Exception:
            pass

        return ExperimentManifest.create(
            seed=self._seed,
            model_ids=model_ids,
            batteries=batteries,
            code_revision=os.environ.get("HR_CODE_REVISION", "unknown"),
            grader_version=GRADER_VERSION,
            runtime_info=runtime_info,
        )

    # ------------------------------------------------------------------
    # Config guard: every battery needs a thresholds.yaml entry (mirrors
    # stage1's SequentialConfig.from_yaml required-batteries validation).
    # ------------------------------------------------------------------
    def require_thresholds(self, batteries: list[BenchmarkCategory]) -> None:
        """Raise ValueError naming any battery missing from thresholds.yaml."""
        from hr.config import config_path
        from hr.stats.sequential import SequentialConfig

        SequentialConfig.from_yaml(
            str(config_path("thresholds.yaml")),
            required_batteries=[battery_code(b) for b in batteries],
        )
    def _chat(
        self,
        model_id: str,
        adapter: Adapter,
        caps: Capabilities,
        cr: ChatRequest,
    ) -> ModelResponse:
        """Send one ChatRequest through the adapter.

        The capability overlay decides thinking: a model without the thinking
        flag never receives a thinking block, regardless of what the
        benchmark requested (adapter-level contract, mirrors calibrate).
        """
        if not caps.supports_thinking:
            cr.thinking_budget = None
        return adapter.chat(
            cr.model_id,
            cr.messages,
            images=cr.images,
            tools=cr.tools,
            thinking_budget=cr.thinking_budget,
            max_output=cr.max_output,
            timeout_s=cr.timeout_s,
        )

    def _chat_with_retry(
        self,
        model_id: str,
        adapter: Adapter,
        caps: Capabilities,
        cr: ChatRequest,
    ) -> ModelResponse:
        """One ChatRequest with per-turn transient retry (up to 3 attempts).

        Mirrors the transient classification and ``min(2**attempt, 8)``
        backoff of :meth:`run_battery`, but scoped to a SINGLE turn: a 503 on
        turn 17 must not discard a 20-turn conversation that already cost 16
        completed turns. Non-transient errors and exhausted retries re-raise
        so :meth:`run_battery` records the failed run as today.
        """
        for attempt in range(2):
            try:
                return self._chat(model_id, adapter, caps, cr)
            except AdapterError as e:
                if e.status_code in _TRANSIENT_STATUS:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise
        return self._chat(model_id, adapter, caps, cr)

    def _single_call(
        self,
        model_id: str,
        adapter: Adapter,
        caps: Capabilities,
        cr: ChatRequest,
        score: Callable[[str], _BenchmarkOutcome],
        *,
        combine_thinking: bool = False,
    ) -> _RunResult:
        """One ChatRequest + one scorer (most single-turn benchmarks)."""
        resp = self._chat(model_id, adapter, caps, cr)
        if combine_thinking:
            text = (resp.text + "\n" + resp.thinking).strip()
        else:
            text = resp.text
        return _RunResult(
            outcome=score(text),
            response_text=resp.text,
            thinking_text=resp.thinking,
            latency_ms=resp.latency_ms,
            tokens_in=resp.tokens_in,
            tokens_out=resp.tokens_out,
            requested_max_output=cr.max_output,
        )
    def run_battery(self, model_id: str, battery: BenchmarkCategory) -> BenchOutcome:
        """Run one battery for one model; never raises for model/battery-level
        faults — failures come back as a scored-0 outcome (v1 parity)."""
        if battery not in self._RUNNERS:
            raise ValueError(f"unknown benchmark: {battery}")

        failed = _BenchmarkOutcome(
            score=0.0, passed=False, raw_output="", status="inconclusive"
        )
        result = _RunResult(outcome=failed)
        try:
            adapter = self._adapter_factory(model_id)
            caps = adapter.probe_capabilities(model_id)
        except Exception as e:  # noqa: BLE001 — v1 parity: record, don't crash
            result = _RunResult(
                outcome=_BenchmarkOutcome(
                    score=0.0, passed=False, raw_output=f"ERROR: {e}",
                    status="inconclusive",
                )
            )
            return self._to_outcome(battery, model_id, result)

        runner = self._RUNNERS[battery]
        for attempt in range(3):
            try:
                result = runner(self, model_id, adapter, caps)
                if self._is_transient_failure(result.outcome, attempt):
                    time.sleep(min(2 ** attempt, 8))
                    continue
                break
            except AdapterError as e:
                result = _RunResult(
                    outcome=_BenchmarkOutcome(
                        score=0.0, passed=False, raw_output=f"ERROR: {e}",
                        status="inconclusive",
                    )
                )
                if e.status_code in _TRANSIENT_STATUS and attempt < 2:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                break
            except Exception as e:  # noqa: BLE001 — v1 parity
                result = _RunResult(
                    outcome=_BenchmarkOutcome(
                        score=0.0, passed=False, raw_output=f"ERROR: {e}",
                        status="inconclusive",
                    )
                )
                break

        return self._to_outcome(battery, model_id, result)

    @staticmethod
    def _is_transient_failure(outcome: _BenchmarkOutcome, attempt: int) -> bool:
        raw = outcome.raw_output or ""
        if "ERROR" not in raw or attempt >= 2:
            return False
        return any(f"HTTP {s}" in raw for s in _TRANSIENT_STATUS)

    def _to_outcome(
        self,
        battery: BenchmarkCategory,
        model_id: str,
        result: _RunResult,
    ) -> BenchOutcome:
        outcome = result.outcome
        labels = battery_item_labels(battery)
        items: list[ItemResult] = []
        if outcome.item_scores is not None:
            if len(outcome.item_scores) != len(labels):
                raise ValueError(
                    f"scorer returned {len(outcome.item_scores)} items for "
                    f"{battery.value}, registry expects {len(labels)}"
                )
            for (label, passed), expected in zip(outcome.item_scores, labels):
                if label != expected:
                    raise ValueError(
                        f"scorer label {label!r} != registry label {expected!r}"
                    )
                if len(labels) == 1:
                    # Single graded units keep the v1 graded score (e.g.
                    # tool_use 60 without tool, vision 85, speed tier).
                    score = outcome.score
                else:
                    score = 100.0 if passed else 0.0
                items.append(
                    ItemResult(
                        label=label,
                        item_id=battery_item_id(battery, label),
                        score=score,
                        passed=passed,
                    )
                )
        elif len(labels) == 1:
            items.append(
                ItemResult(
                    label=labels[0],
                    item_id=battery_item_id(battery, labels[0]),
                    score=outcome.score,
                    passed=outcome.passed,
                )
            )
        else:
            # Scorer failed before grading units -> every item of the battery
            # is recorded as failed (a run without measurements would vanish
            # from health aggregation and hide the failure).
            items = [
                ItemResult(
                    label=label,
                    item_id=battery_item_id(battery, label),
                    score=0.0,
                    passed=False,
                )
                for label in labels
            ]
        return BenchOutcome(
            battery=battery,
            model_id=model_id,
            score=outcome.score,
            passed=outcome.passed,
            status=outcome.status,
            items=items,
            latency_ms=result.latency_ms,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            raw_output=outcome.raw_output,
            response_text=result.response_text,
            thinking_text=result.thinking_text,
            requested_max_output=result.requested_max_output,
        )


__all__ = ["BenchOutcome", "ItemResult", "LivebenchEngine", "SEAT_CODE", "make_sweep_id"]
