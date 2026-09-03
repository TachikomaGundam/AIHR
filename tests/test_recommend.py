from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import typer

from hr.health import HealthReport
from hr.recommend import (
    RecommendationEngine,
    RecommendationResult,
    _heuristic_interval,
    default_constraints,
    format_recommendation_result,
)


def test_task_recommendation_excludes_models_missing_required_measurements() -> None:
    engine = RecommendationEngine.__new__(RecommendationEngine)
    engine._means = {
        "complete": {"vision": 80.0},
        "unmeasured": {"reasoning": 100.0},
    }
    engine._health = {
        model_id: HealthReport(model_id=model_id, sweep_id="sweep", n_measurements=1)
        for model_id in engine._means
    }

    recommendations = engine.recommend_for_task("inspect this image")

    assert recommendations == [("complete", 80.0)]


def test_task_recommendation_excludes_models_failing_measured_health_gate() -> None:
    engine = RecommendationEngine.__new__(RecommendationEngine)
    engine._means = {
        "reliable": {"reasoning": 80.0},
        "looping": {"reasoning": 100.0},
    }
    engine._health = {
        "reliable": HealthReport(model_id="reliable", sweep_id="sweep", n_measurements=1),
        "looping": HealthReport(
            model_id="looping", sweep_id="sweep", n_measurements=1, loop_mean=0.5
        ),
    }

    recommendations = engine.recommend_for_task("reason about this problem")

    assert recommendations == [("reliable", 80.0)]


def test_recommend_with_constraints_filters_by_freshness() -> None:
    from hr.recommendation_constraints import (
        FreshnessConstraint,
        RecommendationConstraints,
    )
    
    engine = RecommendationEngine.__new__(RecommendationEngine)
    engine._sweep_id = "test-sweep"
    engine._means = {"model-a": {"reasoning": 90.0}}
    engine._health = {}
    engine._conn = None
    
    old_date = datetime.now(timezone.utc) - timedelta(days=60)
    engine._get_sweep_created_at = lambda: old_date
    engine._get_latency_stats = lambda: {}
    engine._get_success_rates = lambda: {}
    engine._get_separation_probabilities = lambda: {}
    
    constraints = RecommendationConstraints(
        freshness=FreshnessConstraint(max_age_days=30)
    )
    
    results = engine.recommend_with_constraints("reasoning task", constraints)
    
    assert len(results) == 1
    model_id, score, reasons = results[0]
    assert model_id == "model-a"
    assert len(reasons) > 0
    assert any("older than" in r for r in reasons)


def test_recommend_with_constraints_passes_when_no_constraints() -> None:
    from hr.recommendation_constraints import RecommendationConstraints
    
    engine = RecommendationEngine.__new__(RecommendationEngine)
    engine._sweep_id = "test-sweep"
    engine._means = {"model-a": {"reasoning": 90.0}}
    engine._health = {}
    engine._conn = None
    
    engine._get_sweep_created_at = lambda: datetime.now(timezone.utc)
    engine._get_latency_stats = lambda: {}
    engine._get_success_rates = lambda: {}
    engine._get_separation_probabilities = lambda: {}
    
    constraints = RecommendationConstraints()
    
    results = engine.recommend_with_constraints("reasoning task", constraints)
    
    assert len(results) == 1
    model_id, score, reasons = results[0]
    assert model_id == "model-a"
    assert len(reasons) == 0


def test_get_latency_stats_calculates_percentiles() -> None:
    from unittest.mock import MagicMock
    
    engine = RecommendationEngine.__new__(RecommendationEngine)
    engine._sweep_id = "test-sweep"
    
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    
    mock_cursor.fetchall.return_value = [
        ("model-a", 1000),
        ("model-a", 1100),
        ("model-a", 1200),
        ("model-a", 1300),
        ("model-a", 1400),
        ("model-a", 1500),
        ("model-a", 1600),
        ("model-a", 1700),
        ("model-a", 1800),
        ("model-a", 1900),
    ]
    
    engine._conn = mock_conn
    
    stats = engine._get_latency_stats()
    
    assert "model-a" in stats
    assert "p50" in stats["model-a"]
    assert "p95" in stats["model-a"]
    assert 1400 <= stats["model-a"]["p50"] <= 1500
    assert 1800 <= stats["model-a"]["p95"] <= 1900


