from __future__ import annotations

from tests.test_discover import discover_env  # noqa: F401 (pytest fixture re-export; resolved by parameter name)

from tests.test_discover import (
    _DiscoverConn,
    _Store,
    _setup_env,
    _write_global_config,
    _write_scope_excludes,
    app,
    json,
    pytest,
    runner,
    scope_providers
)

class TestDiscoverFixture:
    def test_prints_exactly_three_with_scope_annotation(self, discover_env, monkeypatch):  # noqa: F811 (fixture param shadows re-export)
        store = _Store()
        monkeypatch.setattr("hr.cli.connect", lambda: _DiscoverConn(store))
        result = runner.invoke(app, ["discover"])
        assert result.exit_code == 0, result.output
        out = result.output
        for model in (
            "bailian-token-plan/qwen3.7-max",
            "bailian-token-plan/deepseek-v4-flash",
            "kimi-for-coding/kimi-k2.7-code",
        ):
            assert model in out, f"missing {model} in:\n{out}"
        assert out.count("(in scope)") == 3
        assert "bailian-token-plan/qwen3.7-max (in scope) (auth: yes)" in out
        assert "kimi-for-coding/kimi-k2.7-code (in scope) (auth: no)" in out
        assert "Discovered 3 model(s)" in out
        # upserted exactly the 3 models under their 2 providers (full ids)
        assert set(store.providers) == {"bailian-token-plan", "kimi-for-coding"}
        assert set(store.models) == {
            "bailian-token-plan/qwen3.7-max",
            "bailian-token-plan/deepseek-v4-flash",
            "kimi-for-coding/kimi-k2.7-code",
        }

    def test_upsert_idempotent_row_counts_unchanged(self, discover_env, monkeypatch):  # noqa: F811 (fixture param shadows re-export)
        store = _Store()
        monkeypatch.setattr("hr.cli.connect", lambda: _DiscoverConn(store))
        first = runner.invoke(app, ["discover"])
        assert first.exit_code == 0, first.output
        assert "upserted 2 provider(s), 3 model row(s) into hr" in first.output
        second = runner.invoke(app, ["discover"])
        assert second.exit_code == 0, second.output
        # same rows, zero new inserts — store unchanged
        assert len(store.providers) == 2 and len(store.models) == 3
        assert "upserted 0 provider(s), 0 model row(s) into hr" in second.output
        assert "Discovered 3 model(s)" in second.output

    def test_all_includes_out_of_scope_provider_marked(self, tmp_path, monkeypatch):
        """Global (in-scope bailian + kimi) + PROJECT opencode.jsonc with an
        excluded local-qwen provider: default filters it (scope_excludes
        override), --all shows it marked as such."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        _write_global_config(config_dir)
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "opencode.jsonc").write_text(
            json.dumps(
                {
                    "provider": {
                        "local-qwen": {
                            "npm": "@ai-sdk/openai-compatible",
                            "options": {"baseURL": "http://127.0.0.1:8999/v1"},
                            "models": {"qwen3.6-8b": {"name": "Qwen 3.6 8B"}},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        _write_scope_excludes(tmp_path / "hr", ["local-qwen"])
        _setup_env(tmp_path, monkeypatch, config_dir=config_dir, cwd=proj)
        store = _Store()
        monkeypatch.setattr("hr.cli.connect", lambda: _DiscoverConn(store))

        default = runner.invoke(app, ["discover"])
        assert default.exit_code == 0, default.output
        assert "local-qwen" not in default.output
        assert default.output.count("(in scope)") == 3
        assert "Discovered 3 model(s)" in default.output

        all_run = runner.invoke(app, ["discover", "--all"])
        assert all_run.exit_code == 0, all_run.output
        assert "local-qwen/qwen3.6-8b (out of scope)" in all_run.output
        assert all_run.output.count("(out of scope)") == 1
        assert all_run.output.count("(in scope)") == 3
        assert "Discovered 4 model(s)" in all_run.output

    def test_malformed_opencode_jsonc_fails_naming_file(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        (config_dir / "opencode.jsonc").write_text(
            '{\n  "provider": { // truncated', encoding="utf-8"
        )
        _setup_env(tmp_path, monkeypatch, config_dir=config_dir)

        def _explode():
            raise AssertionError("connect() must not run when the parse fails")

        monkeypatch.setattr("hr.cli.connect", _explode)
        result = runner.invoke(app, ["discover"])
        assert result.exit_code == 1
        assert "error: invalid opencode config" in result.output
        assert "opencode.jsonc" in result.output
        assert "Traceback" not in result.output

    def test_help_documents_static_parse_limitation(self):
        # COLUMNS/NO_COLOR pin the rich panel width so flag rows never wrap.
        result = runner.invoke(
            app, ["discover", "--help"], env={"COLUMNS": "200", "NO_COLOR": "1"}
        )
        assert result.exit_code == 0, result.output
        assert "static config parse only" in result.output
        assert "--all" in result.output
        assert "scope_excludes" in result.output

class TestScopeProviders:
    def test_default_scope_is_all_discovered_providers(self, tmp_path, monkeypatch):
        """No overrides file -> every discovered provider is in scope."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        _write_global_config(config_dir)
        _setup_env(tmp_path, monkeypatch, config_dir=config_dir)
        assert scope_providers() == frozenset({"bailian-token-plan", "kimi-for-coding"})

    def test_scope_excludes_remove_providers(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        _write_global_config(config_dir)
        _write_scope_excludes(tmp_path / "hr", ["kimi-for-coding"])
        _setup_env(tmp_path, monkeypatch, config_dir=config_dir)
        assert scope_providers() == frozenset({"bailian-token-plan"})

    def test_discovered_provider_without_models_contributes_nothing(
        self, tmp_path, monkeypatch
    ):
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        _write_global_config(config_dir)
        (config_dir / "opencode.jsonc").write_text(
            json.dumps(
                {
                    "provider": {
                        "bailian-token-plan": {
                            "npm": "@ai-sdk/anthropic",
                            "options": {"apiKey": "sk-x"},
                            "models": {"qwen3.7-max": {}},
                        },
                        "auth-stub": {"npm": "@ai-sdk/anthropic"},
                    }
                }
            ),
            encoding="utf-8",
        )
        _setup_env(tmp_path, monkeypatch, config_dir=config_dir)
        assert scope_providers() == frozenset({"bailian-token-plan"})

    def test_corrupt_overrides_file_raises_naming_file(self, tmp_path, monkeypatch):
        rhome = tmp_path / "hr"
        (rhome / "configs").mkdir(parents=True)
        (rhome / "configs" / "fleet.yaml").write_text(
            "wire_overrides: [unclosed\n", encoding="utf-8"
        )
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        _write_global_config(config_dir)
        _setup_env(tmp_path, monkeypatch, config_dir=config_dir)
        with pytest.raises(ValueError, match="fleet.yaml"):
            scope_providers()
