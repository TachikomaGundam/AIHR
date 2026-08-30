"""hr apply — verdict → FastDraw preset/state bridge tests (hr-unification, todo 18).

Offline: duck-typed fake connection (cursor with description/fetchall, test_cli
style) routes each SQL by a substring key; the config dir is redirected with
the OPENCODE_CONFIG_DIR env override so nothing touches ~/.config/opencode.

The full-chain dispatch test (test_apply_dispatch_) exercises the whole
pipeline — latest sweep → capability means → health reports → ranker →
seat_assignments → preset JSON — with only the SQL layer faked. FastDraw
sources are never imported: these tests assert the JSON file contract shapes
the plugin parses (presets store, isModelMap "/" rule, boot-time state file).
"""

from __future__ import annotations

import json
import re
from datetime import date

import pytest
from typer.testing import CliRunner

from hr import apply as apply_mod
from hr.apply import (
    PRESETS_FILENAME,
    STATE_FILENAME,
    agents_from_assignments,
    apply,
    validate_agents,
    write_preset,
    write_state,
)
from hr.cli import app

runner = CliRunner()

# One model with data on all five batteries the seat knobs map onto.
_MODEL = "bailian-token-plan/deepseek-v4-flash"

_MEASUREMENT_SQL_KEY = "SELECT m.item_id, m.score"
_DISTINCT_MODEL_SQL_KEY = "SELECT DISTINCT r.model_id"
_COUNT_SQL_KEY = "SELECT COUNT(m.measurement_id)::int"
_AVG_SQL_KEY = "AVG(m.score)"
_BREAKDOWN_SQL_KEY = "AS mean_score"  # battery-breakdown SQL also contains "AVG(m.score)"
_SEAT_SQL_KEY = "FROM hr.seat"
_MODEL_SQL_KEY = "FROM hr.model"
_BATTERY_SQL_KEY = "battery_code FROM hr.battery"
_LATEST_SQL_KEY = "SELECT s.sweep_id\n"

_BATTERIES = [
    "reasoning",
    "tool_a",
    "hallucination",
    "livebench_long_context",
    "livebench_speed",
]


class _KeyedCursor:
    """Cursor routing fetchall() by the first substring key match (test_cli pattern)."""

    def __init__(self, router):
        self._router = router

    def execute(self, sql, params=None):
        self._match = []
        for key, rows in self._router.items():
            if key in sql:
                self._match = rows
                if key == _MEASUREMENT_SQL_KEY:
                    self.description = [
                        ("item_id",), ("score",), ("tokens_out",), ("response_text",),
                    ]
                else:
                    self.description = [("c",)]
                return
        self.description = [("c",)]

    def fetchall(self):
        return self._match

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class _KeyedConn:
    """Fake connection; unmatched SQL falls back to an empty result set."""

    def __init__(self, router):
        self._router = dict(router)
        self._router.setdefault("", [])

    def cursor(self):
        return _KeyedCursor(self._router)

    def close(self):
        pass


def _router(means=True) -> dict:
    """Router for one sweep s1 with one healthy model on all five batteries."""
    router = {
        _BREAKDOWN_SQL_KEY: [],
        _AVG_SQL_KEY: [
            (_MODEL, "reasoning", 0.9),
            (_MODEL, "tool_a", 0.8),
            (_MODEL, "hallucination", 0.7),
            (_MODEL, "livebench_long_context", 0.6),
            (_MODEL, "livebench_speed", 0.5),
        ],
        _DISTINCT_MODEL_SQL_KEY: [(_MODEL,)],
        _MEASUREMENT_SQL_KEY: [("i1", 0.9, 500, "The answer is 42.")],
        _COUNT_SQL_KEY: [(1,)],
        _SEAT_SQL_KEY: [("oracle", [], None)],
        _MODEL_SQL_KEY: [],
        _BATTERY_SQL_KEY: [(b,) for b in _BATTERIES],
        _LATEST_SQL_KEY: [("s1",)],
    }
    if not means:
        router[_AVG_SQL_KEY] = []
    return router


def _today_name() -> str:
    return f"verdict-{date.today().isoformat()}"


def _entry(store: dict, name: str) -> dict:
    return store["presets"][name]


