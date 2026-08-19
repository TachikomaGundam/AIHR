"""Scorer unit tests at v4 semantics (task 12).

Every scorer keeps v1 formulas/constants intact; these tests pin the v4
shapes: code_gen 13 tests (SIGALRM perf gate), reasoning 13 questions,
instruction_follow 16 constraints, long_context 3 needles + 3 decoys,
tool_use target 105.63 (100 with tool / 60 without / 20 close / 0 wrong),
vision count+colors+positions, speed tok/s tiers, long_horizon 4 components.
"""

from __future__ import annotations

import random

import pytest

from hr.bench.livebench import battery_item_labels
from hr.bench.prompts import build_attention_probe
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
)
from hr.bench.stress_prompts import (
    STRESS_CANNED_TURNS,
    STRESS_CHECKPOINT_TURNS,
    build_stress_instruction,
)
from hr.bench.truths import long_horizon_truths, reasoning_truths
from hr.models import BenchmarkCategory


# ---------------------------------------------------------------------------
# code_gen — 13 hidden tests (8 median + 3 burst + 1 inversion + 1 perf gate)
# ---------------------------------------------------------------------------

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

# Same code but burst_balloons always returns 0 -> cases 8,9,10 fail.
_BUGGY_BURST_CODE = _CORRECT_CODE.replace(
    "    return dp[0][n - 1]", "    return 0"
)

_MISSING_FN_CODE = _CORRECT_CODE.replace(
    "def count_inversions(arr):", "def count_inversions(arr):\n    raise NotImplementedError"
)  # still defines the name -> not "missing required fn" -> all-perf fail


def test_code_gen_perfect_solution_scores_100() -> None:
    outcome = score_code_gen(_CORRECT_CODE)
    assert outcome.score == pytest.approx(100.0)
    assert outcome.passed is True
    assert len(outcome.item_scores or []) == 13
    assert all(passed for _, passed in outcome.item_scores or [])


def test_code_gen_buggy_burst_fails_exactly_three_tests() -> None:
    outcome = score_code_gen(_BUGGY_BURST_CODE)
    assert outcome.score == pytest.approx(10 / 13 * 100.0)
    assert outcome.passed is False
    item_scores = outcome.item_scores
    assert item_scores is not None and len(item_scores) == 13
    failed = [label for label, ok in item_scores if not ok]
    assert failed == ["test.08", "test.09", "test.10"]


def test_code_gen_missing_required_function_scores_zero() -> None:
    text = "```python\ndef sliding_window_median(nums, k):\n    return []\n```"
    outcome = score_code_gen(text)
    assert outcome.score == 0.0
    assert outcome.passed is False
    assert "missing required function" in outcome.raw_output


def test_code_gen_garbage_text_scores_zero() -> None:
    outcome = score_code_gen("this is not code at all")
    assert outcome.score == 0.0
    assert outcome.passed is False


# ---------------------------------------------------------------------------
# reasoning — 13 runtime-computed truth questions
# ---------------------------------------------------------------------------


def test_reasoning_structured_answers_score_100() -> None:
    truths = reasoning_truths()
    assert len(truths) == 13
    answer = "\n".join(f"A{i}: {truths[i]}" for i in range(1, 14))
    outcome = score_reasoning(answer)
    assert outcome.score == pytest.approx(100.0)
    assert outcome.passed is True
    assert len(outcome.item_scores or []) == 13


def test_reasoning_prose_with_boxed_answers_scores_100() -> None:
    truths = reasoning_truths()
    lines = []
    for i in range(1, 14):
        lines.append(f"Q{i}. Working through it carefully... \\boxed{{{truths[i]}}}")
    outcome = score_reasoning("\n".join(lines))
    assert outcome.score == pytest.approx(100.0)
    assert outcome.passed is True


def test_reasoning_partial_answers_score_fraction() -> None:
    truths = reasoning_truths()
    answer = "\n".join(f"A{i}: {truths[i]}" for i in range(1, 10))  # 9 correct
    answer += "\nA10: 1\nA11: 2\nA12: 3\nA13: 4"  # 4 wrong
    outcome = score_reasoning(answer)
    assert outcome.score == pytest.approx(9 / 13 * 100.0)
    assert outcome.passed is False
    item_scores = outcome.item_scores
    assert item_scores is not None
    failed = [label for label, ok in item_scores if not ok]
    assert failed == ["q10", "q11", "q12", "q13"]


