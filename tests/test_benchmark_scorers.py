"""Offline unit tests for the v4 benchmark scorers (hr.bench.scorers).

Rehabilitated from the v3-era ``v1_scorers_LEGACY.py.bak`` (which imported
the retired ``hr.benchmark`` module and injected a machine-specific path into
``sys.path`` — removed: tests import the installed package). The v4 facts
pinned here, with full v1 strictness:

  - code_gen: 13 hidden tests — 8 sliding_window_median + 3 burst_balloons
    + 1 count_inversions small + 1 count_inversions SIGALRM performance gate.
  - reasoning: 13 runtime-computed truth questions.
  - instruction_follow: 16 independent constraints (c1..c16).
  - long_context: 3 needles (alpha/bravo/charlie) + 3 decoys inside the
    ~240K-char haystack; decoys trap is surfaced, never graded.
  - tool_use: ``score_tool_use_text`` (NOT the retired ``_score_tool_use``),
    target 105.63 — 100 with a tool call, 60 without, 20 close, 0 wrong.

Tests import the installed package (no sys.path hacking).
"""

from hr.bench.prompts import (
    DECOY_A,
    DECOY_B,
    DECOY_C,
    HAYSTACK_CHARS,
    NEEDLE_A,
    NEEDLE_B,
    NEEDLE_C,
    build_haystack,
)
from hr.bench.scorers import (
    score_code_gen,
    score_instruction_follow,
    score_long_context,
    score_reasoning,
    score_tool_use_text,
    score_vision,
)
from hr.bench.truths import reasoning_truths

import json


# ─────────────────────────────────────────────────────────────────────────────
# code_gen — 13 hidden tests (cases 0-7 median, 8-10 burst, 11 inversion,
# 12 inversion SIGALRM performance gate)
# ─────────────────────────────────────────────────────────────────────────────

_CORRECT_CODE = '''```python
def sliding_window_median(nums, k):
    out = []
    for i in range(len(nums) - k + 1):
        win = sorted(nums[i:i + k])
        mid = len(win) // 2
        if len(win) % 2:
            out.append(float(win[mid]))
        else:
            out.append((win[mid - 1] + win[mid]) / 2.0)
    return out

def burst_balloons(nums):
    nums = [1] + [int(x) for x in nums] + [1]
    n = len(nums)
    dp = [[0] * n for _ in range(n)]
    for length in range(2, n):
        for left in range(n - length):
            right = left + length
            for i in range(left + 1, right):
                cand = dp[left][i] + nums[left] * nums[i] * nums[right] + dp[i][right]
                if cand > dp[left][right]:
                    dp[left][right] = cand
    return dp[0][n - 1]

def count_inversions(arr):
    def merge_count(a):
        if len(a) <= 1:
            return a[:], 0
        mid = len(a) // 2
        left, il = merge_count(a[:mid])
        right, ir = merge_count(a[mid:])
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
    return merge_count(list(arr))[1]
```'''

# Same bundle but burst_balloons always 0 (cases 8-10 fail) and
# count_inversions raises (cases 11 + 12 perf gate fail) => 8/13.
_PARTIAL_CODE = _CORRECT_CODE.replace(
    "    return dp[0][n - 1]", "    return 0"
).replace(
    "    return merge_count(list(arr))[1]",
    "    raise NotImplementedError",
)


def test_code_gen_correct_100():
    r = score_code_gen(_CORRECT_CODE)
    # Case 12 is the SIGALRM performance gate — passing it is required to
    # reach 13/13, so this assert pins the perf gate by construction.
    assert r.score == 100.0
    assert r.passed is True
    assert r.raw_output == "13/13 tests passed"
    assert r.item_scores is not None and len(r.item_scores) == 13
    assert all(ok for _, ok in r.item_scores)


def test_code_gen_partial():
    r = score_code_gen(_PARTIAL_CODE)
    # median cases 0-7 pass; burst 8-10 and inversion 11 + 12 fail => 8/13 ≈ 61.5
    assert r.score == (8 / 13) * 100.0
    assert r.passed is False
    assert r.item_scores is not None and len(r.item_scores) == 13
    assert sum(1 for _, ok in r.item_scores if ok) == 8


def test_code_gen_garbage():
    r = score_code_gen("just prose with no python code")
    assert r.score == 0.0
    assert r.passed is False
    assert "missing required function" in r.raw_output


# ─────────────────────────────────────────────────────────────────────────────
# reasoning — 13 runtime-computed truth questions
# ─────────────────────────────────────────────────────────────────────────────

def test_reasoning_perfect():
    truths = reasoning_truths()
    assert len(truths) == 13
    text = "\n".join(f"A{i}: {truths[i]}" for i in range(1, 14))
    r = score_reasoning(text)
    assert r.score == 100.0
    assert r.passed is True
    assert r.item_scores is not None and len(r.item_scores) == 13
    assert all(ok for _, ok in r.item_scores)


def test_reasoning_half():
    truths = reasoning_truths()
    lines = [f"A{i}: {truths[i]}" for i in range(1, 8)]       # 7 correct
    lines += [f"A{i}: 0" for i in range(8, 14)]               # 6 wrong
    r = score_reasoning("\n".join(lines))
    # 7/13 ≈ 53.8
    assert r.score == (7 / 13) * 100.0
    assert r.passed is False
    assert r.item_scores is not None
    assert [lbl for lbl, ok in r.item_scores if not ok] == [
        "q8", "q9", "q10", "q11", "q12", "q13",
    ]


# ─────────────────────────────────────────────────────────────────────────────
# tool_use — final-text grading vs target 105.63 (score_tool_use_text)
# ─────────────────────────────────────────────────────────────────────────────

