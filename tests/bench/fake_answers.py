from __future__ import annotations

import re

from hr.bench.truths import long_horizon_truths, reasoning_truths

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
