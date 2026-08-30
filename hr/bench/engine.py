"""Livebench engine — runs the 10 batteries through hr.adapters (task 12).

ALL model calls go through :class:`hr.adapters.ChatRequest` (max_output,
images, tools, thinking_budget; no temperature — v1 never used one and the
unified adapters expose none). The bespoke SSE client and the deepseek
special-case of v1's benchmark engine are gone: wire handling, streaming,
retries and provider routing now live in hr.adapters.

Scoring keeps v1 semantics (see :mod:`hr.bench.scorers`); this module only
turns a scored outcome into hr2.measurement rows under the livebench
batteries (sweep/run/measurement, recording response text, thinking, and the
ACTUAL requested_max_output sent on the wire).
"""

from __future__ import annotations

import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from hr.adapters import adapter_for
from hr.adapters.base import Adapter, AdapterError, Capabilities, ChatRequest
from hr.bench import prompts
from hr.bench import stress_prompts
from hr.bench.livebench import (
    LIVEBENCH_BATTERIES,
    battery_code,
    battery_description,
    battery_item_id,
    battery_item_labels,
    seat_battery_bounds,
)
from hr.bench.scorers import (
    _BenchmarkOutcome,
    _safe_calculate,
    score_attention_probe,
    score_attention_stress,
    score_code_gen,
    score_instruction_follow,
    score_long_context,
    score_long_horizon,
    score_reasoning,
    score_speed,
    score_tool_use_text,
    score_vision,
    skip_vision_outcome,
)
from hr.graders.base import ModelResponse
from hr.models import BenchmarkCategory

#: Seat that owns livebench seat_battery links (same sentinel as stage-0).
SEAT_CODE = "_stage0_sweep"

#: v1 retry window: transient HTTP statuses are retried (attempt 0..2).
_TRANSIENT_STATUS = (429, 400, 408, 500, 502, 503, 504)

#: Tool definition for the tool_use benchmark (v1 shape, translated per wire).
_CALCULATE_TOOL: dict[str, Any] = {
    "name": "calculate",
    "description": "Evaluate a pure arithmetic expression (numbers, +-*/, ()).",
    "input_schema": {
        "type": "object",
        "properties": {"expression": {"type": "string"}},
        "required": ["expression"],
    },
}


@dataclass(frozen=True)
class ItemResult:
    """One graded unit of a battery run (one hr2.measurement row)."""

    label: str
    item_id: str
    score: float  # 0..100 (100/0 for binary units, v1 score for graded ones)
    passed: bool


@dataclass
class BenchOutcome:
    """Result of one (model, battery) run, ready for hr2 storage."""

    battery: BenchmarkCategory
    model_id: str
    score: float
    passed: bool
    items: list[ItemResult] = field(default_factory=list)
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    raw_output: str = ""
    response_text: str = ""
    thinking_text: str = ""
    requested_max_output: int = 16384


@dataclass(frozen=True)
class _RunResult:
    """Scored outcome + the response metadata for hr2.measurement rows."""

    outcome: _BenchmarkOutcome
    response_text: str = ""
    thinking_text: str = ""
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    requested_max_output: int = 16384


