"""Shared fake adapter + canned perfect responses for task-12 bench tests.

NO network: every chat() call resolves deterministically from the request
(messages/tools/images/content), which is also what lets tests assert that
the engine routed through ChatRequest fields (thinking_budget, max_output,
images, tools) unchanged.
"""

from __future__ import annotations

import re
from typing import Any

from hr.adapters.base import Capabilities, ChatRequest
from hr.bench.prompts import (
    CODE_GEN_PROMPT,
    INSTRUCTION_PROMPT,
    LONG_CONTEXT_FOLLOW_UP,
    LONG_HORIZON_PROMPT,
    REASONING_PROMPT,
    SPEED_PROMPT,
    TOOL_TASK_PROMPT,
    VISION_PROMPT,
)
from hr.bench.truths import long_horizon_truths, reasoning_truths
from hr.graders.base import ModelResponse

# ---------------------------------------------------------------------------
# Canned perfect answers
# ---------------------------------------------------------------------------

CORRECT_CODE = '''```python
def sliding_window_median(nums, k):
    out = []
    for i in range(len(nums) - k + 1):
        win = sorted(nums[i : i + k])
        m = len(win) // 2
        if len(win) % 2:
            out.append(float(win[m]))
        else:
            out.append((win[m - 1] + win[m]) / 2.0)
    return out

def burst_balloons(nums):
    padded = [1] + [int(x) for x in nums] + [1]
    n = len(padded)
    dp = [[0] * n for _ in range(n)]
    for length in range(2, n):
        for left in range(n - length):
            right = left + length
            for i in range(left + 1, right):
                cand = (padded[left] * padded[i] * padded[right]
                        + dp[left][i] + dp[i][right])
                if cand > dp[left][right]:
                    dp[left][right] = cand
    return dp[0][n - 1]

def count_inversions(arr):
    def ms(a):
        if len(a) <= 1:
            return a[:], 0
        mid = len(a) // 2
        left, il = ms(a[:mid])
        right, ir = ms(a[mid:])
        merged = []
        inv = il + ir
        i = j = 0
        while i < len(left) and j < len(right):
            if left[i] <= right[j]:
                merged.append(left[i])
                i += 1
            else:
                merged.append(right[j])
                j += 1
                inv += len(left) - i
        merged.extend(left[i:])
        merged.extend(right[j:])
        return merged, inv
    return ms(list(arr))[1]
```
'''

PERFECT_LINES = [
    "The tower stand beside the old church.",
    "Every hour the clock chimes above the town.",
    "Tall windows show 1 distant mountain range.",
    "Its spire rise high, catching the morning sun 2.",
    "Big bells tower above the noisy street.",
    "At dusk the lights come on in the park.",
    "When winter come the square turn quiet and still.",
    "Now midnight bring calm to rest.",
]

PERFECT_INSTRUCTION_JSON = '{"lines": [\n' + ",\n".join(
    f'  "{line}"' for line in PERFECT_LINES
) + "\n]}"

PERFECT_NEEDLES = (
    "alpha: 4471-KILO-2210\nbravo: 9938-ECHO-6643\ncharlie: 1057-TANGO-8830"
)

PERFECT_VISION = (
    "There are four solid squares: red in top-left, blue in top-right, "
    "green in bottom-left, yellow in bottom-right."
)

PERFECT_LONG_HORIZON = _perfect = None  # built lazily below (truths are lazy)


def perfect_long_horizon_answer() -> str:
    t = long_horizon_truths()
    slack_bits = ", ".join(f"{k}={v}" for k, v in t["non_critical_slack"].items())
    return (
        f"CRITICAL_PATH: {'->'.join(t['critical_path'])}\n"
        f"DURATION: {t['duration']} days\n"
        f"SLACK: {slack_bits}\n"
        "ACTION: NONE\n"
    )


def perfect_reasoning_answer() -> str:
    t = reasoning_truths()
    return "\n".join(f"A{i}: {t[i]}" for i in range(1, 14))