def test_get_success_rates_calculates_from_run_status() -> None:
    from unittest.mock import MagicMock
    
    engine = RecommendationEngine.__new__(RecommendationEngine)
    engine._sweep_id = "test-sweep"
    
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    
    mock_cursor.fetchall.return_value = [
        ("model-a", 3, 2),
        ("model-b", 1, 1),
    ]
    
    engine._conn = mock_conn
    
    rates = engine._get_success_rates()
    
    assert rates["model-a"] == 2.0 / 3.0
    assert rates["model-b"] == 1.0


def test_get_sweep_created_at_returns_timestamp() -> None:
    from unittest.mock import MagicMock
    
    engine = RecommendationEngine.__new__(RecommendationEngine)
    engine._sweep_id = "test-sweep"
    
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    
    test_time = datetime.now(timezone.utc)
    mock_cursor.fetchone.return_value = (test_time,)
    
    engine._conn = mock_conn
    
    result = engine._get_sweep_created_at()
    
    assert result == test_time


def test_get_sweep_created_at_returns_none_when_no_sweep() -> None:
    engine = RecommendationEngine.__new__(RecommendationEngine)
    engine._sweep_id = None
    
    result = engine._get_sweep_created_at()
    
    assert result is None


def test_get_separation_probabilities_returns_empty_when_no_sweep() -> None:
    engine = RecommendationEngine.__new__(RecommendationEngine)
    engine._sweep_id = None

    result = engine._get_separation_probabilities()

    assert result == {}


# ---------------------------------------------------------------------------
# Tri-state recommend() API: eligible / excluded / indeterminate with evidence
# ---------------------------------------------------------------------------


def _make_engine(**overrides: object) -> RecommendationEngine:
    base: dict[str, object] = {
        "sweep_id": "sw-test",
        "means": {"alpha": {"reasoning": 90.0}},
        "health": {
            "alpha": HealthReport(model_id="alpha", sweep_id="sw-test", n_measurements=1),
        },
        "sweep_created_at": datetime.now(timezone.utc),
        "latency_stats": {"alpha": {"p50": 500.0, "p95": 2000.0}},
        "success_rates": {"alpha": 1.0},
        # sentinel: default separator requires a rival and passes confidently
        "separation_vs_rival": None,
    }
    base.update(overrides)

    engine = RecommendationEngine.__new__(RecommendationEngine)
    engine._sweep_id = base["sweep_id"]
    engine._means = base["means"]  # type: ignore[assignment]
    engine._health = base["health"]  # type: ignore[assignment]
    engine._conn = None
    engine._get_sweep_created_at = lambda: base["sweep_created_at"]
    engine._get_latency_stats = lambda: base["latency_stats"]
    engine._get_success_rates = lambda: base["success_rates"]
    if base["separation_vs_rival"] is None:
        engine._separation_vs_rival = (
            lambda model_id, rival_id, batteries: None if rival_id is None else 0.95
        )
    else:
        engine._separation_vs_rival = base["separation_vs_rival"]  # type: ignore[assignment]
    return engine


def test_default_constraints_enforce_all_five_with_documented_values() -> None:
    policy = default_constraints()

    assert policy.freshness is not None and policy.freshness.max_age_days == 30
    assert policy.cost is not None and policy.cost.max_cost_per_request_usd == 0.10
    assert policy.latency is not None
    assert policy.latency.max_latency_p50_ms == 60_000
    assert policy.latency.max_latency_p95_ms == 120_000
    assert policy.reliability is not None
    assert policy.reliability.min_success_rate == 0.95
    assert policy.uncertainty is not None
    assert policy.uncertainty.min_separation_confidence == 0.80


