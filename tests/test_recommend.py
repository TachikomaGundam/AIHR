from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import typer

from hr.health import HealthReport
from hr.recommend import (
    RecommendationEngine,
    RecommendationResult,
    _combined_std_error,
    _estimate_task_tokens,
    _measured_interval,
    canonical_battery,
    default_constraints,
    format_recommendation_result,
)


def test_task_recommendation_excludes_models_missing_required_measurements() -> None:
    engine = RecommendationEngine.__new__(RecommendationEngine)
    engine._means = {
        "complete": {"livebench_vision": 80.0},
        "unmeasured": {"livebench_reasoning": 100.0},
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
        "reliable": {"livebench_reasoning": 80.0},
        "looping": {"livebench_reasoning": 100.0},
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
    engine._means = {"model-a": {"livebench_reasoning": 90.0}}
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
    engine._means = {"model-a": {"livebench_reasoning": 90.0}}
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
        "means": {"alpha": {"livebench_reasoning": 90.0}},
        "health": {
            "alpha": HealthReport(model_id="alpha", sweep_id="sw-test", n_measurements=1),
        },
        "sweep_created_at": datetime.now(timezone.utc),
        "latency_stats": {"alpha": {"p50": 500.0, "p95": 2000.0}},
        "success_rates": {"alpha": 1.0},
        # sentinel: default separator requires a rival and passes confidently
        "separation_vs_rival": None,
        "profile_freshness": None,
        "profile_stats": None,
    }
    base.update(overrides)

    engine = RecommendationEngine.__new__(RecommendationEngine)
    engine._sweep_id = base["sweep_id"]
    engine._means = base["means"]  # type: ignore[assignment]
    engine._health = base["health"]  # type: ignore[assignment]
    engine._conn = None
    engine._evidence_errors = []
    engine._get_sweep_created_at = lambda: base["sweep_created_at"]
    engine._get_latency_stats = lambda: base["latency_stats"]
    engine._get_success_rates = lambda: base["success_rates"]
    means_map: dict[str, dict[str, float]] = base["means"]  # type: ignore[assignment]
    profile_freshness = base["profile_freshness"]
    if profile_freshness is None:
        profile_freshness = {
            model_id: {battery: base["sweep_created_at"] for battery in model_means}
            for model_id, model_means in means_map.items()
        }
    profile_stats = base["profile_stats"]
    if profile_stats is None:
        profile_stats = {
            model_id: {battery: (3.0, 13) for battery in model_means}
            for model_id, model_means in means_map.items()
        }
    engine._get_profile_freshness = lambda: profile_freshness
    engine._get_profile_stats = lambda: profile_stats
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
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"livebench_reasoning": 4000})
    engine = _make_engine(
        means={"alpha": {"livebench_reasoning": 90.0}, "beta": {"livebench_reasoning": 80.0}},
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
    assert result.batteries == ("livebench_reasoning",)
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
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"livebench_reasoning": 4000})
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
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"livebench_reasoning": 4000})
    engine = _make_engine(
        means={"alpha": {"livebench_reasoning": 90.0}, "beta": {"livebench_reasoning": 80.0}},
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
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"livebench_reasoning": 4000})

    result = _make_engine(
        latency_stats={"alpha": {"p50": 300_000.0, "p95": 400_000.0}}
    ).recommend("reason about this problem")

    assert result.eligible == ()
    assert [i.model_id for i in result.excluded] == ["alpha"]
    assert any("latency" in reason for reason in result.excluded[0].reasons)


def test_recommend_low_reliability_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hr.recommend._load_pricing", lambda: {"alpha": 1.0})
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"livebench_reasoning": 4000})

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
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"livebench_reasoning": 4000})

    result = _make_engine(latency_stats={}).recommend("reason about this problem")

    assert result.eligible == ()
    assert result.excluded == ()
    assert [i.model_id for i in result.indeterminate] == ["alpha"]
    assert any("missing evidence" in reason for reason in result.indeterminate[0].reasons)


