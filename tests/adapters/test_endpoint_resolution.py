"""Endpoint resolution tests for :mod:`hr.adapters.anthropic_compat`.

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


def test_endpoint_resolution_preserves_comment_markers_inside_strings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # Given: valid JSONC whose credential contains block-comment markers.
    hr_home = tmp_path / "hr-home"
    configs = hr_home / "configs"
    configs.mkdir(parents=True)
    (configs / "fleet.yaml").write_text("gateway_urls: {}\n", encoding="utf-8")
    (configs / "models.yaml").write_text("capabilities: {}\n", encoding="utf-8")
    monkeypatch.setenv("HR_HOME", str(hr_home))
    opencode = tmp_path / "opencode.jsonc"
    opencode.write_text(
        '{"provider":{"vendor":{"options":{"baseURL":"https://example.invalid/v1",'
        '"apiKey":"key/*literal*/value"}}}}',
        encoding="utf-8",
    )
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")

    # When: the endpoint is resolved through the adapter boundary.
    endpoint = AnthropicCompatAdapter(
        opencode_config_path=opencode,
        auth_json_path=auth,
    )._resolve_endpoint("vendor/model")

    # Then: string content is not treated as a JSONC comment.
    assert endpoint.headers["x-api-key"] == "key/*literal*/value"


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


class _StreamResponse:
    status_code = 200

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def iter_lines(self):
        return iter(self._lines)


class _RecordingStreamClient:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.timeout = None

    def stream(self, _method, _url, **kwargs):
        self.timeout = kwargs.get("timeout")
        return _StreamResponse(self._lines)


def test_stream_accepts_tool_only_response_with_zero_usage(fake_config) -> None:
    # Given: a valid SSE response containing only an Anthropic tool call.
    opencode, auth, _ = fake_config
    client = _RecordingStreamClient([
        "event: content_block_start",
        'data: {"content_block":{"type":"tool_use","id":"call-1","name":"lookup"}}',
        "",
        "event: content_block_delta",
        'data: {"delta":{"type":"input_json_delta","partial_json":"{\\"q\\":1}"}}',
        "",
        "event: content_block_stop",
        "data: {}",
        "",
    ])
    adapter = AnthropicCompatAdapter(
        opencode_config_path=opencode,
        auth_json_path=auth,
        http_client=client,
    )

    # When: the stream is decoded without text or token usage metadata.
    response = adapter._do_stream(
        adapter._resolve_endpoint("bailian-token-plan/qwen3.7-plus"),
        {},
        "bailian-token-plan/qwen3.7-plus",
        37,
    )

    # Then: the tool call is the response rather than an empty-body failure.
    assert response.tool_calls == [{"id": "call-1", "name": "lookup", "input": {"q": 1}}]


def test_stream_uses_each_request_timeout(fake_config) -> None:
    # Given: an injected reusable client and a minimal successful response.
    opencode, auth, _ = fake_config
    client = _RecordingStreamClient([
        "event: content_block_delta",
        'data: {"delta":{"type":"text_delta","text":"ok"}}',
        "",
    ])
    adapter = AnthropicCompatAdapter(
        opencode_config_path=opencode,
        auth_json_path=auth,
        http_client=client,
    )

    # When: a call requests a timeout different from the adapter default.
    adapter._do_stream(
        adapter._resolve_endpoint("bailian-token-plan/qwen3.7-plus"),
        {},
        "bailian-token-plan/qwen3.7-plus",
        37,
    )

    # Then: the request carries that timeout instead of a cached default.
    assert client.timeout == 37