def test_reasoning_no_candidates_scores_zero() -> None:
    outcome = score_reasoning("I refuse to elaborate further.")
    assert outcome.score == 0.0
    assert outcome.passed is False


# ---------------------------------------------------------------------------
# instruction_follow — 16 constraints
# ---------------------------------------------------------------------------

# Perfect 8-line clock tower satisfying all 16 constraints (see test for the
# constraint math: 5 words ending in 's', exactly 2 digits, s8 fewest words).
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


def test_instruction_follow_perfect_json_scores_100() -> None:
    outcome = score_instruction_follow(_PERFECT_JSON)
    assert outcome.score == pytest.approx(100.0)
    assert outcome.passed is True
    assert len(outcome.item_scores or []) == 16
    assert all(passed for _, passed in outcome.item_scores or [])


def test_instruction_follow_single_violation_scores_15_16() -> None:
    # "chime" (no final s) -> constraint c5 (exactly 5 words ending in 's')
    # fails; everything else stays satisfied.
    broken = _PERFECT_JSON.replace("the clock chimes above", "the clock chime above")
    outcome = score_instruction_follow(broken)
    assert outcome.score == pytest.approx(15 / 16 * 100.0)
    assert outcome.passed is False
    item_scores = outcome.item_scores
    assert item_scores is not None
    failed = [label for label, ok in item_scores if not ok]
    assert failed == ["c5"]


def test_instruction_follow_no_json_scores_zero() -> None:
    outcome = score_instruction_follow("here is prose, not json")
    assert outcome.score == 0.0
    assert outcome.passed is False


def test_instruction_follow_fenced_json_still_scores() -> None:
    fenced = "```json\n" + _PERFECT_JSON + "\n```"
    outcome = score_instruction_follow(fenced)
    # c16 (JSON is the ONLY output) fails inside fences... check semantics:
    # fences are tolerated for extraction but c16 requires zero fences.
    assert outcome.score == pytest.approx(15 / 16 * 100.0)
    assert not outcome.passed


# ---------------------------------------------------------------------------
# long_context — 3 needles + 3 decoys, 3 graded needle items
# ---------------------------------------------------------------------------


def test_long_context_all_needles_score_100() -> None:
    text = (
        "alpha: 4471-KILO-2210\n"
        "bravo: 9938-ECHO-6643\n"
        "charlie: 1057-TANGO-8830"
    )
    outcome = score_long_context(text)
    assert outcome.score == pytest.approx(100.0)
    assert outcome.passed is True
    assert "clean" in outcome.raw_output
    assert len(outcome.item_scores or []) == 3


def test_long_context_missing_needle_scores_fraction() -> None:
    text = "alpha: 4471-KILO-2210\nbravo: 9938-ECHO-6643\nno charlie here"
    outcome = score_long_context(text)
    assert outcome.score == pytest.approx(2 / 3 * 100.0)
    assert outcome.passed is False
    assert [lbl for lbl, ok in (outcome.item_scores or []) if not ok] == ["charlie"]


def test_long_context_decoy_trap_noted_but_needles_win() -> None:
    text = (
        "alpha: 4471-KILO-2210\nbravo: 9938-ECHO-6643\ncharlie: 1057-TANGO-8830\n"
        "also saw 4472-KILO-2211 somewhere"
    )
    outcome = score_long_context(text)
    assert outcome.score == pytest.approx(100.0)
    assert "decoy_trapped" in outcome.raw_output


# ---------------------------------------------------------------------------
# attention_probe — 8 binary probes (position sweep / assoc pair / distractor)
# ---------------------------------------------------------------------------

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


def test_attention_probe_perfect_answer_scores_100() -> None:
    outcome = score_attention_probe(_PERFECT_ATTENTION, _ATTENTION_EXPECTED)
    assert outcome.score == pytest.approx(100.0)
    assert outcome.passed is True
    assert "clean" in outcome.raw_output
    item_scores = outcome.item_scores
    assert item_scores is not None and len(item_scores) == 8
    assert [lbl for lbl, _ in item_scores] == [
        "pos_head", "pos_mid_early", "pos_mid", "pos_mid_late", "pos_tail",
        "assoc_literal", "assoc_infer", "decoy_resist",
    ]
    assert all(ok for _, ok in item_scores)