def test_recommend_no_sweep_at_all_is_indeterminate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hr.recommend._load_pricing", lambda: {"alpha": 1.0})
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"livebench_reasoning": 4000})

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
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"livebench_reasoning": 4000})
    engine = _make_engine(
        means={"alpha": {"livebench_reasoning": 90.0}, "beta": {"livebench_reasoning": 80.0}},
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
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"livebench_reasoning": 4000})
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
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"livebench_reasoning": 4000})

    result = _make_engine(means={"alpha": {"livebench_vision": 80.0}}).recommend(
        "reason about this problem"
    )

    assert result.eligible == ()
    assert result.excluded == ()
    assert [i.model_id for i in result.indeterminate] == ["alpha"]
    assert result.indeterminate[0].reasons == ("missing battery livebench_reasoning",)


def test_recommend_single_model_without_rival_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("hr.recommend._load_pricing", lambda: {"alpha": 1.0})
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"livebench_reasoning": 4000})

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
        means={"alpha": {"livebench_reasoning": 90.0}, "beta": {"livebench_reasoning": 80.0}},
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
    monkeypatch.setattr("hr.recommend._load_tokens_per_call", lambda: {"livebench_reasoning": 4000})
    engine = _make_engine(
        means={
            "alpha": {"livebench_reasoning": 90.0},
            "beta": {"livebench_reasoning": 80.0},
            "gamma": {"livebench_reasoning": 70.0},
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
        "evidence_errors",
    }
    assert payload["sweep_id"] == "sw-test"
    assert payload["batteries"] == ["livebench_reasoning"]
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
            batteries=("livebench_reasoning",),
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
        "hr.recommend._load_tokens_per_call", lambda: {"livebench_reasoning": 4000, "livebench_code_gen": 3000}
    )
    engine = _make_engine(
        means={"alpha": {"livebench_reasoning": 90.0}, "beta": {"livebench_reasoning": 75.0}},
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
        item.reasons == ("missing battery livebench_code_gen",)
        for item in result.indeterminate
    )


# ---------------------------------------------------------------------------
# A3 (ruling 2026-10-11 item 3): intervals are measured, clamped, and absent
# when the evidence is insufficient — never a fabricated constant band.
# ---------------------------------------------------------------------------


def test_measured_interval_clamps_and_respects_missing_evidence() -> None:
    lo, hi = _measured_interval(100.0, 0.5)
    assert hi == 100.0  # upper clamp on the 0-100 scale
    assert lo <= 100.0

    lo, hi = _measured_interval(1.0, 0.5)
    assert hi == 1.0  # upper clamp on the normalized scale
    assert lo <= 1.0 <= hi

    # no measured samples -> no band (the pre-0.6.4 code fabricated ±0.05)
    assert _measured_interval(80.0, None) is None
    assert _measured_interval(80.0, 0.0) is None

    # band width follows the measured dispersion, not a constant
    tight_lo, tight_hi = _measured_interval(80.0, 0.1)
    wide_lo, wide_hi = _measured_interval(80.0, 5.0)
    assert wide_hi - wide_lo > tight_hi - tight_lo
    assert (tight_hi - tight_lo) / 2.0 == pytest.approx(1.959963985 * 0.1, rel=1e-6)


def test_combined_std_error_requires_every_battery() -> None:
    batteries = ("livebench_code_gen", "livebench_reasoning")
    full = {
        "livebench_code_gen": (3.0, 13),
        "livebench_reasoning": (4.0, 12),
    }
    se = _combined_std_error(batteries, full)
    assert se is not None
    assert se == pytest.approx(
        ((0.5 * 3.0 / 13**0.5) ** 2 + (0.5 * 4.0 / 12**0.5) ** 2) ** 0.5
    )

    # one battery lacking >=2 items -> None (no partial-scale band)
    partial = {"livebench_code_gen": (3.0, 13), "livebench_reasoning": (None, 1)}
    assert _combined_std_error(batteries, partial) is None
    assert _combined_std_error(batteries, {}) is None


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


# ---------------------------------------------------------------------------
# Regression (defect #16, ruling 2026-10-11): fixtures seed ONLY production
# plane names. The pre-0.6.4 suite seeded bare names on BOTH sides and the
# shared wrong world-view kept 1575 tests green while `hr recommend --task`
# returned zero eligible models in production (191, live).
# ---------------------------------------------------------------------------


def test_canonical_battery_resolves_config_vocabulary_to_db_codes() -> None:
    assert canonical_battery("code_gen") == "livebench_code_gen"
    assert canonical_battery("tool_a") == "livebench_tool_use"
    assert canonical_battery("livebench_speed") == "livebench_speed"
    assert canonical_battery("mystery") == "mystery"


