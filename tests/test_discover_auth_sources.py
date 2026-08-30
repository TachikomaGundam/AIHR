from __future__ import annotations

from tests.test_discover import (
    _DiscoverConn,
    _Store,
    _setup_env,
    _write_global_config,
    app,
    json,
    runner,
    textwrap
)

class TestAuthAndProjectSources:
    """Auth presence marks + project config sources beyond the base fixture."""

    def test_inline_api_key_counts_as_auth_present(self, tmp_path, monkeypatch):
        """A provider with options.apiKey inline in opencode.jsonc is marked
        auth-present even with NO auth files at all."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        (config_dir / "opencode.jsonc").write_text(
            textwrap.dedent(
                """\
                {
                  "provider": {
                    "bailian-token-plan": {
                      "npm": "@ai-sdk/anthropic",
                      "options": {
                        "baseURL": "https://token-plan.example/v1",
                        "apiKey": "sk-inline-key"
                      },
                      "models": { "qwen3.7-max": { "name": "Qwen 3.7 Max" } }
                    }
                  }
                }
                """
            ),
            encoding="utf-8",
        )
        _setup_env(tmp_path, monkeypatch, config_dir=config_dir)
        store = _Store()
        monkeypatch.setattr("hr.cli.connect", lambda: _DiscoverConn(store))
        result = runner.invoke(app, ["discover"])
        assert result.exit_code == 0, result.output
        assert "bailian-token-plan/qwen3.7-max (in scope) (auth: yes)" in result.output

    def test_legacy_auth_json_fallback_marks_provider(self, tmp_path, monkeypatch):
        """auth-v2.json absent -> legacy auth.json top-level provider keys."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        _write_global_config(config_dir)
        auth_dir = tmp_path / ".local" / "share" / "opencode"
        auth_dir.mkdir(parents=True)
        (auth_dir / "auth.json").write_text(
            json.dumps({"kimi-for-coding": {"key": "sk-legacy"}}),
            encoding="utf-8",
        )
        _setup_env(tmp_path, monkeypatch, config_dir=config_dir)
        store = _Store()
        monkeypatch.setattr("hr.cli.connect", lambda: _DiscoverConn(store))
        result = runner.invoke(app, ["discover"])
        assert result.exit_code == 0, result.output
        assert "kimi-for-coding/kimi-k2.7-code (in scope) (auth: yes)" in result.output
        # bailian (only in auth-v2, which is absent) falls back to auth: no
        assert "bailian-token-plan/qwen3.7-max (in scope) (auth: no)" in result.output

    def test_project_opencode_dir_config_file_enumerated(self, tmp_path, monkeypatch):
        """<cwd>/.opencode/opencode.jsonc is a recognized project source."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        _write_global_config(config_dir)
        proj = tmp_path / "proj"
        (proj / ".opencode").mkdir(parents=True)
        (proj / ".opencode" / "opencode.jsonc").write_text(
            json.dumps(
                {
                    "provider": {
                        "deepseek": {
                            "npm": "@ai-sdk/openai-compatible",
                            "options": {"baseURL": "https://api.deepseek.com/v1"},
                            "models": {"deepseek-v4-flash": {"name": "DS V4"}},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        _setup_env(tmp_path, monkeypatch, config_dir=config_dir, cwd=proj)
        store = _Store()
        monkeypatch.setattr("hr.cli.connect", lambda: _DiscoverConn(store))
        # deepseek is discovered, so it IS in the default scope — the
        # project-source model enumerates like any other.
        result = runner.invoke(app, ["discover"])
        assert result.exit_code == 0, result.output
        assert "deepseek/deepseek-v4-flash (in scope)" in result.output
        assert "Discovered 4 model(s)" in result.output

    def test_enumerate_models_display_name_falls_back_to_model_id(
        self, tmp_path, monkeypatch
    ):
        """A model entry without a name renders the model_id as display name."""
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        (config_dir / "opencode.jsonc").write_text(
            json.dumps(
                {
                    "provider": {
                        "p1": {
                            "npm": "@ai-sdk/openai-compatible",
                            "options": {"apiKey": "k"},
                            "models": {"unnamed-model": {}},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        _setup_env(tmp_path, monkeypatch, config_dir=config_dir)
        from hr.discover import enumerate_models, scope_providers

        models = enumerate_models(scope_providers())
        assert len(models) == 1
        assert models[0].display_name == "unnamed-model"
        # p1 is discovered, so it joins the default scope automatically
        assert models[0].in_scope is True
        assert models[0].auth_present

    def test_enumerate_models_sorted_by_provider_then_model_id(
        self, tmp_path, monkeypatch
    ):
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        (config_dir / "opencode.jsonc").write_text(
            json.dumps(
                {
                    "provider": {
                        "zeta": {
                            "npm": "@ai-sdk/openai-compatible",
                            "options": {"apiKey": "k"},
                            "models": {
                                "m9": {"name": "M9"},
                                "m1": {"name": "M1"},
                            },
                        },
                        "alpha": {
                            "npm": "@ai-sdk/anthropic",
                            "options": {"apiKey": "k"},
                            "models": {"m2": {"name": "M2"}},
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        _setup_env(tmp_path, monkeypatch, config_dir=config_dir)
        from hr.discover import enumerate_models, scope_providers

        models = enumerate_models(scope_providers())
        assert [(m.provider, m.model_id) for m in models] == [
            ("alpha", "m2"),
            ("zeta", "m1"),
            ("zeta", "m9"),
        ]
