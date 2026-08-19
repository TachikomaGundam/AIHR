"""Endpoint resolution tests for :mod:`hr2.adapters.anthropic_compat`.

These tests are parse-only — they never touch the network. They write
fake JSONC / JSON to a tempdir and point the adapter at those paths
via the constructor overrides; the ``configs/fleet.yaml`` ``gateway_urls``
fallback resolves against a synthetic fleet file under a tmp ``HR_HOME``
so no real repo or user config is ever read.

Key contracts verified:

  * Provider routing is purely structural: the prefix before ``/`` names
    the provider (a bare slug is its own provider key) — no name lists.
  * ``baseURL`` + ``apiKey`` come from the JSONC config provider block
    after stripping ``//`` and ``/* */`` comments, with the fleet.yaml
    ``gateway_urls`` map as the base-URL fallback for providers that have
    no config block (registry-only gateways).
  * auth.json keys are read per provider (with trailing newline stripped).
  * Missing base URL / key raises ``AdapterError`` naming the provider and
    the declaration sites.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hr.adapters.anthropic_compat import (
    AnthropicCompatAdapter,
    _provider_for,
)
from hr.adapters.base import AdapterError


# ---------------------------------------------------------------------------
# Provider routing — generic prefix-or-bare, no name heuristics
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "model_id, expected_provider",
    [
        ("bailian-token-plan/deepseek-v4-flash", "bailian-token-plan"),
        ("bailian-token-plan/qwen3.7-plus", "bailian-token-plan"),
        ("bailian-token-plan/glm-5.2", "bailian-token-plan"),
        ("bailian-token-plan/kimi-k2.6", "bailian-token-plan"),
        ("kimi-for-coding", "kimi-for-coding"),
        # A bare slug is its own provider key (mirrors adapter_for); the old
        # name-list heuristics (kimi-for-coding-highspeed -> kimi-for-coding)
        # are gone — prefixed ids carry the provider explicitly.
        ("kimi-for-coding-highspeed", "kimi-for-coding-highspeed"),
        ("some-future-provider/slug", "some-future-provider"),
    ],
)
def test_provider_routing(model_id: str, expected_provider: str) -> None:
    assert _provider_for(model_id) == expected_provider


# ---------------------------------------------------------------------------
# Fixture to build a fake opencode config + auth.json + fleet gateway map
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_config(tmp_path: Path, monkeypatch) -> tuple[Path, Path, dict[str, str]]:
    """Return (opencode_jsonc_path, auth_json_path, keys)."""
    # Synthetic gateway_urls under a tmp HR_HOME: the adapter's base-URL
    # fallback must never read the repo's real configs/fleet.yaml.
    hr_home = tmp_path / "hr-home"
    configs = hr_home / "configs"
    configs.mkdir(parents=True)
    (configs / "fleet.yaml").write_text(
        "gateway_urls:\n"
        "  kimi-for-coding: https://example.invalid/kimi/v1\n",
        encoding="utf-8",
    )
    (configs / "models.yaml").write_text(
        "capabilities:\n"
        "  qwen3.7-plus: {thinking: true, vision: true}\n"
        "  bailian-token-plan/deepseek-v4-flash: {thinking: false, vision: false}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HR_HOME", str(hr_home))

    opencode = tmp_path / "opencode.jsonc"
    # Include both // and /* */ comment styles to exercise the stripper.
    opencode.write_text(
        """
        {
          // top-level comment
          "provider": {
            /* block comment */
            "bailian-token-plan": {
              "options": {
                "baseURL": "https://example.invalid/apps/anthropic/v1",
                "apiKey": "sk-test-bailian-1234"
              }
            }
          }
        }
        """,
        encoding="utf-8",
    )
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps({"kimi-for-coding": {"key": "sk-test-kimi-5678\n"}}),
        encoding="utf-8",
    )
    return opencode, auth, {
        "bailian": "sk-test-bailian-1234",
        "kimi": "sk-test-kimi-5678",
    }


# ---------------------------------------------------------------------------
def test_bailian_endpoint_resolution(fake_config) -> None:
    opencode, auth, keys = fake_config
    adapter = AnthropicCompatAdapter(
        opencode_config_path=opencode,
        auth_json_path=auth,
    )
    endpoint = adapter._resolve_endpoint(
        "bailian-token-plan/deepseek-v4-flash"
    )
    assert endpoint.provider == "bailian-token-plan"
    assert endpoint.url == (
        "https://example.invalid/apps/anthropic/v1/messages"
    )
    assert endpoint.headers["x-api-key"] == keys["bailian"]
    assert endpoint.headers["anthropic-version"] == "2023-06-01"


def test_kimi_endpoint_resolution_from_gateway_map(fake_config) -> None:
    """kimi has no config block — base URL falls back to fleet gateway_urls,
    key comes from auth.json (trailing newline stripped)."""
    opencode, auth, keys = fake_config
    adapter = AnthropicCompatAdapter(
        opencode_config_path=opencode,
        auth_json_path=auth,
    )
    endpoint = adapter._resolve_endpoint("kimi-for-coding")
    assert endpoint.provider == "kimi-for-coding"
    assert endpoint.url == "https://example.invalid/kimi/v1/messages"
    # Trailing newline must be stripped.
    assert endpoint.headers["x-api-key"] == keys["kimi"]


def test_missing_bailian_key_raises(fake_config, tmp_path: Path) -> None:
    opencode = tmp_path / "opencode.jsonc"
    opencode.write_text(
        '{"provider": {"bailian-token-plan": {"options": {}}}}',
        encoding="utf-8",
    )
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps({"kimi-for-coding": {"key": "sk-test-kimi-5678"}}),
        encoding="utf-8",
    )
    adapter = AnthropicCompatAdapter(
        opencode_config_path=opencode,
        auth_json_path=auth,
    )
    with pytest.raises(AdapterError):
        adapter._resolve_endpoint("bailian-token-plan/deepseek-v4-flash")


def test_missing_auth_json_raises(fake_config, tmp_path: Path) -> None:
    opencode = tmp_path / "opencode.jsonc"
    opencode.write_text(
        '{"provider": {"bailian-token-plan": {"options": {"apiKey": "x"}}}}',
        encoding="utf-8",
    )
    adapter = AnthropicCompatAdapter(
        opencode_config_path=opencode,
        auth_json_path=tmp_path / "no-auth.json",
    )
    with pytest.raises(AdapterError):
        adapter._resolve_endpoint("kimi-for-coding")


# ---------------------------------------------------------------------------
# Capability probing
# ---------------------------------------------------------------------------
def test_probe_capabilities_thinking_models(fake_config) -> None:
    opencode, auth, _ = fake_config
    adapter = AnthropicCompatAdapter(
        opencode_config_path=opencode,
        auth_json_path=auth,
    )
    cap = adapter.probe_capabilities("bailian-token-plan/qwen3.7-plus")
    assert cap.supports_thinking is True
    assert cap.provider == "bailian-token-plan"

    cap2 = adapter.probe_capabilities(
        "bailian-token-plan/deepseek-v4-flash"
    )
    assert cap2.supports_thinking is False


def test_probe_capabilities_caches(fake_config) -> None:
    opencode, auth, _ = fake_config
    adapter = AnthropicCompatAdapter(
        opencode_config_path=opencode,
        auth_json_path=auth,
    )
    c1 = adapter.probe_capabilities("bailian-token-plan/qwen3.7-plus")
    c2 = adapter.probe_capabilities("bailian-token-plan/qwen3.7-plus")
    assert c1 is c2