from __future__ import annotations

from tests.test_apply import (
    PRESETS_FILENAME,
    STATE_FILENAME,
    _KeyedConn,
    _LATEST_SQL_KEY,
    _MODEL,
    _entry,
    _router,
    _today_name,
    agents_from_assignments,
    app,
    apply,
    json,
    pytest,
    re,
    runner,
    validate_agents
)

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
