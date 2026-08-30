"""Offline fixture tests for ``hr discover`` (static opencode.jsonc enumeration).

None of these touch the live database: a fake connection simulates the
``ON CONFLICT DO NOTHING`` semantics of the HR upserts (rowcount 0 for
rows that already exist), so idempotency is asserted on real insert counts.
Config isolation: ``OPENCODE_CONFIG_DIR`` redirects the global config,
``HOME`` redirects the auth files, ``HR_HOME`` redirects configs/fleet.yaml.
"""

from __future__ import annotations

from pathlib import Path

import json

import textwrap

import pytest

from typer.testing import CliRunner

from hr.cli import app  # noqa: F401 (re-export; consumed by sibling test modules)

from hr.discover import scope_providers  # noqa: F401 (re-export; consumed by sibling test modules)

runner = CliRunner()

class _Store:
    """Simulated provider and model rows."""

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
