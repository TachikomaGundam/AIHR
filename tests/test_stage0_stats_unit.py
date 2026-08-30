"""Stage-0 separation statistics contract tests (committed surface).

Exercises hr.stage0_stats: sweep-state grouping, infra-failure exclusion
policy, pairwise bootstrap separation (real stats functions plus
deterministic pseudo-patches for the classification branches), and the
separation matrix printer in both live-state and DB modes.
"""

from __future__ import annotations

import pytest

from hr.stage0_stats import (
    SweepState,
    _bootstrap_separation_from_state,
    _key,
    _print_matrix,
    print_separation_matrix,
    should_exclude_zero,
)


class _FakeCursor:
    def __init__(self, rows: list[tuple]):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def execute(self, sql, params) -> None:  # noqa: ARG002
        assert params is not None

    def fetchall(self) -> list[tuple]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[tuple]):
        self._rows = rows
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows)

    def close(self) -> None:
        self.closed = True


def test_sweep_state_defaults() -> None:
    state = SweepState(sweep_id="s1")
    assert state.total_tokens == 0
    assert state.total_calls == 0
    assert state.stopped_at_cap is False
    assert state.stopped_reason == ""
    assert state.measurements_by_model_battery == {}


def test_key_joins_model_and_battery() -> None:
    assert _key("m1", "reasoning") == "m1|reasoning"


@pytest.mark.parametrize(
    ("infra", "expected"),
    [
        (None, False),
        ("RATE_LIMIT", True),
        ("SERVER_5XX", True),
        ("TIMEOUT", True),
        ("GATEWAY_4XX", True),
        ("EMPTY_RESPONSE", False),  # terminal
        ("SCHEMA_INVALID", False),
        ("CONTENT_FILTER", False),
        ("UNKNOWN", False),
        ("NOT_A_REAL_CODE", False),  # unknown string -> not retryable
    ],
)
def test_should_exclude_zero_policy(infra: str | None, expected: bool) -> None:
    assert should_exclude_zero(infra) is expected


def test_bootstrap_separation_separated_with_real_stats() -> None:
    state = SweepState(sweep_id="s1")
    state.measurements_by_model_battery = {
        "winner|reasoning": {"i1": [1.0] * 10, "i2": [1.0] * 10},
        "loser|reasoning": {"i1": [0.0] * 10, "i2": [0.0] * 10},
    }
    result = _bootstrap_separation_from_state(state)
    assert set(result.keys()) == {"reasoning"}
    (pair,) = result["reasoning"]
    assert pair["model_a"] == "winner"
    assert pair["model_b"] == "loser"
    assert pair["p_separated"] == 1.0
    assert pair["p_weak"] == 0.0
    assert pair["p_tie"] == 0.0
    assert pair["directional"] is True


def test_bootstrap_separation_tie_is_exact_tie() -> None:
    state = SweepState(sweep_id="s1")
    state.measurements_by_model_battery = {
        "m1|reasoning": {"i1": [0.5] * 10},
        "m2|reasoning": {"i1": [0.5] * 10},
    }
    result = _bootstrap_separation_from_state(state)
    (pair,) = result["reasoning"]
    # Identical arrays can never separate; the three probabilities sum to 1.
    assert pair["p_separated"] + pair["p_weak"] + pair["p_tie"] == pytest.approx(1.0)
    assert pair["directional"] is True


