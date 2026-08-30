from __future__ import annotations

import random
from typing import Any, Callable, Protocol

from hr.adapters.base import Adapter, AdapterError, Capabilities, ChatRequest
from hr.bench import prompts, stress_prompts
from hr.bench.engine_results import _RunResult
from hr.bench.scorers import _safe_calculate, score_attention_stress, score_tool_use_text
from hr.graders.base import ModelResponse
from hr.models import BenchmarkCategory

_CALCULATE_TOOL: dict[str, Any] = {
    "name": "calculate",
    "description": "Evaluate a pure arithmetic expression (numbers, +-*/, ()).",
    "input_schema": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]},
}

class EngineInteractiveMixin(Protocol):
    _timeout_s: int
    _chat: Callable[..., ModelResponse]
    _chat_with_retry: Callable[..., ModelResponse]
    _rng_for: Callable[[str, BenchmarkCategory], random.Random]

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

    def _run_attention_stress(self, model_id: str, adapter: Adapter, caps: Capabilities) -> _RunResult:
        """20-turn scripted conversation; checkpoints at turns 5/10/15/20.

        Sequential by design (the point is decay over the growing history).
        Each turn sends the accumulated messages and appends the extracted
        response text back — thinking stays out of the history.
        """
        rng = self._rng_for(model_id, BenchmarkCategory.attention_stress)
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



__all__ = ["EngineInteractiveMixin"]