def test_attention_probe_missing_token_scores_fraction() -> None:
    text = _PERFECT_ATTENTION.replace("5) E7F8-A9B0\n", "")  # pos_tail dropped
    outcome = score_attention_probe(text, _ATTENTION_EXPECTED)
    assert outcome.score == pytest.approx(7 / 8 * 100.0)
    assert outcome.passed is False
    assert [lbl for lbl, ok in (outcome.item_scores or []) if not ok] == ["pos_tail"]
    assert "pos_tail" in outcome.raw_output


def test_attention_probe_case_insensitive_substring_match() -> None:
    text = _PERFECT_ATTENTION.lower().replace(")", ") ...")
    outcome = score_attention_probe(text, _ATTENTION_EXPECTED)
    assert outcome.score == pytest.approx(100.0)
    assert outcome.passed is True


def test_attention_probe_distractor_confusion_noted_but_items_pass() -> None:
    text = _PERFECT_ATTENTION + "\n(also that box 3170 looked like the answer)"
    outcome = score_attention_probe(text, _ATTENTION_EXPECTED)
    assert outcome.score == pytest.approx(100.0)
    assert outcome.passed is True
    assert "distractor_confused" in outcome.raw_output


def test_attention_probe_distractor_echoed_as_answer_fails_item() -> None:
    text = _PERFECT_ATTENTION.replace("8) 4821", "8) 3170")  # distractor chosen
    outcome = score_attention_probe(text, _ATTENTION_EXPECTED)
    assert outcome.score == pytest.approx(7 / 8 * 100.0)
    assert outcome.passed is False
    assert [lbl for lbl, ok in (outcome.item_scores or []) if not ok] == ["decoy_resist"]
    assert "distractor_confused" in outcome.raw_output


def test_attention_probe_full_miss_scores_zero() -> None:
    outcome = score_attention_probe("I cannot answer this", _ATTENTION_EXPECTED)
    assert outcome.score == 0.0
    assert outcome.passed is False
    assert len(outcome.item_scores or []) == 8
    assert all(not ok for _, ok in outcome.item_scores or [])


def test_attention_probe_builder_planted_needles_and_keys() -> None:
    prompt, expected = build_attention_probe(random.Random(20260819))
    labels = battery_item_labels(BenchmarkCategory.attention_probe)
    assert set(expected) == set(labels) | {"__distractors__"}

    for label in ("pos_head", "pos_mid_early", "pos_mid", "pos_mid_late", "pos_tail"):
        assert f"station {label} is {expected[label]}" in prompt
    city = expected["assoc_literal"]
    assert f"lives in {city}." in prompt
    assert f"number {expected['decoy_resist']}." in prompt
    assert "Answer each line exactly" in prompt
    distractors = expected["__distractors__"].split(",")
    assert len(distractors) == 4
    assert expected["decoy_resist"] not in distractors
    for d in distractors:
        assert f"has number {d}." in prompt
    # a perfect reader scores 100 against its own builder output
    perfect = "\n".join(
        f"{i}) {expected[label]}" for i, label in enumerate(labels, 1)
    )
    assert score_attention_probe(perfect, expected).score == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# attention_stress — 5-constraint survival at 4 checkpoints of a 20-turn script
# ---------------------------------------------------------------------------

_STRESS_TOKEN = "7F3A-9C2E"

_STRESS_LABELS = ("survive_t5", "survive_t10", "survive_t15", "survive_t20")

_STRESS_GOOD = (
    "[ROGER] Here is the update.\n"
    "- One\n- Two\n- Three\n" + _STRESS_TOKEN
)

_STRESS_BAD = "I cannot follow that.\n- One\n- Two\n" + _STRESS_TOKEN


def _stress_checkpoints(which: dict[str, str]) -> dict[str, str]:
    return {label: which.get(label, _STRESS_GOOD) for label in _STRESS_LABELS}


def test_attention_stress_all_pass_scores_100() -> None:
    outcome = score_attention_stress(
        {l: _STRESS_GOOD for l in _STRESS_LABELS}, _STRESS_TOKEN
    )
    assert outcome.score == pytest.approx(100.0)
    assert outcome.passed is True
    assert "clean" in outcome.raw_output
    item_scores = outcome.item_scores
    assert item_scores is not None and len(item_scores) == 4
    assert [lbl for lbl, _ in item_scores] == list(_STRESS_LABELS)
    assert all(ok for _, ok in item_scores)


