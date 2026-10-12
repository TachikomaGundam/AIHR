from __future__ import annotations

import random
from typing import Callable, Protocol

from hr.adapters.base import Adapter, Capabilities, ChatRequest
from hr.bench import prompts
from hr.bench.engine_results import _RunResult
from hr.bench.scorer_shared import ItemDetail, _BenchmarkOutcome
from hr.bench.scorers import code_gen_task_slice, score_attention_probe, score_code_gen_task, score_instruction_follow, score_long_context, score_long_horizon, score_reasoning_single, score_speed, score_vision, skip_vision_outcome
from hr.bench.truths import reasoning_truths
from hr.graders.base import ModelResponse
from hr.models import BenchmarkCategory
from hr.sandbox import sandbox_available

class EngineRunnersMixin(Protocol):
    _timeout_s: int
    _single_call: Callable[..., _RunResult]
    _chat: Callable[..., ModelResponse]
    _rng_for: Callable[[str, BenchmarkCategory], random.Random]

    def _run_code_gen(self, model_id: str, adapter: Adapter, caps: Capabilities) -> _RunResult:
        reason = sandbox_available()
        if reason is not None:
            from hr.bench.scorer_runtime import skip_sandbox_outcome
            return _RunResult(outcome=skip_sandbox_outcome(reason))
        # 0.6.5 (ruling C): one function per request. The mega prompt made the
        # three functions share one output fence, so a single truncation or a
        # runaway answer on one function zeroed all 13 hidden tests. Each
        # function now grades on its own; a bad call only costs its slice.
        scores: list[tuple[str, bool]] = []
        details: list[ItemDetail] = []
        total_in = total_out = total_lat = 0
        answered = False
        for name, spec in prompts.CODE_GEN_TASK_SPECS:
            first, count = code_gen_task_slice(name)
            result = self._single_call(
                model_id, adapter, caps,
                ChatRequest(
                    model_id=model_id,
                    messages=[
                        {"role": "user", "content": prompts.code_gen_item_prompt(spec)}
                    ],
                    thinking_budget=None,  # v1: disable_thinking=True
                    max_output=16384,
                    timeout_s=self._timeout_s,
                ),
                lambda text, _n=name: score_code_gen_task(text, _n),
            )
            outcome = result.outcome
            if outcome.status == "scored":
                answered = True
            slice_scores = outcome.item_scores or [
                (f"test.{first + i:02d}", False) for i in range(count)
            ]
            for label, passed in slice_scores:
                scores.append((label, passed))
                details.append(
                    ItemDetail(
                        label=label,
                        response_text=result.response_text,
                        thinking_text=result.thinking_text,
                        tokens_in=result.tokens_in,
                        tokens_out=result.tokens_out,
                        latency_ms=result.latency_ms,
                        requested_max_output=result.requested_max_output,
                        finish_reason=result.finish_reason,
                    )
                )
            total_in += result.tokens_in
            total_out += result.tokens_out
            total_lat += result.latency_ms
        n_tests = 13
        if not answered:
            return _RunResult(
                outcome=_BenchmarkOutcome(
                    score=0.0, passed=False, status="inconclusive",
                    raw_output=f"INCONCLUSIVE: all {len(prompts.CODE_GEN_TASK_SPECS)} "
                    "code calls generated tokens without a gradeable answer",
                )
            )
        correct = sum(1 for _label, passed in scores if passed)
        return _RunResult(
            outcome=_BenchmarkOutcome(
                score=correct / n_tests * 100.0,
                passed=correct == n_tests,
                raw_output=f"{correct}/{n_tests} tests passed",
                item_scores=scores,
                item_details=details,
            ),
            latency_ms=total_lat,
            tokens_in=total_in,
            tokens_out=total_out,
        )

    def _run_factuality(self, model_id: str, adapter: Adapter, caps: Capabilities) -> _RunResult:
        """B2: 8 cards x 2 single-turn calls (answerable key-fact + trap)."""
        from hr.bench.prompts import (
            FACT_ANSWER_HEADER,
            FACT_TRAP_HEADER,
            FACTUALITY_CARDS,
        )
        from hr.bench.scorers import score_factuality

        results: list[tuple[str, str]] = []
        text_log: list[str] = []
        tokens_in = tokens_out = latency = 0
        for card in FACTUALITY_CARDS:
            texts: list[str] = []
            for tmpl in (FACT_ANSWER_HEADER, FACT_TRAP_HEADER):
                prompt = tmpl.format(s=card["sheet"], q=card["q_answer"]
                                     if tmpl is FACT_ANSWER_HEADER else card["q_trap"])
                resp = self._chat(model_id, adapter, caps, ChatRequest(
                    model_id=model_id,
                    messages=[{"role": "user", "content": prompt}],
                    thinking_budget=None,
                    max_output=512,
                    timeout_s=self._timeout_s,
                ))
                tokens_in += resp.tokens_in
                tokens_out += resp.tokens_out
                latency += resp.latency_ms
                texts.append(resp.text)
            results.append((texts[0], texts[1]))
            # #12: persist BOTH sides per card — a blind auditor must be able
            # to re-derive key_hit and fabrication from the DB alone; the
            # round-7 sweep stored only the trap side, leaving every verdict
            # dependent on the scorer's in-process self-report.
            text_log.append(f"[ans] {texts[0][:200]}\n[trap] {texts[1][:120]}")
        outcome = score_factuality(results)
        return _RunResult(
            outcome=outcome,
            response_text="\n".join(text_log),
            latency_ms=latency,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            requested_max_output=512,
        )

    def _run_reasoning(self, model_id: str, adapter: Adapter, caps: Capabilities) -> _RunResult:
        # 0.6.5 (owner ruling 2026-10-12): one request per question. The 13
        # questions used to ride one shared output budget — one truncation
        # zeroed the whole battery and made cross-model scores meaningless
        # (191 round 10: cloud flash 0/13 with content NULL, score meant
        # "ran out of budget", not "cannot solve").
        truths = reasoning_truths()
        if len(truths) != len(prompts.REASONING_QUESTIONS):
            raise ValueError(
                f"reasoning registry drift: {len(prompts.REASONING_QUESTIONS)} "
                f"questions vs {len(truths)} truths"
            )
        scores: list[tuple[str, bool]] = []
        details: list[ItemDetail] = []
        verdicts: list[str] = []
        correct = 0
        empty_finals = 0
        total_in = total_out = total_lat = 0
        for q_idx, question in enumerate(prompts.REASONING_QUESTIONS, start=1):
            truth = truths[q_idx]
            result = self._single_call(
                model_id, adapter, caps,
                ChatRequest(
                    model_id=model_id,
                    messages=[
                        {"role": "user", "content": prompts.reasoning_item_prompt(question)}
                    ],
                    thinking_budget=4096,
                    max_output=16384,
                    timeout_s=self._timeout_s,
                ),
                lambda text, _t=truth: score_reasoning_single(text, _t),
                combine_thinking=True,
            )
            outcome = result.outcome
            if outcome.status != "scored":
                empty_finals += 1
            passed = outcome.status == "scored" and outcome.passed
            correct += 1 if passed else 0
            scores.append((f"q{q_idx}", passed))
            verdicts.append(f"Q{q_idx}:{'✓' if passed else '✗'}")
            details.append(
                ItemDetail(
                    label=f"q{q_idx}",
                    response_text=result.response_text,
                    thinking_text=result.thinking_text,
                    tokens_in=result.tokens_in,
                    tokens_out=result.tokens_out,
                    latency_ms=result.latency_ms,
                    requested_max_output=result.requested_max_output,
                    finish_reason=result.finish_reason,
                )
            )
            total_in += result.tokens_in
            total_out += result.tokens_out
            total_lat += result.latency_ms
        n = len(prompts.REASONING_QUESTIONS)
        if correct == 0 and empty_finals == n:
            return _RunResult(
                outcome=_BenchmarkOutcome(
                    score=0.0, passed=False, status="inconclusive",
                    raw_output=f"INCONCLUSIVE: {n}/{n} calls generated tokens "
                    "without ever emitting a final answer",
                )
            )
        return _RunResult(
            outcome=_BenchmarkOutcome(
                score=correct / n * 100.0,
                passed=correct == n,
                raw_output=f"{correct}/{n} correct; {','.join(verdicts)}",
                item_scores=scores,
                item_details=details,
            ),
            latency_ms=total_lat,
            tokens_in=total_in,
            tokens_out=total_out,
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
        rng = self._rng_for(model_id, BenchmarkCategory.attention_probe)
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



__all__ = ["EngineRunnersMixin"]