def test_bootstrap_separation_branches_weak_and_tie(monkeypatch) -> None:
    """Deterministic classification branches (pseudo bootstrap).

    p_a/p_b fixed per model pair: the winner is the side with the larger
    comparison, then classify() selects the dict shape.
    """
    import hr.stats.bootstrap as bootstrap_mod

    def fake_paired(px: list[float], py: list[float]) -> float:  # noqa: ARG001
        return {0.9: 0.8, 0.1: 0.2, 0.5: 0.5, 0.97: 0.97, 0.03: 0.03}[px[0]]

    def fake_classify(p: float) -> str:
        return {0.8: "weak", 0.5: "tie", 0.97: "separated"}[p]

    monkeypatch.setattr(bootstrap_mod, "paired_bootstrap_separation", fake_paired)
    monkeypatch.setattr(bootstrap_mod, "classify", fake_classify)

    state = SweepState(sweep_id="s1")
    state.measurements_by_model_battery = {
        "model-a|reasoning": {"i1": [0.9]},
        "model-b|reasoning": {"i1": [0.1]},
        "model-c|tool_a": {"i1": [0.5]},
        "model-d|tool_a": {"i1": [0.5]},
        "model-e|vision": {"i1": [0.97]},
        "model-f|vision": {"i1": [0.03]},
    }
    result = _bootstrap_separation_from_state(state)

    (weak_pair,) = result["reasoning"]
    assert weak_pair["model_a"] == "model-a"
    assert weak_pair["model_b"] == "model-b"
    assert weak_pair["p_separated"] == 0.0
    assert weak_pair["p_weak"] == pytest.approx(0.8)
    assert weak_pair["p_tie"] == pytest.approx(0.2)

    (tie_pair,) = result["tool_a"]
    assert tie_pair["p_separated"] == 0.0
    assert tie_pair["p_weak"] == 0.0
    assert tie_pair["p_tie"] == 1.0

    (sep_pair,) = result["vision"]
    assert sep_pair["p_separated"] == pytest.approx(0.97)
    assert sep_pair["p_weak"] == pytest.approx(0.03)
    assert sep_pair["p_tie"] == 0.0


def test_bootstrap_separation_skips_empty_measurements() -> None:
    state = SweepState(sweep_id="s1")
    state.measurements_by_model_battery = {
        "m1|reasoning": {"i1": [], "i2": []},
    }
    assert _bootstrap_separation_from_state(state) == {"reasoning": []}


def test_print_separation_matrix_from_state(capsys) -> None:
    state = SweepState(sweep_id="s1")
    state.measurements_by_model_battery = {
        "a|reasoning": {"i1": [1.0] * 8},
        "b|reasoning": {"i1": [0.0] * 8},
    }
    print_separation_matrix(state=state)
    out = capsys.readouterr().out
    assert "=== Stage 0 Separation Matrix ===" in out
    assert "--- Battery: reasoning ---" in out
    assert "Separated: 1 pairs | Weak: 0 pairs | Tie: 0 pairs" in out


def test_print_separation_matrix_db_branch(monkeypatch, capsys) -> None:
    import hr.db

    conn = _FakeConn(
        [
            ("battery-reasoning", "model-longname-a", "model-b", 0.9, 0.1, 0.0),
            ("battery-reasoning", "model-b", "model-longname-a", 0.1, 0.9, 0.0),
        ]
    )
    monkeypatch.setattr(hr.db, "connect", lambda: conn)
    print_separation_matrix(sweep_id="sweep-1")
    out = capsys.readouterr().out
    assert conn.closed  # owned connection closed on every path
    assert "--- Battery: reasoning ---" in out
    assert "sep" in out


def test_print_separation_matrix_no_args(capsys) -> None:
    print_separation_matrix()
    assert "Provide sweep_id or live state." in capsys.readouterr().out


def test_print_matrix_empty_pairs(capsys) -> None:
    _print_matrix({"reasoning": []})
    out = capsys.readouterr().out
    assert "--- Battery: reasoning ---" in out
    assert "(no pairs recorded)" in out


def test_print_matrix_renders_labels(monkeypatch, capsys) -> None:
    # Deterministic labels via pseudo-stats: a over b -> sep; c vs d -> tie.
    import hr.stats.bootstrap as bootstrap_mod

    monkeypatch.setattr(
        bootstrap_mod,
        "paired_bootstrap_separation",
        lambda px, py: 0.8 if px and px[0] > py[0] else 0.2,
    )
    monkeypatch.setattr(
        bootstrap_mod,
        "classify",
        lambda p: "weak" if p >= 0.05 else "tie",
    )
    state = SweepState(sweep_id="s1")
    state.measurements_by_model_battery = {
        "alpha|reasoning": {"i1": [0.9]},
        "beta|reasoning": {"i1": [0.1]},
    }
    print_separation_matrix(state=state)
    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out
    assert "sep" in out or "weak" in out