def test_tool_perfect():
    r = score_tool_use_text("TOTAL: 105.63", tool_used=True)
    assert r.score == 100.0
    assert r.passed is True


def test_tool_without_tool():
    # Same correct total but the loop never used a tool => 60 (not 100).
    r = score_tool_use_text("TOTAL: 105.63", tool_used=False)
    assert r.score == 60.0
    assert r.passed is False


def test_tool_close():
    # |107.20 - 105.63| = 1.57 <= 2.0 => the 20-point close band.
    r = score_tool_use_text("the total is 107.20", tool_used=True)
    assert r.score == 20.0
    assert r.passed is False


def test_tool_wrong():
    r = score_tool_use_text("FINAL: 42.00", tool_used=True)
    assert r.score == 0.0
    assert r.passed is False


# ─────────────────────────────────────────────────────────────────────────────
# instruction_follow — 16 independent constraints
# ─────────────────────────────────────────────────────────────────────────────

# 8 lines satisfying all 16 constraints: exactly 8 (c1), all end '.' (c2),
# starts in order the/every/tall/its/big/at/when/now (c3), 6-10 words each (c4),
# exactly 5 words end in 's' — trees/leaves/its/waits/sets (c5), no 'z' (c6),
# 4th has exactly one comma (c7), last is fewest words, 6 == min 6 (c8),
# exactly 2 digits total (c9), no word > 10 letters (c10), "hour" once in 2nd
# (c11), "tower" once in 5th (c12), 61 words total (c13), one digit in 3rd
# (c14), "midnight" once in last (c15), JSON only (c16).
_PERFECT_LINES = [
    "the bright star twin above tonight.",             # 6 w
    "every hour the moon above cold night.",           # 7 w, hour x1
    "tall green trees count 2 silver leaves.",         # 7 w, 1 digit
    "its proud gate swung, the old iron lock.",        # 8 w, 1 comma
    "big grey tower waits until the soft sun sets 2.",  # 10 w, tower x1, 1 digit
    "at dawn the pale light glow warm and gold.",      # 9 w
    "when night fall the cold wind blow below.",       # 8 w
    "now midnight low upon the hill.",                 # 6 w, midnight x1, fewest
]
_PERFECT_JSON = '{"lines": ' + json.dumps(_PERFECT_LINES) + '}'


def test_instruction_perfect():
    r = score_instruction_follow(_PERFECT_JSON)
    assert r.score == 100.0
    assert r.passed is True
    assert r.item_scores is not None and len(r.item_scores) == 16
    assert all(ok for _, ok in r.item_scores)


def test_instruction_non_json():
    r = score_instruction_follow("just prose about lighthouse")
    assert r.score == 0.0
    assert r.passed is False


def test_instruction_one_violation():
    # Drop the 5th line's digit => c9 (exactly 2 digits overall) fails.
    bad = _PERFECT_JSON.replace("sets 2.", "sets.")
    r = score_instruction_follow(bad)
    # 15/16 = 93.75
    assert r.score == 93.75
    assert r.passed is False
    assert r.item_scores is not None
    assert [lbl for lbl, ok in r.item_scores if not ok] == ["c9"]


# ─────────────────────────────────────────────────────────────────────────────
# long_context — 3 needles + 3 decoys, ~240K-char haystack
# ─────────────────────────────────────────────────────────────────────────────

def test_long_context_all_three():
    text = (
        "alpha: 4471-KILO-2210\n"
        "bravo: 9938-ECHO-6643\n"
        "charlie: 1057-TANGO-8830"
    )
    r = score_long_context(text)
    assert r.score == 100.0
    assert r.passed is True
    assert r.item_scores is not None and len(r.item_scores) == 3


def test_long_context_two():
    text = "alpha: 4471-KILO-2210\nbravo: 9938-ECHO-6643"
    r = score_long_context(text)
    assert r.score == (2 / 3) * 100.0
    assert r.passed is False


def test_long_context_one():
    text = "charlie: 1057-TANGO-8830"
    r = score_long_context(text)
    assert r.score == (1 / 3) * 100.0
    assert r.passed is False


def test_long_context_none():
    r = score_long_context("no codes here")
    assert r.score == 0.0
    assert r.passed is False


def test_long_context_decoy_trap_noted():
    # Decoy code 4472-KILO-2211 must NOT lower the score — only be surfaced.
    text = (
        "alpha: 4471-KILO-2210\nbravo: 9938-ECHO-6643\ncharlie: 1057-TANGO-8830\n"
        "decoy: 4472-KILO-2211"
    )
    r = score_long_context(text)
    assert r.score == 100.0
    assert r.passed is True
    assert "decoy_trapped" in r.raw_output


def test_haystack_contains_all_needles_and_decoys():
    h = build_haystack()
    assert len(h) >= HAYSTACK_CHARS          # HAYSTACK_CHARS = 240_000
    for fragment in (NEEDLE_A, NEEDLE_B, NEEDLE_C,
                     DECOY_A, DECOY_B, DECOY_C):
        assert fragment in h


# ─────────────────────────────────────────────────────────────────────────────
# vision — PNG with 4 colored squares (count 25 + colors 4x15 + positions 4x3.75)
# ─────────────────────────────────────────────────────────────────────────────

def test_vision_descriptive():
    text = (
        "There are four colored squares on white background. "
        "A red square in the top-left, a blue square in the top-right, "
        "a green square in the bottom-left, and a yellow square in the bottom-right."
    )
    r = score_vision(text)
    assert r.score == 100.0
    assert r.passed is True


def test_vision_vague():
    text = "Some shapes are visible."
    r = score_vision(text)
    assert r.score < 50.0
    assert r.passed is False