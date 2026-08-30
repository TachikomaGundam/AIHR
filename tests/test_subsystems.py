"""Offline tests for the ported v1 subsystems (task 14): blend math, seat
specs from configs/seats.yaml, knowledge-merge single store, wiki publish
skip, and CliRunner smoke for the 4 commands.

No live DB and no network: the recommend command path uses an empty fake
connection (same duck-typing approach as tests/test_cli.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from hr.cli import app
from hr.config import wiki_config
from hr.recommend import (
    _blend_value,
    _REFERENCE_PRIOR,
    _seat_capability_weights,
    _seat_gates_ok,
    load_seat_specs,
)

runner = CliRunner()
_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# blend math
# ---------------------------------------------------------------------------


class TestBlendValue:
    def test_prior_constant_is_70(self):
        assert _REFERENCE_PRIOR == 70.0

    def test_min_caps_at_live_when_live_beats_eff_ref(self):
        # live 80 > eff_ref 75 -> capped at 75
        assert _blend_value(80.0, (75.0, 1.0)) == 75.0

    def test_live_below_eff_ref_wins(self):
        assert _blend_value(60.0, (95.0, 1.0)) == 60.0

    def test_zero_confidence_uses_prior(self):
        # eff_ref = 0*ref + 1*70 = 70, min(80, 70) = 70
        assert _blend_value(80.0, (95.0, 0.0)) == 70.0

    def test_half_confidence_interpolates(self):
        # eff_ref = 0.5*90 + 0.5*70 = 80, min(None-live) -> 80
        assert _blend_value(None, (90.0, 0.5)) == 80.0

    def test_no_reference_uses_live_alone(self):
        assert _blend_value(80.0, None) == 80.0

    def test_neither_live_nor_reference_is_zero(self):
        assert _blend_value(None, None) == 0.0

    def test_live_zero_forces_zero(self):
        # a live 0 is a hard floor regardless of the reference
        assert _blend_value(0.0, (95.0, 1.0)) == 0.0


# ---------------------------------------------------------------------------
# seat specs — configs/seats.yaml ONLY
# ---------------------------------------------------------------------------


class TestSeatSpecs:
    def test_seats_yaml_has_18_seats(self):
        seats = load_seat_specs()
        assert len(seats) == 18

    def test_seat_fields_present(self):
        for seat in load_seat_specs():
            assert seat["seat_code"]
            assert "domain" in seat
            assert "cost_tier" in seat
            assert "ctx_p95" in seat
            assert isinstance(seat.get("required_capabilities"), list)

    def test_missing_seats_yaml_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HR_HOME", str(tmp_path))
        assert load_seat_specs() == []

    def test_corrupt_seats_yaml_raises_naming_file(self, tmp_path, monkeypatch):
        configs = tmp_path / "configs"
        configs.mkdir()
        (configs / "seats.yaml").write_text("seats: [unclosed\n", encoding="utf-8")
        monkeypatch.setenv("HR_HOME", str(tmp_path))
        with pytest.raises(ValueError) as exc:
            load_seat_specs()
        assert "seats.yaml" in str(exc.value)
        assert str(configs / "seats.yaml") in str(exc.value)

    def test_weights_derive_from_role_requirements_or_domain(self):
        oracle = {"seat_code": "oracle", "domain": "reasoning"}
        weights = _seat_capability_weights(oracle)
        assert weights == {"reasoning": 1.0}
        unknown = {"seat_code": "no_such_seat", "domain": "vision"}
        assert _seat_capability_weights(unknown) == {"vision": 1.0}

    def test_gates_from_seats_yaml(self):
        assert _seat_gates_ok({"required_capabilities": ["vision"]}, {"vision": 10.0})
        assert not _seat_gates_ok({"required_capabilities": ["vision"]}, {"vision": 0.0})
        assert _seat_gates_ok({"required_capabilities": ["tools"]}, {"tool_use": 5.0})
        assert _seat_gates_ok({"required_capabilities": []}, {})


# ---------------------------------------------------------------------------
# knowledge merge — single store
# ---------------------------------------------------------------------------


class TestKnowledgeMerge:
    def test_reference_and_research_are_distinct_views_of_one_store(self):
        from hr.reference import load_reference_scores
        from hr.research import load_findings

        # Given: the single configured knowledge store.
        references = load_reference_scores()
        findings = load_findings()

        # When/Then: each parser exposes its own typed view without DB seeding.
        assert references
        assert findings
        assert all(len(score) == 3 for categories in references.values() for score in categories.values())
        assert all(len(finding) == 4 for entries in findings.values() for finding in entries)


# ---------------------------------------------------------------------------
# publish skip (missing [wiki] config)
# ---------------------------------------------------------------------------


class TestPublishSkip:
    def test_publish_without_wiki_config_skips_cleanly(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HR_HOME", str(tmp_path))
        result = runner.invoke(app, ["publish"])
        assert result.exit_code == 0
        assert "wiki not configured" in result.output
        assert "skipping" in result.output

    def test_wiki_config_absent_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HR_HOME", str(tmp_path))
        assert wiki_config() is None

    def test_wiki_config_present_returns_section(self, tmp_path, monkeypatch):
        (tmp_path / "hr.toml").write_text(
            '[wiki]\ngraphql_url = "http://wiki.example/graphql"\n'
            'api_key_file = "/tmp/key"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("HR_HOME", str(tmp_path))
        assert wiki_config() == {
            "graphql_url": "http://wiki.example/graphql",
            "api_key_file": "/tmp/key",
        }

    def test_wiki_config_empty_section_returns_none(self, tmp_path, monkeypatch):
        (tmp_path / "hr.toml").write_text("[wiki]\n", encoding="utf-8")
        monkeypatch.setenv("HR_HOME", str(tmp_path))
        assert wiki_config() is None


# ---------------------------------------------------------------------------
# command smoke (typer CliRunner)
# ---------------------------------------------------------------------------

class _EmptyCursor:
    def __init__(self):
        self.description = []

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class _EmptyConn:
    def cursor(self, cursor_factory=None):
        return _EmptyCursor()

    def close(self):
        pass


class TestCommandSmoke:
    @pytest.mark.parametrize(
        "name", ["reference", "research", "publish", "recommend"],
    )
    def test_help_responds_exit_zero(self, name):
        result = runner.invoke(app, [name, "--help"])
        assert result.exit_code == 0
        assert name in result.output

    def test_reference_query_runs_offline(self):
        result = runner.invoke(app, ["reference"])
        assert result.exit_code == 0
        assert "Reference store" in result.output
        assert "models" in result.output

    def test_reference_unknown_model_fails_cleanly(self):
        result = runner.invoke(app, ["reference", "no-such-model"])
        assert result.exit_code != 0
        assert "not in the reference store" in result.output

    def test_research_query_runs_offline(self):
        result = runner.invoke(app, ["research"])
        assert result.exit_code == 0
        assert "Research store" in result.output
        assert "findings" in result.output

    def test_recommend_18_seats_flow_into_output(self, monkeypatch):
        monkeypatch.setattr("hr.recommend.get_connection", lambda: _EmptyConn())
        result = runner.invoke(app, ["recommend"])
        assert result.exit_code == 0
        seats = load_seat_specs()
        assert len(seats) == 18
        for seat in seats:
            assert f"| {seat['seat_code']} " in result.output
        assert "Capability prior" in result.output

    def test_recommend_task_smoke(self, monkeypatch):
        monkeypatch.setattr("hr.recommend.get_connection", lambda: _EmptyConn())
        result = runner.invoke(app, ["recommend", "--task", "write a unit test"])
        assert result.exit_code == 0
        assert "no recommendations returned" in result.output

    def test_recommend_corrupt_seats_yaml_fails_naming_file(self, tmp_path, monkeypatch):
        configs = tmp_path / "configs"
        configs.mkdir()
        (configs / "seats.yaml").write_text("seats: [unclosed\n", encoding="utf-8")
        monkeypatch.setenv("HR_HOME", str(tmp_path))
        result = runner.invoke(app, ["recommend"])
        assert result.exit_code != 0
        assert "seats.yaml" in result.output
        assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# engine-level seat recommendations (offline, empty fake conn)
# ---------------------------------------------------------------------------


class TestSeatRecommendations:
    def test_engine_picks_best_model_per_seat(self, monkeypatch):
        from hr.recommend import RecommendationEngine

        monkeypatch.setattr("hr.recommend.get_connection", lambda: _EmptyConn())
        engine = RecommendationEngine()
        try:
            seats = load_seat_specs()
            report = engine.seat_recommendations(seats)
        finally:
            engine.close()
        assert len(seats) == 18
        for seat in seats:
            assert f"| {seat['seat_code']} " in report
        assert "docs/en/capability-prior.md" in report