def test_recommend_eligible_with_full_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hr.recommend._load_pricing", lambda: {"alpha": 1.0, "beta": 1.0})
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"reasoning": 4000})
    engine = _make_engine(
        means={"alpha": {"reasoning": 90.0}, "beta": {"reasoning": 80.0}},
        health={
            model: HealthReport(model_id=model, sweep_id="sw-test", n_measurements=1)
            for model in ("alpha", "beta")
        },
        latency_stats={
            "alpha": {"p50": 500.0, "p95": 2000.0},
            "beta": {"p50": 500.0, "p95": 2000.0},
        },
        success_rates={"alpha": 1.0, "beta": 1.0},
    )

    result = engine.recommend("reason about this problem")

    assert result.sweep_id == "sw-test"
    assert result.batteries == ("reasoning",)
    assert result.sweep_age_days is not None and result.sweep_age_days < 1.0
    assert [item.model_id for item in result.eligible] == ["alpha", "beta"]
    assert result.excluded == ()
    assert result.indeterminate == ()

    item = result.eligible[0]
    assert item.model_id == "alpha"
    assert item.status == "eligible"
    assert item.score == pytest.approx(90.0)
    assert item.interval is not None
    lo, hi = item.interval
    assert 0.0 <= lo <= item.score <= hi
    assert set(item.thresholds) >= {
        "freshness_max_age_days",
        "cost_max_per_request_usd",
        "latency_max_p50_ms",
        "latency_max_p95_ms",
        "reliability_min_success_rate",
        "uncertainty_min_separation_confidence",
        "health_gate",
    }
    assert len(item.caveats) > 0
    assert item.reasons == ()
    assert item.cost_estimate_usd == pytest.approx(4000.0 / 1_000_000.0)


def test_recommend_stale_evidence_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hr.recommend._load_pricing", lambda: {"alpha": 1.0})
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"reasoning": 4000})
    stale = datetime.now(timezone.utc) - timedelta(days=60)

    result = _make_engine(sweep_created_at=stale).recommend("reason about this problem")

    assert result.eligible == ()
    assert len(result.excluded) == 1
    item = result.excluded[0]
    assert item.status == "excluded"
    assert any("freshness" in reason for reason in item.reasons)
    assert result.indeterminate == ()


def test_recommend_over_budget_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hr.recommend._load_pricing", lambda: {"alpha": 1000.0, "beta": 0.5})
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"reasoning": 4000})
    engine = _make_engine(
        means={"alpha": {"reasoning": 90.0}, "beta": {"reasoning": 80.0}},
        health={
            "alpha": HealthReport(model_id="alpha", sweep_id="sw-test", n_measurements=1),
            "beta": HealthReport(model_id="beta", sweep_id="sw-test", n_measurements=1),
        },
        latency_stats={
            "alpha": {"p50": 500.0, "p95": 2000.0},
            "beta": {"p50": 500.0, "p95": 2000.0},
        },
        success_rates={"alpha": 1.0, "beta": 1.0},
    )

    result = engine.recommend("reason about this problem")

    assert [i.model_id for i in result.eligible] == ["beta"]
    assert [i.model_id for i in result.excluded] == ["alpha"]
    assert any("cost" in reason for reason in result.excluded[0].reasons)


def test_recommend_high_latency_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hr.recommend._load_pricing", lambda: {"alpha": 1.0})
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"reasoning": 4000})

    result = _make_engine(
        latency_stats={"alpha": {"p50": 300_000.0, "p95": 400_000.0}}
    ).recommend("reason about this problem")

    assert result.eligible == ()
    assert [i.model_id for i in result.excluded] == ["alpha"]
    assert any("latency" in reason for reason in result.excluded[0].reasons)


def test_recommend_low_reliability_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hr.recommend._load_pricing", lambda: {"alpha": 1.0})
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"reasoning": 4000})

    result = _make_engine(success_rates={"alpha": 0.5}).recommend(
        "reason about this problem"
    )

    assert result.eligible == ()
    assert [i.model_id for i in result.excluded] == ["alpha"]
    assert any("reliability" in reason for reason in result.excluded[0].reasons)


