from __future__ import annotations

import random
from typing import Callable, Protocol

from hr.adapters.base import Adapter, Capabilities, ChatRequest
from hr.bench import prompts
from hr.bench.engine_results import _RunResult
from hr.bench.scorers import score_attention_probe, score_code_gen, score_instruction_follow, score_long_context, score_long_horizon, score_reasoning, score_speed, score_vision, skip_vision_outcome
from hr.graders.base import ModelResponse
from hr.models import BenchmarkCategory

class EngineRunnersMixin(Protocol):
    _timeout_s: int
    _single_call: Callable[..., _RunResult]
    _chat: Callable[..., ModelResponse]
    _rng_for: Callable[[str, BenchmarkCategory], random.Random]

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