def test_attention_stress_trailing_newline_token_still_matches() -> None:
    text = _STRESS_GOOD + "\n\n"
    outcome = score_attention_stress(
        {l: text for l in _STRESS_LABELS}, _STRESS_TOKEN
    )
    assert outcome.score == pytest.approx(100.0)


@pytest.mark.parametrize(
    ("broken", "broken_id"),
    [
        ("[ROGERX] was here.\n- One\n- Two\n- Three\n" + _STRESS_TOKEN, "start_tag"),
        (_STRESS_GOOD.replace(_STRESS_TOKEN, "wrong-9C2E"), "end_token"),
        (_STRESS_GOOD.replace("the update", "the important update"), "neg_word"),
        (_STRESS_GOOD.replace("\n- Three\n", "\n"), "bullet_lines"),
        ("[ROGER] " + "word " * 60 + "\n- One\n- Two\n- Three\n" + _STRESS_TOKEN, "word_count"),
    ],
)
def test_attention_stress_single_constraint_failure_detected(
    broken: str, broken_id: str
) -> None:
    outcome = score_attention_stress(
        _stress_checkpoints({"survive_t5": broken}), _STRESS_TOKEN
    )
    assert outcome.score == pytest.approx(75.0)
    assert outcome.passed is False
    assert [lbl for lbl, ok in (outcome.item_scores or []) if not ok] == ["survive_t5"]
    assert f"survive_t5: {broken_id}" in outcome.raw_output


def test_attention_stress_earliest_failing_checkpoint_noted() -> None:
    responses = _stress_checkpoints({
        "survive_t5": _STRESS_GOOD.replace("the update", "the important update"),
        "survive_t10": "[ROGER] " + "word " * 60 + "\n- One\n- Two\n- Three\n" + _STRESS_TOKEN,
    })
    outcome = score_attention_stress(responses, _STRESS_TOKEN)
    assert outcome.score == pytest.approx(50.0)
    assert outcome.passed is False
    assert "survive_t5: neg_word" in outcome.raw_output
    assert "survive_t10" not in outcome.raw_output


def test_attention_stress_full_miss_scores_zero() -> None:
    outcome = score_attention_stress(
        {l: _STRESS_BAD for l in _STRESS_LABELS}, _STRESS_TOKEN
    )
    assert outcome.score == 0.0
    assert outcome.passed is False
    assert len(outcome.item_scores or []) == 4
    assert all(not ok for _, ok in outcome.item_scores or [])


def test_attention_stress_builder_is_deterministic_script() -> None:
    instruction = build_stress_instruction(_STRESS_TOKEN)
    assert _STRESS_TOKEN in instruction
    assert "[ROGER]" in instruction
    assert len(STRESS_CANNED_TURNS) == 19
    assert max(STRESS_CHECKPOINT_TURNS) == len(STRESS_CANNED_TURNS) + 1 == 20
    assert STRESS_CHECKPOINT_TURNS == (5, 10, 15, 20)
    assert build_stress_instruction(_STRESS_TOKEN) == instruction
    # every canned turn is checkable by the scorer's constraints (never
    # contains the banned word itself — the bait turns invite it without
    # writing it)
    assert all("important" not in turn.lower() for turn in STRESS_CANNED_TURNS)


# ---------------------------------------------------------------------------
# tool_use — multi-turn calculate loop, target 105.63
# ---------------------------------------------------------------------------


def test_tool_use_correct_with_tool_scores_100() -> None:
    outcome = score_tool_use_text("TOTAL: 105.63", tool_used=True)
    assert outcome.score == pytest.approx(100.0)
    assert outcome.passed is True


def test_tool_use_correct_without_tool_scores_60() -> None:
    outcome = score_tool_use_text("TOTAL: 105.63", tool_used=False)
    assert outcome.score == pytest.approx(60.0)
    assert outcome.passed is False


def test_tool_use_close_answer_scores_20() -> None:
    outcome = score_tool_use_text("the total is 107.20", tool_used=True)
    assert outcome.score == pytest.approx(20.0)
    assert outcome.passed is False