def test_recommend_missing_evidence_is_indeterminate_not_eligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("hr.recommend._load_pricing", lambda: {"alpha": 1.0})
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"reasoning": 4000})

    result = _make_engine(latency_stats={}).recommend("reason about this problem")

    assert result.eligible == ()
    assert result.excluded == ()
    assert [i.model_id for i in result.indeterminate] == ["alpha"]
    assert any("missing evidence" in reason for reason in result.indeterminate[0].reasons)


def test_recommend_no_sweep_at_all_is_indeterminate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hr.recommend._load_pricing", lambda: {"alpha": 1.0})
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"reasoning": 4000})

    result = _make_engine(
        sweep_id=None,
        sweep_created_at=None,
        latency_stats={},
        success_rates={},
    ).recommend("reason about this problem")

    assert result.sweep_id is None
    assert result.sweep_age_days is None
    assert result.eligible == ()
    assert result.excluded == ()
    assert [i.model_id for i in result.indeterminate] == ["alpha"]
    assert any("freshness" in reason for reason in result.indeterminate[0].reasons)


def test_recommend_low_separation_is_indeterminate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hr.recommend._load_pricing", lambda: {"alpha": 1.0, "beta": 1.0})
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"reasoning": 4000})
    engine = _make_engine(
        means={"alpha": {"reasoning": 90.0}, "beta": {"reasoning": 80.0}},
        health={
            "alpha": HealthReport(model_id="alpha", sweep_id="sw-test", n_measurements=1),
            "beta": HealthReport(model_id="beta", sweep_id="sw-test", n_measurements=1),
        },
        latency_stats={
            "alpha": {"p50": 500.0, "p95": 2000.0},
            "beta": {"p50": 500.0, "p95": 2000.0},
        },
        success_rates={"alpha": 1.0, "beta": 1.0},
        separation_vs_rival=lambda model_id, rival_id, batteries: 0.5,
    )

    result = engine.recommend("reason about this problem")

    assert result.eligible == ()
    assert result.excluded == ()
    assert {i.model_id for i in result.indeterminate} == {"alpha", "beta"}
    assert all(
        any("uncertainty" in reason for reason in item.reasons)
        for item in result.indeterminate
    )


def test_recommend_health_gate_fail_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hr.recommend._load_pricing", lambda: {"alpha": 1.0})
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"reasoning": 4000})
    engine = _make_engine(
        health={
            "alpha": HealthReport(
                model_id="alpha", sweep_id="sw-test", n_measurements=1, loop_mean=0.5
            ),
        }
    )

    result = engine.recommend("reason about this problem")

    assert result.eligible == ()
    assert [i.model_id for i in result.excluded] == ["alpha"]
    assert any("health" in reason for reason in result.excluded[0].reasons)


def test_recommend_no_measurements_for_task_batteries_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("hr.recommend._load_pricing", lambda: {"alpha": 1.0})
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"reasoning": 4000})

    result = _make_engine(means={"alpha": {"vision": 80.0}}).recommend(
        "reason about this problem"
    )

    assert result.eligible == ()
    assert result.excluded == ()
    assert [i.model_id for i in result.indeterminate] == ["alpha"]
    assert result.indeterminate[0].reasons == ("missing battery reasoning",)


def test_recommend_single_model_without_rival_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("hr.recommend._load_pricing", lambda: {"alpha": 1.0})
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"reasoning": 4000})

    result = _make_engine().recommend("reason about this problem")

    assert result.eligible == ()
    assert [i.model_id for i in result.indeterminate] == ["alpha"]
    assert any("uncertainty" in reason for reason in result.indeterminate[0].reasons)


def test_recommend_falls_back_to_stage0_token_estimate_when_profile_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "hr.recommend._load_pricing", lambda: {"alpha": 2.0, "beta": 2.0}
    )
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {})
    engine = _make_engine(
        means={"alpha": {"reasoning": 90.0}, "beta": {"reasoning": 80.0}},
        health={
            "alpha": HealthReport(model_id="alpha", sweep_id="sw-test", n_measurements=1),
            "beta": HealthReport(model_id="beta", sweep_id="sw-test", n_measurements=1),
        },
        latency_stats={
            "alpha": {"p50": 500.0, "p95": 2000.0},
            "beta": {"p50": 500.0, "p95": 2000.0},
        },
        success_rates={"alpha": 1.0, "beta": 1.0},
    )

    result = engine.recommend("reason about this problem")

    assert [item.model_id for item in result.eligible] == ["alpha", "beta"]
    assert result.eligible[0].cost_estimate_usd == pytest.approx(
        2.0 * 5000 / 1_000_000.0
    )