# ---------------------------------------------------------------------------
# attention_probe: the fake answers as a perfect reader would — it extracts
# every planted needle + the follow-up question's target tag from the prompt
# itself (tokens are rng-generated, so nothing is hardcoded here).
# ---------------------------------------------------------------------------


def perfect_attention_probe_answer(prompt_text: str) -> str:
    """Reply to an attention-probe prompt with exactly the planted answers."""
    tokens = dict(
        re.findall(
            r"The recovery token for station (pos_\w+) is "
            r"([0-9A-F]{4}-[0-9A-F]{4})\.",
            prompt_text,
        )
    )
    live = re.search(r"([A-Z][a-z]+ [A-Z][a-z]+) lives in ([A-Za-z ]+)\s*\.", prompt_text)
    city = live.group(2).strip() if live else "??"
    follow_up = prompt_text.split("Answer each line exactly", 1)[1]
    tag_m = re.search(r"archive box labeled ([A-Z]{4})\?", follow_up)
    decoy = "????"
    if tag_m:
        boxes = dict(re.findall(r"labeled ([A-Z]{4}) has number (\d{4})", prompt_text))
        decoy = boxes.get(tag_m.group(1), "????")
    lines = [f"{i}) {tok}" for i, tok in enumerate(tokens.values(), 1)]
    lines.append("6) " + city)
    lines.append("7) " + city)
    lines.append("8) " + decoy)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# FakeAdapter
# ---------------------------------------------------------------------------

GARBAGE_TEXT = "kvetch zorch glorp utterly meaningless filler text."


class FakeAdapter:
    """Scripted adapter: perfect answers, or garbage for garbage_model_ids."""

    def __init__(self, garbage: bool = False) -> None:
        self.garbage = garbage
        self.requests: list[tuple[str, ChatRequest]] = []
        self.tool_loop_turns: dict[str, int] = {}

    def probe_capabilities(self, model_id: str) -> Capabilities:
        return Capabilities(
            model_id=model_id,
            provider="fake",
            supports_thinking=True,
            supports_vision=True,
        )

    # -- helpers to keep the chat() dispatcher small -----------------------
    def _stress_response(self, joined: str) -> str:
        """A compliant stress-turn reply: all 5 constraints, token extracted
        from the instruction (it is rng-generated, never hardcoded)."""
        token_m = re.search(r"exact token ([0-9A-F]{4}-[0-9A-F]{4})", joined)
        token = token_m.group(1) if token_m else "DEAD-BEEF"
        return (
            "[ROGER] Here is my answer.\n"
            "- First point.\n"
            "- Second point.\n"
            "- Third point.\n" + token
        )

    def _perfect_for(self, cr: ChatRequest) -> str:
        joined = " ".join(
            m.get("content") if isinstance(m.get("content"), str) else ""
            for m in cr.messages
        )
        if cr.tools:
            return ""  # tool_use turn 1 handled by the loop below
        if cr.images:
            return PERFECT_VISION
        tail = joined[-120:]
        if "Write these three Python functions" in joined:
            return CORRECT_CODE
        if "How many integers n with 1 <= n" in joined:
            return perfect_reasoning_answer()
        if "clock tower" in joined:
            return PERFECT_INSTRUCTION_JSON
        if "obey these five rules" in joined:
            return self._stress_response(joined)
        if "RECOVERY codes" in joined:
            return PERFECT_NEEDLES
        if "Answer each line exactly" in joined:
            return perfect_attention_probe_answer(joined)
        if "project with 6 tasks" in joined:
            return perfect_long_horizon_answer()
        if "Say hello in 10 different languages" in joined:
            return "hello\nbonjour\nhallo\nciao\nhola\nkonnichiwa\n"
            "namaste\nmerhaba\nxin chao\nsalam"
        if "calculate" in joined:
            return "TOTAL: 105.63"
        return GARBAGE_TEXT

    def chat(
        self,
        model_id: str,
        messages: list[dict],
        *,
        images: list[dict] | None = None,
        tools: list[dict] | None = None,
        thinking_budget: int | None = None,
        max_output: int = 16384,
        timeout_s: int = 600,
    ) -> ModelResponse:
        cr = ChatRequest(
            model_id=model_id,
            messages=list(messages),
            images=images,
            tools=tools,
            thinking_budget=thinking_budget,
            max_output=max_output,
            timeout_s=timeout_s,
        )
        self.requests.append((model_id, cr))

        if self.garbage:
            return ModelResponse(
                text=GARBAGE_TEXT, latency_ms=111, tokens_in=10, tokens_out=10
            )

        # Multi-turn tool_use: turn 1 issues a calculate call; turn 2
        # (history contains tool_result) answers with the total.
        if tools:
            key = model_id
            turn = self.tool_loop_turns.get(key, 0)
            self.tool_loop_turns[key] = turn + 1
            has_tool_result = any(
                isinstance(m.get("content"), list)
                and any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in m["content"]
                )
                for m in messages
            )
            if not has_tool_result:
                return ModelResponse(
                    text="",
                    tool_calls=[
                        {
                            "id": "toolu_1",
                            "name": "calculate",
                            "input": {"expression": "(3*17.50)+(2*24.99)"},
                        }
                    ],
                    latency_ms=500,
                    tokens_in=100,
                    tokens_out=50,
                )
            return ModelResponse(
                text="TOTAL: 105.63",
                latency_ms=500,
                tokens_in=100,
                tokens_out=400,
            )

        text = self._perfect_for(cr)
        return ModelResponse(
            text=text,
            thinking="working notes" if thinking_budget else "",
            latency_ms=2000,
            tokens_in=240,
            tokens_out=2000 if "Say hello" in text else 512,
        )


