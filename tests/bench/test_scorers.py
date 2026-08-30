"""Scorer unit tests at v4 semantics (task 12).

Every scorer keeps v1 formulas/constants intact; these tests pin the v4
shapes: code_gen 13 tests (SIGALRM perf gate), reasoning 13 questions,
instruction_follow 16 constraints, long_context 3 needles + 3 decoys,
tool_use target 105.63 (100 with tool / 60 without / 20 close / 0 wrong),
vision count+colors+positions, speed tok/s tiers, long_horizon 4 components.
"""

from __future__ import annotations

import random  # noqa: F401 (re-export; consumed by sibling test modules)

import pytest  # noqa: F401 (re-export; consumed by sibling test modules)

from hr.bench.livebench import battery_item_labels  # noqa: F401 (re-export; consumed by sibling test modules)

from hr.bench.prompts import build_attention_probe  # noqa: F401 (re-export; consumed by sibling test modules)

from hr.bench.scorers import (
    _BenchmarkOutcome,  # noqa: F401 (re-export; consumed by sibling test modules)
    _safe_calculate,  # noqa: F401 (re-export; consumed by sibling test modules)
    score_attention_probe,  # noqa: F401 (re-export; consumed by sibling test modules)
    score_attention_stress,  # noqa: F401 (re-export; consumed by sibling test modules)
    score_code_gen,  # noqa: F401 (re-export; consumed by sibling test modules)
    score_instruction_follow,  # noqa: F401 (re-export; consumed by sibling test modules)
    score_long_context,  # noqa: F401 (re-export; consumed by sibling test modules)
    score_long_horizon,  # noqa: F401 (re-export; consumed by sibling test modules)
    score_reasoning,  # noqa: F401 (re-export; consumed by sibling test modules)
    score_speed,  # noqa: F401 (re-export; consumed by sibling test modules)
    score_tool_use_text,  # noqa: F401 (re-export; consumed by sibling test modules)
    score_vision,  # noqa: F401 (re-export; consumed by sibling test modules)
)

from hr.bench.stress_prompts import (
    STRESS_CANNED_TURNS,  # noqa: F401 (re-export; consumed by sibling test modules)
    STRESS_CHECKPOINT_TURNS,  # noqa: F401 (re-export; consumed by sibling test modules)
    build_stress_instruction,  # noqa: F401 (re-export; consumed by sibling test modules)
)

from hr.bench.truths import long_horizon_truths, reasoning_truths  # noqa: F401 (re-export; consumed by sibling test modules)

from hr.models import BenchmarkCategory  # noqa: F401 (re-export; consumed by sibling test modules)

_CORRECT_CODE = '''```python
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

_BUGGY_BURST_CODE = _CORRECT_CODE.replace(
    "    return dp[0][n - 1]", "    return 0"
)

_MISSING_FN_CODE = _CORRECT_CODE.replace(
    "def count_inversions(arr):", "def count_inversions(arr):\n    raise NotImplementedError"
)

_PERFECT_LINES = [
    "The tower stand beside the old church.",          # 7 w, starts The
    "Every hour the clock chimes above the town.",      # 8 w, hour x1, chimes-s
    "Tall windows show 1 distant mountain range.",      # 6 w, 1 digit, windows-s
    "Its spire rise high, catching the morning sun 2.",  # 9 w, 1 comma, 1 digit
    "Big bells tower above the noisy street.",          # 7 w, tower x1, bells-s
    "At dusk the lights come on in the park.",      # 9 w, lights-s
    "When winter come the square turn quiet and still.",  # 8 w
    "Now midnight bring calm to rest.",                # 6 w, midnight x1, min words
]

_PERFECT_JSON = '{"lines": [\n' + ",\n".join(
    f'  "{line}"' for line in _PERFECT_LINES
) + "\n]}"

_ATTENTION_EXPECTED: dict[str, str] = {
    "pos_head": "A1B2-C3D4",
    "pos_mid_early": "E5F6-A7B8",
    "pos_mid": "C9D0-E1F2",
    "pos_mid_late": "A3B4-C5D6",
    "pos_tail": "E7F8-A9B0",
    "assoc_literal": "Helsinki",
    "assoc_infer": "Helsinki",
    "decoy_resist": "4821",
    "__distractors__": "3170,9056,6284,7439",
}

_PERFECT_ATTENTION = (
    "1) A1B2-C3D4\n2) E5F6-A7B8\n3) C9D0-E1F2\n4) A3B4-C5D6\n5) E7F8-A9B0\n"
    "6) Helsinki\n7) Helsinki\n8) 4821"
)

_STRESS_TOKEN = "7F3A-9C2E"

_STRESS_LABELS = ("survive_t5", "survive_t10", "survive_t15", "survive_t20")

_STRESS_GOOD = (
    "[ROGER] Here is the update.\n"
    "- One\n- Two\n- Three\n" + _STRESS_TOKEN
)

_STRESS_BAD = "I cannot follow that.\n- One\n- Two\n" + _STRESS_TOKEN

def _stress_checkpoints(which: dict[str, str]) -> dict[str, str]:
    return {label: which.get(label, _STRESS_GOOD) for label in _STRESS_LABELS}

def _perfect_long_horizon_answer() -> str:
    t = long_horizon_truths()
    slack_bits = ", ".join(f"{k}={v}" for k, v in t["non_critical_slack"].items())
    return (
        f"CRITICAL_PATH: {'->'.join(t['critical_path'])}\n"
        f"DURATION: {t['duration']} days\n"
        f"SLACK: {slack_bits}\n"
        "ACTION: NONE\n"
    )
