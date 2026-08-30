"""Offline fixture tests for ``hr discover`` (static opencode.jsonc enumeration).

None of these touch the live database: a fake connection simulates the
``ON CONFLICT DO NOTHING`` semantics of the hr upserts (rowcount 0 for
rows that already exist), so idempotency is asserted on real insert counts.
Config isolation: ``OPENCODE_CONFIG_DIR`` redirects the global config,
``HOME`` redirects the auth files, ``HR_HOME`` redirects configs/fleet.yaml.
"""

from __future__ import annotations

import json
import textwrap

import pytest
from typer.testing import CliRunner

from hr.cli import app
from hr.discover import scope_providers

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fake connection with ON CONFLICT DO NOTHING semantics
# ---------------------------------------------------------------------------


class _Store:
    """Simulated hr.provider/hr.model rows (key -> payload)."""

    def __init__(self) -> None:
        self.providers: dict[str, str] = {}
        self.models: dict[str, str] = {}


class _DiscoverCursor:
    def __init__(self, store: _Store) -> None:
        self.store = store
        self.rowcount = 0

    def execute(self, sql: str, params=None) -> None:
        self.rowcount = 0
        values = tuple(params or ())
        if "INSERT INTO hr.provider" in sql:
            provider_id, name = values
            if provider_id not in self.store.providers:
                self.store.providers[provider_id] = name
                self.rowcount = 1
        elif "INSERT INTO hr.model" in sql:
            model_id, provider_fk, model_name = values
            if model_id not in self.store.models:
                self.store.models[model_id] = model_name
                self.rowcount = 1

    def __enter__(self) -> _DiscoverCursor:
        return self

    def __exit__(self, *_args) -> bool:
        return False


class _DiscoverConn:
    def __init__(self, store: _Store) -> None:
        self.store = store

    def cursor(self, cursor_factory=None) -> _DiscoverCursor:
        return _DiscoverCursor(self.store)

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_GLOBAL_JSONC = """\
// global opencode config — JSONC comments must be tolerated
{
  "$schema": "https://opencode.ai/config.json",
  /* block comment */
  "provider": {
    "bailian-token-plan": {
      "npm": "@ai-sdk/anthropic",
      "options": { "baseURL": "https://token-plan.example/v1" },
      "models": {
        "qwen3.7-max": { "name": "Qwen 3.7 Max" },
        "deepseek-v4-flash": { "name": "DeepSeek V4 Flash" }
      }
    },
    "kimi-for-coding": {
      "npm": "@ai-sdk/anthropic",
      "options": { "baseURL": "https://api.kimi.com/coding/v1" },
      "models": { "kimi-k2.7-code": { "name": "Kimi K2.7 Code" } }
    }
  }
}
"""


def _write_global_config(config_dir) -> None:
    (config_dir / "opencode.jsonc").write_text(
        textwrap.dedent(_GLOBAL_JSONC), encoding="utf-8"
    )


def _write_auth_v2(root, accounts: dict) -> None:
    auth_dir = root / ".local" / "share" / "opencode"
    auth_dir.mkdir(parents=True)
    (auth_dir / "auth-v2.json").write_text(
        json.dumps({"version": 2, "accounts": accounts, "active": {}}),
        encoding="utf-8",
    )


def _setup_env(tmp_path, monkeypatch, config_dir=None, home=None, cwd=None) -> None:
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(config_dir or tmp_path / "opencode"))
    monkeypatch.setenv("HOME", str(home or tmp_path))
    monkeypatch.setenv("HR_HOME", str(tmp_path / "hr"))
    monkeypatch.chdir(cwd or tmp_path)


def _write_scope_excludes(rhome: Path, excludes: list[str]) -> None:
    ((rhome / "configs")).mkdir(parents=True, exist_ok=True)
    (rhome / "configs" / "fleet.yaml").write_text(
        "scope_excludes:\n" + "".join(f"  - {p}\n" for p in excludes),
        encoding="utf-8",
    )


@pytest.fixture
def discover_env(hr_sandbox: dict):
    """Global opencode.jsonc (2 providers/3 models, all in default scope) +
    auth-v2.json; empty project cwd (staging workspace)."""
    _write_global_config(hr_sandbox["config_dir"])
    _write_auth_v2(hr_sandbox["home"], {"bailian-token-plan": [{"type": "api", "key": "sk-fake"}]})
    return hr_sandbox["tmp_path"]


# ---------------------------------------------------------------------------
# hr discover fixture tests
# ---------------------------------------------------------------------------


class TestDiscoverFixture:
    def test_prints_exactly_three_with_scope_annotation(self, discover_env, monkeypatch):
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

    def test_upsert_idempotent_row_counts_unchanged(self, discover_env, monkeypatch):
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
        result = runner.invoke(app, ["discover", "--help"])
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