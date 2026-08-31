from __future__ import annotations

from tests.bench.test_scorers import (
    BenchmarkCategory,
    _ATTENTION_EXPECTED,
    _BUGGY_BURST_CODE,
    _CORRECT_CODE,
    _PERFECT_ATTENTION,
    _PERFECT_JSON,
    battery_item_labels,
    build_attention_probe,
    pytest,
    random,
    reasoning_truths,
    score_attention_probe,
    score_code_gen,
    score_instruction_follow,
    score_long_context,
    score_reasoning
)

@pytest.mark.sandbox
def test_code_gen_perfect_solution_scores_100() -> None:
    outcome = score_code_gen(_CORRECT_CODE)
    assert outcome.score == pytest.approx(100.0)
    assert outcome.passed is True
    assert len(outcome.item_scores or []) == 13
    assert all(passed for _, passed in outcome.item_scores or [])


@pytest.mark.sandbox
def test_code_gen_cannot_access_host_environment_or_filesystem(tmp_path, monkeypatch) -> None:
    # Given: otherwise-correct model code that probes host secrets and files.
    escaped_file = tmp_path / "escaped.txt"
    monkeypatch.setenv("HR_SANDBOX_SECRET", "must-not-leak")
    probe = (
        "import os\n"
        "try:\n"
        f"    open({str(escaped_file)!r}, 'w').write('escaped')\n"
        "except OSError:\n"
        "    pass\n"
        "if os.environ.get('HR_SANDBOX_SECRET'):\n"
        "    raise RuntimeError('host environment leaked')\n"
    )
    response = _CORRECT_CODE.replace("```\n", probe + "```\n")

    # When: the code-generation scorer executes the response.
    outcome = score_code_gen(response)

    # Then: grading succeeds inside isolation without host access.
    assert outcome.score == pytest.approx(100.0)
    assert not escaped_file.exists()


@pytest.mark.sandbox
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
