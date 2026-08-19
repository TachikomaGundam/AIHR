"""Tests for hr2.health — behavioral-health analyzer."""

from __future__ import annotations

import pytest

from hr.health import (
    HealthReport,
    _answer_completion_rate,
    _has_final_answer,
    _loop_score,
    _self_consistency,
    _token_efficiency,
    _truncation_rate,
    compute_health,
    report,
)


# ---------------------------------------------------------------------------
# Loop score
# ---------------------------------------------------------------------------
class TestLoopScore:
    def test_empty_text_returns_none(self):
        assert _loop_score("") is None
        assert _loop_score(None) is None
        assert _loop_score("   \n   ") is None

    def test_clean_unique_text_is_low(self):
        clean = "\n".join(f"unique line number {i}" for i in range(50))
        s = _loop_score(clean)
        assert isinstance(s, float)
        assert s < 0.1

    def test_repeated_paragraph_is_high(self):
        block = "The quick brown fox jumps over the lazy dog. " * 2
        looping = (block + "\n") * 6
        s = _loop_score(looping)
        assert s is not None
        assert s >= 0.9

    def test_line_level_dup_only(self):
        text = ("\n".join(["the same line"] * 20))
        s = _loop_score(text)
        assert s is not None
        assert s >= 0.9

    def test_span_level_span_repeat_signal(self):
        chunk = "a" * 100
        s = _loop_score(chunk)
        assert s == 1.0

    def test_short_text_no_span_signal(self):
        s = _loop_score("hello world")
        assert isinstance(s, float)
        assert s == 0.0


# ---------------------------------------------------------------------------
# Truncation rate
# ---------------------------------------------------------------------------
class TestTruncationRate:
    def test_no_data(self):
        assert _truncation_rate([]) is None
        assert _truncation_rate([None, None]) is None

    def test_all_truncated(self):
        assert _truncation_rate([16000, 16384, 17000]) == 1.0

    def test_none_truncated(self):
        assert _truncation_rate([100, 500, 1000]) == 0.0

    def test_custom_cap(self):
        assert _truncation_rate([8000, 8192, 8100], cap=8100) == pytest.approx(
            2 / 3
        )


# ---------------------------------------------------------------------------
# Token efficiency
# ---------------------------------------------------------------------------
class TestTokenEfficiency:
    def test_zero_score_returns_none(self):
        assert _token_efficiency([100, 200], [0.0, 0.0]) is None

    def test_basic(self):
        e = _token_efficiency([1000, 1000], [0.5, 0.5])
        assert e == pytest.approx(2000.0)

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            _token_efficiency([1, 2], [0.5])


# ---------------------------------------------------------------------------
# Final-answer heuristic
# ---------------------------------------------------------------------------
class TestFinalAnswer:
    def test_empty_false(self):
        assert _has_final_answer("") is False
        assert _has_final_answer(None) is False

    def test_chinese_marker(self):
        assert _has_final_answer("经过思考...结论是42。") is True

    def test_trailing_number(self):
        assert _has_final_answer("the GCD is\n12.") is True

    def test_no_signal(self):
        assert _has_final_answer("just rambling text without a result") is False


# ---------------------------------------------------------------------------
# Answer completion rate
# ---------------------------------------------------------------------------
class TestAnswerCompletionRate:
    def test_no_rows_returns_none(self):
        assert _answer_completion_rate([]) is None

    def test_all_complete(self):
        rows = [
            {"response_text": "The answer is 42.", "tokens_out": 100},
            {"response_text": "结论: 42", "tokens_out": 200},
        ]
        r = _answer_completion_rate(rows)
        assert r == pytest.approx(1.0)

    def test_truncated_at_cap(self):
        rows = [
            {"response_text": "The answer conclusion", "tokens_out": 16384},
        ]
        r = _answer_completion_rate(rows)
        assert r == pytest.approx(0.0)

    def test_no_text_stored_rows_ignored(self):
        rows = [
            {"response_text": None, "tokens_out": 100},
            {"response_text": "", "tokens_out": 200},
            {"response_text": "42", "tokens_out": 50},
        ]
        r = _answer_completion_rate(rows)
        assert r == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Self consistency
# ---------------------------------------------------------------------------
class TestSelfConsistency:
    def test_empty(self):
        mr, up = _self_consistency([])
        assert mr is None and up is None

    def test_perfect_consistency(self):
        rows = [
            {"item_id": "i1", "score": 0.9},
            {"item_id": "i1", "score": 0.9},
            {"item_id": "i2", "score": 0.8},
            {"item_id": "i2", "score": 0.8},
        ]
        mr, up = _self_consistency(rows)
        assert mr == pytest.approx(0.0)
        assert up == pytest.approx(1.0)

    def test_divergent_reps(self):
        rows = [
            {"item_id": "i1", "score": 0.2},
            {"item_id": "i1", "score": 0.9},
            {"item_id": "i2", "score": 0.5},
            {"item_id": "i2", "score": 0.5},
        ]
        mr, up = _self_consistency(rows)
        assert mr == pytest.approx(0.35)
        assert up == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# compute_health + report via mock connection
# ---------------------------------------------------------------------------
class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.description = [("item_id",), ("score",), ("tokens_out",), ("response_text",)]

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return FakeCursor(self._rows)


def test_compute_health_aggregates():
    rows = [
        ("i1", 0.5, 1000, "The answer is 42."),
        ("i1", 0.5, 1000, "The answer is 42."),
        ("i2", 0.8, 2000, "结论: 42."),
        ("i2", 0.8, 2000, "结论: 42."),
    ]
    conn = FakeConn(rows)
    hr = compute_health("m1", "s1", conn)
    assert isinstance(hr, HealthReport)
    assert hr.n_measurements == 4
    assert hr.truncation_rate == pytest.approx(0.0)
    assert hr.token_efficiency is not None
    assert hr.consistency_mean_range == pytest.approx(0.0)
    assert hr.consistency_unanimity_pct == pytest.approx(1.0)
    assert hr.answer_completion_rate == pytest.approx(1.0)


def test_compute_health_empty_notes():
    conn = FakeConn([])
    hr = compute_health("m1", "s1", conn)
    assert hr.n_measurements == 0
    assert "no measurements" in hr.notes


def test_report_markdown_table():
    rows = [("i1", 0.9, 500, "42")]
    conn = FakeConn(rows)
    md = report(["m1"], "s1", conn)
    assert "model" in md
    assert "m1" in md
    assert md.count("\n") >= 2