def test_recommend_result_shape_and_renderings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hr.recommend._load_pricing", lambda: {"alpha": 1.0, "beta": 1.0, "gamma": 1.0})
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"reasoning": 4000})
    engine = _make_engine(
        means={
            "alpha": {"reasoning": 90.0},
            "beta": {"reasoning": 80.0},
            "gamma": {"reasoning": 70.0},
        },
        health={
            model: HealthReport(model_id=model, sweep_id="sw-test", n_measurements=1)
            for model in ("alpha", "beta", "gamma")
        },
        latency_stats={
            "alpha": {"p50": 500.0, "p95": 2000.0},
            "beta": {"p50": 300_000.0, "p95": 400_000.0},
        },
        success_rates={"alpha": 1.0, "beta": 1.0, "gamma": 1.0},
    )

    result = engine.recommend("reason about this problem")

    assert [i.model_id for i in result.eligible] == ["alpha"]
    assert [i.model_id for i in result.excluded] == ["beta"]
    assert [i.model_id for i in result.indeterminate] == ["gamma"]

    payload = json.loads(format_recommendation_result(result, fmt="json"))
    assert set(payload) == {
        "task",
        "batteries",
        "sweep_id",
        "sweep_age_days",
        "eligible",
        "excluded",
        "indeterminate",
    }
    assert payload["sweep_id"] == "sw-test"
    assert payload["batteries"] == ["reasoning"]
    assert len(payload["eligible"]) == 1
    item = payload["eligible"][0]
    assert set(item) == {
        "model_id",
        "status",
        "score",
        "interval",
        "thresholds",
        "caveats",
        "reasons",
        "cost_estimate_usd",
    }
    assert item["model_id"] == "alpha"
    assert item["status"] == "eligible"
    assert item["score"] == pytest.approx(90.0)
    assert isinstance(item["interval"], list) and len(item["interval"]) == 2
    assert item["reasons"] == []
    assert len(payload["excluded"]) == 1
    assert payload["excluded"][0]["model_id"] == "beta"
    assert payload["excluded"][0]["reasons"]
    assert len(payload["indeterminate"]) == 1
    assert payload["indeterminate"][0]["model_id"] == "gamma"
    assert payload["indeterminate"][0]["reasons"]

    table = format_recommendation_result(result, fmt="table")
    for marker in (
        "# Task:",
        "# Batteries:",
        "# Sweep:",
        "# Policy:",
        "ELIGIBLE (1):",
        "EXCLUDED (1):",
        "INDETERMINATE (1):",
    ):
        assert marker in table
    assert "alpha" in table
    assert "beta" in table
    assert "gamma" in table


# ---------------------------------------------------------------------------
# A1: RecommendationEngine connection lifecycle (close + context manager)
# ---------------------------------------------------------------------------


def test_recommendation_engine_close_closes_connection() -> None:
    from unittest.mock import MagicMock

    engine = RecommendationEngine.__new__(RecommendationEngine)
    conn = MagicMock()
    engine._conn = conn

    engine.close()

    conn.close.assert_called_once_with()
    # close() is idempotent: a second call is a no-op, not a double close
    engine.close()
    conn.close.assert_called_once_with()


def test_recommendation_engine_is_a_context_manager() -> None:
    from unittest.mock import MagicMock

    engine = RecommendationEngine.__new__(RecommendationEngine)
    conn = MagicMock()
    engine._conn = conn

    with engine as ctx:
        assert ctx is engine
        conn.close.assert_not_called()

    conn.close.assert_called_once_with()