class NoVisionAdapter(FakeAdapter):
    def probe_capabilities(self, model_id: str) -> Capabilities:
        cap = super().probe_capabilities(model_id)
        return Capabilities(
            model_id=cap.model_id,
            provider=cap.provider,
            supports_thinking=cap.supports_thinking,
            supports_vision=False,
        )


class ToolsRejectedAdapter(FakeAdapter):
    """Rejects the tools field on the FIRST turn, then answers with text."""

    def chat(self, *args: Any, **kwargs: Any) -> ModelResponse:
        tools = kwargs.get("tools")
        if tools:
            from hr.adapters.base import AdapterError

            raise AdapterError("tools parameter not supported by this endpoint")
        kwargs["tools"] = None
        messages = list(args[1])
        final = "TOTAL: 105.63"
        for m in messages:
            if isinstance(m.get("content"), str) and "calculate" in m["content"]:
                final = "The total is $105.63."
        return ModelResponse(
            text=final, latency_ms=300, tokens_in=50, tokens_out=80
        )


class ForgetfulStressAdapter(FakeAdapter):
    """Plays the stress conversation compliantly, then drops the end token
    after ``drop_after`` turns -> survive_t15/t20 checkpoints fail."""

    def __init__(self, drop_after: int = 11) -> None:
        super().__init__()
        self.turn = 0
        self.drop_after = drop_after

    def _stress_response(self, joined: str) -> str:
        self.turn += 1
        resp = super()._stress_response(joined)
        if self.turn > self.drop_after:
            resp = resp.rsplit("\n", 1)[0]  # token line dropped -> end_token fails
        return resp


class FlakyStressAdapter(FakeAdapter):
    """Raises one transient AdapterError mid-conversation, then behaves as the
    compliant adapter — the engine must retry the SAME turn, not the battery."""

    def __init__(self, fail_turn: int = 17) -> None:
        super().__init__()
        self.fail_turn = fail_turn
        self.turn = 0
        self.failed = False
        self.failed_messages: list[dict] | None = None

    def chat(self, *args: Any, **kwargs: Any) -> ModelResponse:
        self.turn += 1
        if self.turn == self.fail_turn and not self.failed:
            self.failed = True
            self.failed_messages = [dict(m) for m in args[1]]
            from hr.adapters.base import AdapterError

            raise AdapterError("upstream 503", status_code=503)
        return super().chat(*args, **kwargs)