def test_tool_use_wrong_answer_scores_0() -> None:
    outcome = score_tool_use_text("TOTAL: 999.00", tool_used=True)
    assert outcome.score == pytest.approx(0.0)
    assert outcome.passed is False


def test_tool_use_currency_and_prose_parsed() -> None:
    outcome = score_tool_use_text(
        "After tax the final answer comes to $105.63", tool_used=True
    )
    assert outcome.score == pytest.approx(100.0)


def test_native_calculate_safe_arithmetic() -> None:
    assert float(_safe_calculate("52.5 + 49.98")) == pytest.approx(102.48)
    assert float(_safe_calculate("102.48 * 0.95")) == pytest.approx(97.356)
    assert float(_safe_calculate("(3 * 17.50) + (2 * 24.99)")) == pytest.approx(102.48)
    out = _safe_calculate("__import__('os').system('echo hi')")
    assert out.startswith("ERROR")


# ---------------------------------------------------------------------------
# vision — 4 squares: count (25) + colors (4x15) + positions (4x3.75)
# ---------------------------------------------------------------------------


def test_vision_perfect_answer_scores_100() -> None:
    text = (
        "There are four solid squares: red in top-left, blue in top-right, "
        "green in bottom-left, yellow in bottom-right."
    )
    outcome = score_vision(text)
    assert outcome.score == pytest.approx(100.0)
    assert outcome.passed is True


def test_vision_colors_without_positions_scores_85() -> None:
    text = "four squares colored red, blue, green, and yellow"
    outcome = score_vision(text)
    assert outcome.score == pytest.approx(85.0)
    assert outcome.passed is True  # >= 80


def test_vision_partial_counts_scores_low() -> None:
    outcome = score_vision("there are 3 red squares")
    assert outcome.score == pytest.approx(15.0)
    assert outcome.passed is False


# ---------------------------------------------------------------------------
# speed — tok/s tiers (same constants as v1)
# ---------------------------------------------------------------------------


def test_speed_tier_boundaries() -> None:
    assert score_speed(1000, 10_000, "hi").score == 90.0   # 100 t/s > 80
    assert score_speed(600, 10_000, "hi").score == 75.0    # 60 t/s > 50
    assert score_speed(400, 10_000, "hi").score == 60.0    # 40 t/s > 30
    assert score_speed(200, 10_000, "hi").score == 45.0    # 20 t/s > 15
    assert score_speed(100, 10_000, "hi").score == 30.0    # 10 t/s
    assert score_speed(0, 0, "hi").score == 30.0           # zero guard


# ---------------------------------------------------------------------------
# long_horizon — CPM over the 6-task graph, 4 graded components
# ---------------------------------------------------------------------------


def _perfect_long_horizon_answer() -> str:
    t = long_horizon_truths()
    slack_bits = ", ".join(f"{k}={v}" for k, v in t["non_critical_slack"].items())
    return (
        f"CRITICAL_PATH: {'->'.join(t['critical_path'])}\n"
        f"DURATION: {t['duration']} days\n"
        f"SLACK: {slack_bits}\n"
        "ACTION: NONE\n"
    )


def test_long_horizon_perfect_answer_scores_100() -> None:
    outcome = score_long_horizon(_perfect_long_horizon_answer())
    assert outcome.score == pytest.approx(100.0)
    assert outcome.passed is True
    labels = [lbl for lbl, _ in (outcome.item_scores or [])]
    assert labels == ["critical_path", "duration", "slack", "action"]


def test_long_horizon_wrong_duration_scores_75() -> None:
    answer = _perfect_long_horizon_answer().replace("DURATION: 15 days", "DURATION: 16 days")
    outcome = score_long_horizon(answer)
    assert outcome.score == pytest.approx(75.0)
    assert not outcome.passed
    assert [lbl for lbl, ok in (outcome.item_scores or []) if not ok] == ["duration"]


def test_long_horizon_garbage_scores_0() -> None:
    outcome = score_long_horizon("no structured fields at all")
    assert outcome.score == 0.0
    assert outcome.passed is False


# ---------------------------------------------------------------------------
# outcome type sanity (v1 shape preserved)
# ---------------------------------------------------------------------------


def test_outcome_dataclass_shape() -> None:
    o = _BenchmarkOutcome(score=10.0, passed=False, raw_output="x")
    assert o.score == 10.0 and o.passed is False and o.raw_output == "x"