class _SpyCliEngine:
    def __init__(self) -> None:
        from unittest.mock import MagicMock

        self.closed = False
        self.boom_on_recommend: RuntimeError | None = None
        # legacy code closes the private attribute directly; give it a mock
        # so the red test fails on the missing public close(), not on an
        # AttributeError inside the CLI
        self._conn = MagicMock()

    def recommend(self, task: str) -> RecommendationResult:
        if self.boom_on_recommend is not None:
            raise self.boom_on_recommend
        return RecommendationResult(
            task=task,
            batteries=("reasoning",),
            sweep_id=None,
            sweep_age_days=None,
            eligible=(),
            excluded=(),
            indeterminate=(),
        )

    def seat_recommendations(self, seats: object) -> str:
        return ""

    def close(self) -> None:
        self.closed = True


def _patch_cli_engine(monkeypatch: pytest.MonkeyPatch) -> _SpyCliEngine:
    from hr.cli_knowledge import recommend as cli_recommend  # noqa: F401

    spy = _SpyCliEngine()
    monkeypatch.setattr("hr.recommend.RecommendationEngine", lambda: spy)
    return spy


def test_cli_recommend_closes_engine_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hr.cli_knowledge import recommend as cli_recommend

    spy = _patch_cli_engine(monkeypatch)

    cli_recommend(task="reason about this problem")

    assert spy.closed is True


def test_cli_recommend_closes_engine_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hr.cli_knowledge import recommend as cli_recommend

    spy = _patch_cli_engine(monkeypatch)
    spy.boom_on_recommend = RuntimeError("boom")

    with pytest.raises(typer.Exit):
        cli_recommend(task="reason about this problem")

    assert spy.closed is True


def test_cli_recommend_seat_path_closes_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hr.cli_knowledge import recommend as cli_recommend

    spy = _patch_cli_engine(monkeypatch)
    monkeypatch.setattr("hr.recommend.load_seat_specs", lambda: [])

    cli_recommend(task=None)

    assert spy.closed is True


# ---------------------------------------------------------------------------
# A2: partial battery coverage is indeterminate, never scored with 0.0
# ---------------------------------------------------------------------------


def test_recommend_partial_battery_coverage_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: two models that both lack the code_gen battery the task needs
    # When: recommend() evaluates "reason and code"
    # Then: both are indeterminate with "missing battery code_gen" — partial
    # coverage never scores the missing batteries as 0.0 (unified with
    # recommend_for_task / recommend_with_constraints full-coverage rules)
    monkeypatch.setattr("hr.recommend._load_pricing", lambda: {"alpha": 1.0, "beta": 1.0})
    monkeypatch.setattr(
        "hr.recommend._load_tokens_per_call", lambda: {"reasoning": 4000, "code_gen": 3000}
    )
    engine = _make_engine(
        means={"alpha": {"reasoning": 90.0}, "beta": {"reasoning": 75.0}},
        health={
            model: HealthReport(model_id=model, sweep_id="sw-test", n_measurements=1)
            for model in ("alpha", "beta")
        },
        latency_stats={
            model: {"p50": 500.0, "p95": 2000.0} for model in ("alpha", "beta")
        },
        success_rates={"alpha": 1.0, "beta": 1.0},
    )

    result = engine.recommend("reason and code")

    assert result.eligible == ()
    assert result.excluded == ()
    assert {item.model_id for item in result.indeterminate} == {"alpha", "beta"}
    assert all(
        item.reasons == ("missing battery code_gen",)
        for item in result.indeterminate
    )


# ---------------------------------------------------------------------------
# A3: heuristic interval clamps to the score's natural scale
# ---------------------------------------------------------------------------


def test_heuristic_interval_clamps_upper_bound_to_score_scale() -> None:
    # Given: a 0-100-scale score of 100 (normalizes to 1.0)
    # When: the interval is computed with any separation probability
    # Then: the upper bound stays <= 1.0 in normalized units and
    # lo <= score <= hi holds — plus the same contract on [0, 1] inputs
    lo, hi = _heuristic_interval(100.0, 0.5)
    assert hi / 100.0 <= 1.0
    assert lo <= 100.0 <= hi

    lo, hi = _heuristic_interval(100.0, 0.0)
    assert hi / 100.0 <= 1.0
    assert lo <= 100.0 <= hi

    # same contract on the normalized [0, 1] scale
    lo, hi = _heuristic_interval(1.0, 0.0)
    assert lo <= 1.0 <= hi <= 1.0

    # mid-range 0-100 scores keep the prior semantics (no upper squeeze)
    lo, hi = _heuristic_interval(90.0, 0.95)
    assert lo <= 90.0 <= hi


