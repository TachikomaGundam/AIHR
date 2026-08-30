"""Knowledge-store CLI command contract tests (committed surface).

Exercises the reference/research/publish/recommend commands registered
in hr.cli_knowledge against typer's app, with the underlying stores and
recommendation engine faked. Offline and deterministic.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from hr.cli_knowledge import app


class _FakeEngine:
    def __init__(self, *, result=None, seats_output="seats!"):
        self.result = result or _FakeResult()
        self.seats_output = seats_output
        self.closed = False
        self._conn = self

    def recommend(self, task: str):  # noqa: ARG002
        return self.result

    def seat_recommendations(self, seats):  # noqa: ARG002
        return self.seats_output

    def close(self) -> None:
        self.closed = True


class _FakeResult:
    """Empty tri-state result shaped like ``hr.recommend.RecommendationResult``."""

    task = None
    batteries = ()
    sweep_id = None
    sweep_age_days = None
    eligible = []
    excluded = []
    indeterminate = []


runner = CliRunner()


def test_reference_summary_and_model(monkeypatch) -> None:
    monkeypatch.setattr(
        "hr.reference.load_reference_scores",
        lambda: {"m1": {"code": (95.0, 0.9, "bench")}},
    )
    result = runner.invoke(app, ["reference"])
    assert result.exit_code == 0
    assert "Reference store: 1 models" in result.output
    assert "m1" in result.output

    result = runner.invoke(app, ["reference", "m1"])
    assert result.exit_code == 0
    assert "95.0 (confidence 0.90)" in result.output


def test_reference_unknown_model_fails(monkeypatch) -> None:
    monkeypatch.setattr("hr.reference.load_reference_scores", lambda: {})
    result = runner.invoke(app, ["reference", "nope"])
    assert result.exit_code == 1
    assert "error: 'nope' not in the reference store" in result.stderr


def test_research_summary_and_model(monkeypatch) -> None:
    monkeypatch.setattr(
        "hr.research.load_findings",
        lambda: {"m1": [("reasoning", "strong", 0.8, "src")]},
    )
    result = runner.invoke(app, ["research", "m1"])
    assert result.exit_code == 0
    assert "strong" in result.output

    result = runner.invoke(app, ["research"])
    assert result.exit_code == 0
    assert "findings (reasoning)" in result.output


def test_research_unknown_model_fails(monkeypatch) -> None:
    monkeypatch.setattr("hr.research.load_findings", lambda: {})
    result = runner.invoke(app, ["research", "nope"])
    assert result.exit_code == 1
    assert "error: 'nope' not in the research store" in result.stderr


def test_publish_skips_when_wiki_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr("hr.publish.wiki_target", lambda: None)
    result = runner.invoke(app, ["publish"])
    assert result.exit_code == 0
    assert "wiki not configured, skipping" in result.output


def test_publish_success(monkeypatch) -> None:
    monkeypatch.setattr("hr.publish.wiki_target", lambda: {"graphql_url": "x"})
    monkeypatch.setattr("hr.publish.publish_from_target", lambda target: None)
    result = runner.invoke(app, ["publish"])
    assert result.exit_code == 0
    assert "Published to Wiki.js" in result.output


def test_publish_runtime_error_fails_cleanly(monkeypatch) -> None:
    monkeypatch.setattr("hr.publish.wiki_target", lambda: {"graphql_url": "x"})

    def _boom(_target):
        raise RuntimeError("graphql exploded")

    monkeypatch.setattr("hr.publish.publish_from_target", _boom)
    result = runner.invoke(app, ["publish"])
    assert result.exit_code == 1
    assert "error: graphql exploded" in result.stderr


def test_recommend_task_formats_result(monkeypatch) -> None:
    engine = _FakeEngine(result=_FakeResult())

    class _FakeAnonResult(_FakeResult):
        eligible = [{"model": "m1"}]
        excluded = []
        indeterminate = []

    engine.result = _FakeAnonResult()
    monkeypatch.setattr("hr.recommend.RecommendationEngine", lambda: engine)
    monkeypatch.setattr(
        "hr.recommend.format_recommendation_result",
        lambda result, fmt: f"formatted-{fmt}",
    )
    result = runner.invoke(app, ["recommend", "--task", "write code"])
    assert result.exit_code == 0
    assert "formatted-table" in result.output
    assert engine.closed


def test_recommend_task_empty_result(monkeypatch) -> None:
    engine = _FakeEngine(result=_FakeResult())
    monkeypatch.setattr("hr.recommend.RecommendationEngine", lambda: engine)
    result = runner.invoke(app, ["recommend", "--task", "write code"])
    assert result.exit_code == 0
    assert "(no recommendations returned)" in result.output


def test_recommend_task_empty_result_json(monkeypatch) -> None:
    engine = _FakeEngine(result=_FakeResult())
    monkeypatch.setattr("hr.recommend.RecommendationEngine", lambda: engine)
    result = runner.invoke(app, ["recommend", "--task", "write code", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["eligible"] == []
    assert payload["excluded"] == []
    assert payload["indeterminate"] == []


def test_recommend_task_json_flag(monkeypatch) -> None:
    engine = _FakeEngine(result=_FakeResult())

    class _AnonResult(_FakeResult):
        eligible = [{"model": "m1"}]

    engine.result = _AnonResult()
    monkeypatch.setattr("hr.recommend.RecommendationEngine", lambda: engine)
    monkeypatch.setattr(
        "hr.recommend.format_recommendation_result",
        lambda result, fmt: f"json-out-{fmt}",
    )
    result = runner.invoke(app, ["recommend", "--task", "t", "--json"])
    assert result.exit_code == 0
    assert "json-out-json" in result.output


def test_recommend_no_task_prints_seats(monkeypatch) -> None:
    engine = _FakeEngine(seats_output="seat table")
    monkeypatch.setattr("hr.recommend.load_seat_specs", lambda: {"oracle": {}})
    monkeypatch.setattr("hr.recommend.RecommendationEngine", lambda: engine)
    result = runner.invoke(app, ["recommend"])
    assert result.exit_code == 0
    assert "seat table" in result.output
    assert engine.closed


def test_recommend_engine_construction_fails(monkeypatch) -> None:
    def _boom():
        raise RuntimeError("cannot load seats")

    monkeypatch.setattr("hr.recommend.RecommendationEngine", _boom)
    result = runner.invoke(app, ["recommend", "--task", "t"])
    assert result.exit_code == 1
    assert "error: cannot load seats" in result.stderr