class TestApplyFullChain:
    """CLI dispatch through the real pipeline over a faked SQL layer."""

    def test_dispatch_writes_contract_preset(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr("hr.cli.connect", lambda: _KeyedConn(_router()))
        monkeypatch.setattr("hr.apply.load_deployable", lambda: {_MODEL})

        result = runner.invoke(app, ["apply"])

        assert result.exit_code == 0, result.output
        preset_path = tmp_path / PRESETS_FILENAME
        assert preset_path.exists()
        store = json.loads(preset_path.read_text())
        assert set(store) == {"presets"}  # contract shape: only the presets key
        name = _today_name()
        assert set(_entry(store, name)) == {"description", "createdAt", "agents"}
        assert _entry(store, name)["description"] == "hr verdict seating from sweep s1"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z",
                            _entry(store, name)["createdAt"])
        agents = _entry(store, name)["agents"]
        assert agents["oracle"] == _MODEL
        # FastDraw isModelMap rule: every agents value MUST contain "/".
        for agent, model in agents.items():
            assert re.fullmatch(r".+/.+", model), f"{agent}: {model!r} lacks /"
        # Underscore seat codes → hyphen runtime agent names.
        assert "visual-engineering" in agents
        assert "unspecified-high" in agents
        assert not (tmp_path / STATE_FILENAME).exists()  # no --set-state
        assert "preset" in result.output
        # rich Console wraps long paths mid-word; assert on the unwrappable parts.
        assert isinstance(result.output, str) and "fastdraw-presets" in result.output
        assert "18 agent" in result.output

    def test_dispatch_set_state_parity(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr("hr.cli.connect", lambda: _KeyedConn(_router()))
        monkeypatch.setattr("hr.apply.load_deployable", lambda: {_MODEL})

        result = runner.invoke(app, ["apply", "--set-state"])

        assert result.exit_code == 0, result.output
        store = json.loads((tmp_path / PRESETS_FILENAME).read_text())
        state = json.loads((tmp_path / STATE_FILENAME).read_text())
        assert state == {"agents": _entry(store, _today_name())["agents"]}
        assert ".fastdraw.json" in result.output
        assert "restart" in result.output.lower()

    def test_dispatch_custom_preset_name(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr("hr.cli.connect", lambda: _KeyedConn(_router()))
        monkeypatch.setattr("hr.apply.load_deployable", lambda: {_MODEL})

        result = runner.invoke(app, ["apply", "--preset", "prod-lock"])

        assert result.exit_code == 0, result.output
        store = json.loads((tmp_path / PRESETS_FILENAME).read_text())
        assert set(store["presets"]) == {"prod-lock"}

    def test_empty_verdict_refuses_and_writes_nothing(self, monkeypatch, tmp_path):
        """No capability means → no seating → non-zero refusal, no files."""
        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr("hr.cli.connect", lambda: _KeyedConn(_router(means=False)))
        monkeypatch.setattr("hr.apply.load_deployable", lambda: {_MODEL})

        result = runner.invoke(app, ["apply"])

        assert result.exit_code == 1
        assert "no verdict seating" in result.output
        assert not (tmp_path / PRESETS_FILENAME).exists()
        assert not (tmp_path / STATE_FILENAME).exists()

    def test_no_sweeps_refuses(self, monkeypatch, tmp_path):
        """No latest sweep at all → refusal naming the cause."""
        router = _router()
        del router[_LATEST_SQL_KEY]
        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr("hr.cli.connect", lambda: _KeyedConn(router))
        monkeypatch.setattr("hr.apply.load_deployable", lambda: {_MODEL})

        result = runner.invoke(app, ["apply"])

        assert result.exit_code == 1
        assert "no sweeps" in result.output
        assert not (tmp_path / PRESETS_FILENAME).exists()


class TestOverwriteSemantics:
    def test_rerun_same_name_replaces_entry_keeps_others(self, tmp_path):
        presets_path = tmp_path / PRESETS_FILENAME
        presets_path.write_text(json.dumps({
            "presets": {
                "old-a": {"description": "a", "createdAt": "2026-01-01T00:00:00.000Z",
                          "agents": {"x": "p/m1"}},
                "old-b": {"description": "b", "createdAt": "2026-01-01T00:00:00.000Z",
                          "agents": {"y": "p/m2"}},
            }
        }, indent=2) + "\n")
        before = presets_path.read_text()

        apply(_KeyedConn(_router()), preset_name="verdict-test",
              config_dir=tmp_path, deployable={_MODEL})
        store1 = json.loads(presets_path.read_text())
        assert "old-a" in store1["presets"] and "old-b" in store1["presets"]
        old_a_first = json.dumps(store1["presets"]["old-a"], sort_keys=True)

        apply(_KeyedConn(_router()), preset_name="verdict-test",
              config_dir=tmp_path, deployable={_MODEL})
        store2 = json.loads(presets_path.read_text())
        # Same name rerun replaces only its own entry; others byte-identical.
        assert set(store2["presets"]) == {"old-a", "old-b", "verdict-test"}
        assert json.dumps(store2["presets"]["old-a"], sort_keys=True) == old_a_first
        assert json.dumps(store2["presets"]["old-b"], sort_keys=True) == \
            json.dumps(json.loads(before)["presets"]["old-b"], sort_keys=True)

    def test_corrupt_presets_file_refuses(self, tmp_path):
        (tmp_path / PRESETS_FILENAME).write_text("{not json")
        with pytest.raises(RuntimeError, match="refusing"):
            apply(_KeyedConn(_router()), config_dir=tmp_path, deployable={_MODEL})
        # the corrupt file is untouched
        assert (tmp_path / PRESETS_FILENAME).read_text() == "{not json"

    def test_wrong_shape_presets_file_refuses(self, tmp_path):
        (tmp_path / PRESETS_FILENAME).write_text(json.dumps({"agents": {}}))
        with pytest.raises(RuntimeError, match="refusing"):
            apply(_KeyedConn(_router()), config_dir=tmp_path, deployable={_MODEL})


class TestContractRules:
    def test_agents_from_assignments_skips_unassigned_and_normalizes_names(self):
        assignments = [
            {"seat_code": "visual_engineering", "primary": "p/m1"},
            {"seat_code": "unspecified_high", "primary": "p/m2"},
            {"seat_code": "sisyphus_junior", "primary": None},  # no seating
            {"seat_code": "oracle", "primary": "p/m3"},
        ]
        assert agents_from_assignments(assignments) == {
            "visual-engineering": "p/m1",
            "unspecified-high": "p/m2",
            "oracle": "p/m3",
        }

    def test_validate_agents_refuses_empty(self):
        with pytest.raises(RuntimeError, match="no verdict seating"):
            validate_agents({})

    def test_validate_agents_refuses_value_without_slash(self):
        with pytest.raises(RuntimeError, match="provider/model"):
            validate_agents({"oracle": "deepseek-v4-flash"})

    def test_validate_agents_accepts_provider_model_values(self):
        validate_agents({"oracle": "bailian-token-plan/deepseek-v4-flash", "deep": "kimi/k3"})

    def test_set_state_parity_engine_level(self, tmp_path):
        summary = apply(_KeyedConn(_router()), set_state=True,
                        preset_name="verdict-state", config_dir=tmp_path,
                        deployable={_MODEL})
        store = json.loads((tmp_path / PRESETS_FILENAME).read_text())
        state = json.loads((tmp_path / STATE_FILENAME).read_text())
        assert state == {"agents": _entry(store, "verdict-state")["agents"]}
        assert "restart" in summary.lower()


class TestNeverClobberWithEmptyData:
    """An empty verdict must refuse WITHOUT touching an existing presets
    file — presets are never clobbered with empty data (contract, todo 18)."""

    def test_empty_verdict_preserves_existing_presets_file(self, monkeypatch, tmp_path):
        presets_path = tmp_path / PRESETS_FILENAME
        payload = json.dumps(
            {"presets": {"prod-lock": {"description": "locked", "createdAt": "x",
                                       "agents": {"oracle": "p/m1"}}}},
            indent=2,
        ) + "\n"
        presets_path.write_text(payload, encoding="utf-8")
        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr("hr.cli.connect", lambda: _KeyedConn(_router(means=False)))
        monkeypatch.setattr("hr.apply.load_deployable", lambda: {_MODEL})

        result = runner.invoke(app, ["apply"])

        assert result.exit_code == 1
        assert "no verdict seating" in result.output
        # byte-identical: the failed run never wrote the store
        assert presets_path.read_text() == payload

    def test_validate_agents_refuses_when_one_binding_lacks_slash(self):
        # mixed map: one valid, one invalid -> the whole seating is refused
        with pytest.raises(RuntimeError, match="provider/model"):
            validate_agents({"oracle": "p/m1", "deep": "deepseek-v4-flash"})


class TestFileContractShape:
    """The written files satisfy FastDraw's contract: presets store shape,
    isModelMap "/" rule, JSON.stringify(store, null, 2) formatting."""

    def test_write_preset_creates_nested_dir_and_exact_shape(self, tmp_path):
        cfg = tmp_path / "nested" / "config"
        agents = {"oracle": "p/m1", "visual-engineering": "p/m2"}
        path = write_preset(
            agents, "prod-lock", cfg,
            description="desc", created_at="2026-01-01T00:00:00.000Z",
        )
        assert path == cfg / PRESETS_FILENAME
        assert path.parent.is_dir()
        store = json.loads(path.read_text(encoding="utf-8"))
        assert set(store) == {"presets"}  # contract: only the presets key
        entry = store["presets"]["prod-lock"]
        assert set(entry) == {"description", "createdAt", "agents"}
        assert entry["description"] == "desc"
        assert entry["createdAt"] == "2026-01-01T00:00:00.000Z"
        assert entry["agents"] == agents
        # isModelMap rule at the FILE level: every agents value contains "/"
        for agent, model in entry["agents"].items():
            assert re.fullmatch(r".+/.+", model), f"{agent}: {model!r} lacks /"
        # JSON.stringify(store, null, 2) parity: 2-space indent + trailing NL
        text = path.read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert '\n  "presets"' in text
        assert '\n    "prod-lock"' in text

    def test_write_state_exact_shape(self, tmp_path):
        agents = {"oracle": "p/m1", "visual-engineering": "p/m2"}
        path = write_state(agents, tmp_path)
        assert path == tmp_path / STATE_FILENAME
        state = json.loads(path.read_text(encoding="utf-8"))
        assert set(state) == {"agents"}  # contract: only the agents key
        assert state["agents"] == agents
        text = path.read_text(encoding="utf-8")
        assert text.endswith("\n") and '\n  "agents"' in text