def make_sweep_id() -> str:
    """One sweep per bench invocation: livebench-<ts>-<rand>."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"livebench-{ts}-{uuid.uuid4().hex[:6]}"


class LivebenchEngine:
    """Runs the 10 livebench batteries over the unified adapters.

    ``adapter_factory`` is injectable for tests (default: the config-driven
    :func:`hr.adapters.adapter_for` router). The engine itself contains zero
    model names and zero wire protocol code.
    """

    def __init__(
        self,
        *,
        adapter_factory: Callable[[str], Adapter] | None = None,
        timeout_s: int = 600,
    ) -> None:
        self._adapter_factory = adapter_factory or adapter_for
        self._timeout_s = timeout_s

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

    # ------------------------------------------------------------------
    # Registration (idempotent, self-healing — same shape as tool_b)
    # ------------------------------------------------------------------
    def ensure_registered(self, conn) -> None:
        """Upsert batteries/items/seat links for all 10 livebench batteries.

        Uses the same stage0 upsert helpers as scripts/register_tool_b_battery.py
        and the CLI, so registration is idempotent on any DB (ON CONFLICT DO
        NOTHING) and self-heals FK prerequisites (the seat row is upserted
        first, exactly like the tool_b script learned to do).
        """
        from hr.stage0 import (
            _upsert_battery,
            _upsert_battery_item,
            _upsert_seat,
            _upsert_seat_battery,
        )

        _upsert_seat(conn, SEAT_CODE, "Stage-0 sweep")
        for battery in LIVEBENCH_BATTERIES:
            battery_id = _upsert_battery(
                conn, battery_code(battery), battery_description(battery)
            )
            for pos, label in enumerate(battery_item_labels(battery)):
                item_id = battery_item_id(battery, label)
                self._upsert_livebench_item(conn, item_id, battery_code(battery))
                _upsert_battery_item(conn, battery_id, item_id, pos)
            n_initial, n_max = seat_battery_bounds(battery)
            _upsert_seat_battery(conn, SEAT_CODE, battery_id, n_initial, n_max)

    @staticmethod
    def _upsert_livebench_item(conn, item_id: str, kind: str) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO hr.item_pool (item_id, item_code, version, domain, "
                "kind, json_meta) VALUES (%s, %s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (item_id) DO NOTHING",
                (item_id, item_id, "v1", kind, "livebench",
                 '{"kind": "livebench"}'),
            )
        conn.commit()

    # ------------------------------------------------------------------
    # Chat — single path through ChatRequest
    # ------------------------------------------------------------------
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
        for attempt in range(3):
            try:
                return self._chat(model_id, adapter, caps, cr)
            except AdapterError as e:
                if e.status_code in _TRANSIENT_STATUS and attempt < 2:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise

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

    # ------------------------------------------------------------------
    # Runners (each builds its ChatRequest exactly like v1's call params)
    # ------------------------------------------------------------------
    def _run_code_gen(self, model_id: str, adapter: Adapter, caps: Capabilities) -> _RunResult:
        return self._single_call(
            model_id, adapter, caps,
            ChatRequest(
                model_id=model_id,
                messages=[{"role": "user", "content": prompts.CODE_GEN_PROMPT}],
                thinking_budget=None,  # v1: disable_thinking=True
                max_output=32768,
                timeout_s=self._timeout_s,
            ),
            score_code_gen,
        )

    def _run_reasoning(self, model_id: str, adapter: Adapter, caps: Capabilities) -> _RunResult:
        return self._single_call(
            model_id, adapter, caps,
            ChatRequest(
                model_id=model_id,
                messages=[{"role": "user", "content": prompts.REASONING_PROMPT}],
                thinking_budget=4096,
                max_output=32768,
                timeout_s=self._timeout_s,
            ),
            score_reasoning,
            combine_thinking=True,
        )

    def _run_instruction_follow(self, model_id: str, adapter: Adapter, caps: Capabilities) -> _RunResult:
        return self._single_call(
            model_id, adapter, caps,
            ChatRequest(
                model_id=model_id,
                messages=[{"role": "user", "content": prompts.INSTRUCTION_PROMPT}],
                thinking_budget=None,  # v1: disable_thinking=True
                max_output=32768,
                timeout_s=self._timeout_s,
            ),
            score_instruction_follow,
        )

    def _run_tool_use(self, model_id: str, adapter: Adapter, caps: Capabilities) -> _RunResult:
        """Real multi-turn tool loop with the AST-guarded calculate tool.

        Graded on whatever final TOTAL the model emitted (v1 semantics); if
        the gateway rejects the tools field on turn 0, retry without tools
        and grade the text-only answer — never crash the suite.
        """
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": prompts.TOOL_TASK_PROMPT}
        ]
        tools: list[dict[str, Any]] | None = [dict(_CALCULATE_TOOL)]
        tool_used = False
        final_text = ""
        tokens_in = tokens_out = 0
        latency_ms = 0
        for turn in range(6):
            cr = ChatRequest(
                model_id=model_id,
                messages=list(messages),
                tools=tools,
                thinking_budget=(4096 if caps.supports_thinking else None),
                max_output=4096,
                timeout_s=self._timeout_s,
            )
            try:
                resp = self._chat(model_id, adapter, caps, cr)
            except AdapterError as e:
                if turn == 0 and tools and "tool" in str(e).lower():
                    # Gateway rejects the tools field -> retry without it.
                    tools = None
                    continue
                raise
            tokens_in += resp.tokens_in
            tokens_out += resp.tokens_out
            latency_ms += resp.latency_ms
            if resp.text:
                final_text = resp.text

            # Rebuild an assistant message honoring both text and tool calls.
            content: list[dict[str, Any]] = []
            if resp.text:
                content.append({"type": "text", "text": resp.text})
            for i, tc in enumerate(resp.tool_calls):
                content.append({
                    "type": "tool_use",
                    "id": tc.get("id") or f"toolu_{i}",
                    "name": tc.get("name", ""),
                    "input": tc.get("input") or {},
                })
            if not content:
                break
            messages.append({"role": "assistant", "content": content})

            calls = [b for b in content if b.get("type") == "tool_use"]
            if not calls:
                break
            tool_used = True
            for block in calls:
                name = block.get("name", "")
                tool_id = block.get("id", "")
                inp = block.get("input") or {}
                if name == "calculate":
                    result = _safe_calculate(str(inp.get("expression", "")))
                else:
                    result = f"ERROR: unknown tool '{name}'"
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": tool_id,
                         "content": str(result)}
                    ],
                })

        outcome = score_tool_use_text(final_text, tool_used)
        return _RunResult(
            outcome=outcome,
            response_text=final_text,
            latency_ms=latency_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            requested_max_output=4096,
        )

    def _run_long_context(self, model_id: str, adapter: Adapter, caps: Capabilities) -> _RunResult:
        haystack = prompts.build_haystack()
        full_prompt = haystack + "\n\n" + prompts.LONG_CONTEXT_FOLLOW_UP
        return self._single_call(
            model_id, adapter, caps,
            ChatRequest(
                model_id=model_id,
                messages=[{"role": "user", "content": full_prompt}],
                thinking_budget=8192,  # v1 default
                max_output=16384,
                timeout_s=self._timeout_s,
            ),
            score_long_context,
        )

    def _run_attention_probe(self, model_id: str, adapter: Adapter, caps: Capabilities) -> _RunResult:
        rng = random.Random()  # fresh per run — tokens/offsets vary every run
        prompt, expected = prompts.build_attention_probe(rng)
        return self._single_call(
            model_id, adapter, caps,
            ChatRequest(
                model_id=model_id,
                messages=[{"role": "user", "content": prompt}],
                thinking_budget=8192,
                max_output=16384,
                timeout_s=self._timeout_s,
            ),
            lambda text: score_attention_probe(text, expected),
        )

    def _run_attention_stress(self, model_id: str, adapter: Adapter, caps: Capabilities) -> _RunResult:
        """20-turn scripted conversation; checkpoints at turns 5/10/15/20.

        Sequential by design (the point is decay over the growing history).
        Each turn sends the accumulated messages and appends the extracted
        response text back — thinking stays out of the history.
        """
        rng = random.Random()  # fresh per run — the token varies every run
        token = stress_prompts.make_stress_token(rng)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": stress_prompts.build_stress_instruction(token)}
        ]
        first = self._chat_with_retry(
            model_id, adapter, caps,
            ChatRequest(
                model_id=model_id, messages=list(messages),
                thinking_budget=8192, max_output=16384, timeout_s=self._timeout_s,
            ),
        )
        messages.append({"role": "assistant", "content": self._stress_content(first)})
        latency_ms = first.latency_ms
        tokens_in = first.tokens_in
        tokens_out = first.tokens_out

        checkpoints: dict[str, str] = {}
        for i, prompt in enumerate(stress_prompts.STRESS_CANNED_TURNS, start=2):
            messages.append({"role": "user", "content": prompt})
            resp = self._chat_with_retry(
                model_id, adapter, caps,
                ChatRequest(
                    model_id=model_id, messages=list(messages),
                    thinking_budget=8192, max_output=16384,
                    timeout_s=self._timeout_s,
                ),
            )
            latency_ms += resp.latency_ms
            tokens_in += resp.tokens_in
            tokens_out += resp.tokens_out
            messages.append({"role": "assistant", "content": self._stress_content(resp)})
            if i in stress_prompts.STRESS_CHECKPOINT_TURNS:
                checkpoints[f"survive_t{i}"] = resp.text

        outcome = score_attention_stress(checkpoints, token)
        return _RunResult(
            outcome=outcome,
            response_text=checkpoints.get("survive_t20", ""),
            latency_ms=latency_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            requested_max_output=16384,
        )

    @staticmethod
    def _stress_content(resp: ModelResponse) -> list[dict[str, Any]]:
        """History block for one assistant turn (text only, thinking stripped)."""
        return [{"type": "text", "text": resp.text}] if resp.text else [{"type": "text", "text": ""}]

    def _run_vision(self, model_id: str, adapter: Adapter, caps: Capabilities) -> _RunResult:
        if not caps.supports_vision:
            return _RunResult(outcome=skip_vision_outcome())
        return self._single_call(
            model_id, adapter, caps,
            ChatRequest(
                model_id=model_id,
                messages=[{"role": "user", "content": prompts.VISION_PROMPT}],
                images=[{
                    "media_type": "image/png",
                    "data": prompts.build_test_image_png(),
                }],
                thinking_budget=None,
                max_output=16384,
                timeout_s=self._timeout_s,
            ),
            score_vision,
        )

    def _run_speed(self, model_id: str, adapter: Adapter, caps: Capabilities) -> _RunResult:
        cr = ChatRequest(
            model_id=model_id,
            messages=[{"role": "user", "content": prompts.SPEED_PROMPT}],
            thinking_budget=None,  # v1: disable_thinking=True
            max_output=16384,
            timeout_s=self._timeout_s,
        )
        resp = self._chat(model_id, adapter, caps, cr)
        return _RunResult(
            outcome=score_speed(resp.tokens_out, resp.latency_ms, resp.text),
            response_text=resp.text,
            latency_ms=resp.latency_ms,
            tokens_in=resp.tokens_in,
            tokens_out=resp.tokens_out,
            requested_max_output=cr.max_output,
        )

    def _run_long_horizon(self, model_id: str, adapter: Adapter, caps: Capabilities) -> _RunResult:
        return self._single_call(
            model_id, adapter, caps,
            ChatRequest(
                model_id=model_id,
                messages=[{"role": "user", "content": prompts.LONG_HORIZON_PROMPT}],
                thinking_budget=8192,
                max_output=16384,
                timeout_s=self._timeout_s,
            ),
            score_long_horizon,
            combine_thinking=True,
        )

    _RUNNERS: dict[
        BenchmarkCategory,
        Callable[["LivebenchEngine", str, Adapter, Capabilities], _RunResult],
    ] = {
        BenchmarkCategory.code_gen: _run_code_gen,
        BenchmarkCategory.reasoning: _run_reasoning,
        BenchmarkCategory.instruction_follow: _run_instruction_follow,
        BenchmarkCategory.tool_use: _run_tool_use,
        BenchmarkCategory.long_context: _run_long_context,
        BenchmarkCategory.attention_probe: _run_attention_probe,
        BenchmarkCategory.attention_stress: _run_attention_stress,
        BenchmarkCategory.vision: _run_vision,
        BenchmarkCategory.speed: _run_speed,
        BenchmarkCategory.long_horizon: _run_long_horizon,
    }

    # ------------------------------------------------------------------
    # Run one (model, battery)
    # ------------------------------------------------------------------
    def run_battery(self, model_id: str, battery: BenchmarkCategory) -> BenchOutcome:
        """Run one battery for one model; never raises for model/battery-level
        faults — failures come back as a scored-0 outcome (v1 parity)."""
        if battery not in self._RUNNERS:
            raise ValueError(f"unknown benchmark: {battery}")

        failed = _BenchmarkOutcome(score=0.0, passed=False, raw_output="")
        result = _RunResult(outcome=failed)
        adapter: Adapter | None = None
        caps: Capabilities | None = None
        try:
            adapter = self._adapter_factory(model_id)
            caps = adapter.probe_capabilities(model_id)
        except Exception as e:  # noqa: BLE001 — v1 parity: record, don't crash
            result = _RunResult(
                outcome=_BenchmarkOutcome(
                    score=0.0, passed=False, raw_output=f"ERROR: {e}"
                )
            )

        if result.outcome.raw_output == "":  # adapter resolution succeeded
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
                            score=0.0, passed=False, raw_output=f"ERROR: {e}"
                        )
                    )
                    if e.status_code in _TRANSIENT_STATUS and attempt < 2:
                        time.sleep(min(2 ** attempt, 8))
                        continue
                    break
                except Exception as e:  # noqa: BLE001 — v1 parity
                    result = _RunResult(
                        outcome=_BenchmarkOutcome(
                            score=0.0, passed=False, raw_output=f"ERROR: {e}"
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
            items=items,
            latency_ms=result.latency_ms,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            raw_output=outcome.raw_output,
            response_text=result.response_text,
            thinking_text=result.thinking_text,
            requested_max_output=result.requested_max_output,
        )

    # ------------------------------------------------------------------
    # Storage — sweep/run/measurement rows incl. battery linkage
    # ------------------------------------------------------------------
    def store(
        self,
        conn,
        sweep_id: str,
        model_id: str,
        battery: BenchmarkCategory,
        outcome: BenchOutcome,
    ) -> None:
        """Write one run + per-item measurements under ``sweep_id``.

        Provider/model rows are upserted first (bench runs must work without
        a prior ``hr discover``), then the sweep, then the run, then one
        measurement row per graded item with the ACTUAL requested_max_output.
        """
        from hr.stage0 import (
            _insert_measurement,
            _insert_run,
            _insert_sweep,
            _upsert_model,
            _upsert_provider,
        )

        provider = model_id.split("/", 1)[0] if "/" in model_id else model_id
        _upsert_provider(conn, provider, provider)
        _upsert_model(conn, model_id, provider, model_id)
        _insert_sweep(conn, sweep_id, SEAT_CODE, "livebench")

        run_id = f"run-{uuid.uuid4().hex}"
        battery_id = f"battery-{battery_code(battery)}"
        _insert_run(
            conn,
            run_id,
            sweep_id,
            model_id,
            battery_id,
            1,
            outcome.tokens_in + outcome.tokens_out,
            0.0,
            True,
        )
        for item in outcome.items:
            _insert_measurement(
                conn,
                f"meas-{uuid.uuid4().hex}",
                run_id,
                item.item_id,
                1,
                item.score,
                outcome.tokens_in,
                outcome.tokens_out,
                outcome.latency_ms,
                response_text=outcome.response_text or None,
                thinking_text=outcome.thinking_text or None,
                requested_max_output=outcome.requested_max_output,
            )


__all__ = [
    "BenchOutcome",
    "ItemResult",
    "LivebenchEngine",
    "SEAT_CODE",
    "make_sweep_id",
]