# ---------------------------------------------------------------------------
# A4: latency percentiles use linear interpolation (np.percentile default)
# ---------------------------------------------------------------------------


def test_get_latency_stats_uses_linear_interpolation_percentiles() -> None:
    from unittest.mock import MagicMock

    engine = RecommendationEngine.__new__(RecommendationEngine)
    engine._sweep_id = "test-sweep"

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchall.return_value = [("model-a", v) for v in (0, 1, 2, 3)]
    engine._conn = mock_conn

    stats = engine._get_latency_stats()

    assert stats["model-a"]["p50"] == 1.5
    assert stats["model-a"]["p95"] == pytest.approx(2.85)
    assert stats == {"model-a": {"p50": 1.5, "p95": pytest.approx(2.85)}}


# ---------------------------------------------------------------------------
# T6 (audit bug 10): custom seats injected via a seats.local.yaml overlay are
# not covered by the measured SEAT_CODES, so seat_assignments() never returns
# a row for them; the table builder used to crash with a raw KeyError.
# ---------------------------------------------------------------------------


class _EmptyCursor:
    def __init__(self) -> None:
        self.description = []

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self) -> list:
        return []

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        pass


class _EmptyConn:
    def cursor(self, cursor_factory=None):
        return _EmptyCursor()

    def close(self) -> None:
        pass


def test_seat_recommendations_custom_overlay_seat_renders_no_data_row(
    hr_sandbox: dict,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Given a seats.local.yaml overlay appending an unmeasured seat,
    When building the seat recommendation table,
    Then it renders a no-data row at the seat-table position (no KeyError),
    warns, and every built-in seat keeps its existing row."""
    import logging
    import yaml

    from hr.recommend import load_seat_specs
    from hr.seats.health_gates import SEAT_HEALTH_GATE
    from tests.conftest import materialize_templates

    materialize_templates(hr_sandbox)
    builtin = load_seat_specs()
    assert len(builtin) == 18
    selected = builtin + [{"seat_code": "my_custom_seat", "domain": "test"}]
    # local overlay wins and lists are replaced, so the overlay carries the
    # built-in 18 plus the appended custom seat
    (hr_sandbox["configs"] / "seats.local.yaml").write_text(
        yaml.safe_dump({"seats": selected}), encoding="utf-8"
    )
    assert load_seat_specs() == selected

    monkeypatch.setattr("hr.recommend.get_connection", lambda: _EmptyConn())
    engine = RecommendationEngine()
    try:
        with caplog.at_level(logging.WARNING, logger="hr.recommend"):
            report = engine.seat_recommendations(load_seat_specs())
    finally:
        engine.close()

    rows = [
        line
        for line in report.splitlines()
        if line.startswith("| ") and not line.startswith("| seat")
    ]
    # (b) custom seat renders a no-data row at its seat-table position (last)
    assert rows[-1] == "| my_custom_seat | test | — | no-data |"
    # (d) built-in seats still render, in seat-table order, unchanged
    assert [row.split("|")[1].strip() for row in rows] == [
        str(seat["seat_code"]) for seat in selected
    ]
    for seat in builtin:
        expected = (
            f"| {seat['seat_code']} | {seat.get('domain', '—')} | — "
            f"| {SEAT_HEALTH_GATE[str(seat['seat_code'])]} |"
        )
        assert expected in rows
    # (c) a WARNING was logged naming the uncovered seat
    assert any(
        record.levelno == logging.WARNING
        and "my_custom_seat" in record.getMessage()
        and "SEAT_CODES" in record.getMessage()
        for record in caplog.records
    )
