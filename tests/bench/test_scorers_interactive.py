from __future__ import annotations

from tests.bench.test_scorers import (
    STRESS_CANNED_TURNS,
    STRESS_CHECKPOINT_TURNS,
    _BenchmarkOutcome,
    _STRESS_BAD,
    _STRESS_GOOD,
    _STRESS_LABELS,
    _STRESS_TOKEN,
    _perfect_long_horizon_answer,
    _safe_calculate,
    _stress_checkpoints,
    build_stress_instruction,
    pytest,
    score_attention_stress,
    score_long_horizon,
    score_speed,
    score_tool_use_text,
    score_vision
)

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
    assert outcome.passed is True

def test_vision_partial_counts_scores_low() -> None:
    outcome = score_vision("there are 3 red squares")
    assert outcome.score == pytest.approx(15.0)
    assert outcome.passed is False

def test_speed_tier_boundaries() -> None:
    assert score_speed(1000, 10_000, "hi").score == 90.0   # 100 t/s > 80
    assert score_speed(600, 10_000, "hi").score == 75.0    # 60 t/s > 50
    assert score_speed(400, 10_000, "hi").score == 60.0    # 40 t/s > 30
    assert score_speed(200, 10_000, "hi").score == 45.0    # 20 t/s > 15
    assert score_speed(100, 10_000, "hi").score == 30.0    # 10 t/s
    assert score_speed(0, 0, "hi").score == 30.0

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

def test_outcome_dataclass_shape() -> None:
    o = _BenchmarkOutcome(score=10.0, passed=False, raw_output="x")
    assert o.score == 10.0 and o.passed is False and o.raw_output == "x"