def test_task_router_matches_production_battery_codes() -> None:
    engine = _make_engine(
        means={"alpha": {"livebench_code_gen": 92.0, "livebench_reasoning": 88.0}},
    )

    ranked = engine.recommend_for_task("debug and analyze this code")

    assert ranked == [("alpha", pytest.approx(90.0))]


def test_fallback_router_set_is_canonical() -> None:
    engine = _make_engine()
    batteries = engine._match_batteries("absolutely nothing matches this qwerty")
    assert batteries == (
        "livebench_code_gen",
        "livebench_reasoning",
        "livebench_speed",
        "livebench_tool_use",
    )


def test_recommend_reports_missing_canonical_battery_honestly() -> None:
    engine = _make_engine(means={"alpha": {"livebench_speed": 99.0}})
    result = engine.recommend("debug this code")
    assert result.eligible == ()
    assert result.indeterminate[0].reasons == ("missing battery livebench_code_gen",)


def test_freshness_uses_oldest_required_capability() -> None:
    from hr.recommendation_constraints import FreshnessConstraint, RecommendationConstraints

    now = datetime.now(timezone.utc)
    stale = now - timedelta(days=60)
    engine = _make_engine(
        means={"alpha": {"livebench_code_gen": 95.0, "livebench_reasoning": 90.0}},
        sweep_created_at=now,
        profile_freshness={
            "alpha": {"livebench_code_gen": now, "livebench_reasoning": stale}
        },
    )
    policy = RecommendationConstraints(freshness=FreshnessConstraint(max_age_days=30))

    result = engine.recommend("debug this reasoning problem", constraints=policy)

    assert result.eligible == ()
    detail = result.excluded[0].reasons[0]
    assert "older than 30 days" in detail
    assert "[oldest required capability: livebench_reasoning]" in detail
    assert result.sweep_age_days is not None and result.sweep_age_days < 1.0

    narrow = engine.recommend("debug this code", constraints=policy)
    assert [item.model_id for item in narrow.eligible] == ["alpha"]


def test_interval_absent_without_measured_variance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "hr.recommend._load_pricing", lambda: {"alpha": 1.0, "beta": 1.0}
    )
    engine = _make_engine(
        means={
            "alpha": {"livebench_reasoning": 90.0},
            "beta": {"livebench_reasoning": 80.0},
        },
        health={
            model: HealthReport(
                model_id=model, sweep_id="sw-test", n_measurements=1
            )
            for model in ("alpha", "beta")
        },
        latency_stats={
            "alpha": {"p50": 500.0, "p95": 2000.0},
            "beta": {"p50": 500.0, "p95": 2000.0},
        },
        success_rates={"alpha": 1.0, "beta": 1.0},
        profile_stats={},
    )
    result = engine.recommend("reason about this problem")
    item = result.eligible[0]
    assert item.interval is None
    assert all("interval" not in caveat for caveat in item.caveats)


def test_read_evidence_failure_is_loud_not_silent(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    engine = _make_engine()

    def boom() -> dict[str, dict[str, float]]:
        raise RuntimeError("db gone")

    engine._get_latency_stats = boom
    with caplog.at_level(logging.WARNING, logger="hr.recommend"):
        result = engine.recommend("reason about this problem")

    assert any(
        "latency_stats" in error and "RuntimeError: db gone" in error
        for error in result.evidence_errors
    )
    assert any("latency_stats" in record.getMessage() for record in caplog.records)
    assert result.eligible == ()


def test_token_provenance_distinguishes_profile_from_fallback() -> None:
    tokens, basis = _estimate_task_tokens(
        ("livebench_code_gen",), {"livebench_code_gen": 6000}
    )
    assert tokens == 6000
    assert "max of profile [livebench_code_gen]" in basis

    tokens, basis = _estimate_task_tokens(("livebench_vision",), {})
    assert tokens == 5000
    assert basis.startswith("tokens 5000/request: generic fallback")


def test_tokens_per_call_config_resolves_legacy_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hr.recommend as recommend_module

    monkeypatch.setattr(
        recommend_module,
        "load_yaml",
        lambda name: {"tokens_per_call": {"code_gen": 6000, "tool_a": 1500}},
    )
    assert recommend_module._load_tokens_per_call() == {
        "livebench_code_gen": 6000,
        "livebench_tool_use": 1500,
    }
