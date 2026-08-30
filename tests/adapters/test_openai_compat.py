"""Endpoint resolution for the OpenAI-compatible adapter (no hardcoded base URL).

The base URL chain (override > models cache > opencode options > fleet.yaml
gateway_urls) must be locked: a previously hardcoded public endpoint was
removed and every link of the fallback chain is covered here.
"""

import json
import inspect

import pytest

from hr.adapters.openai_compat import (
    OpenAICompatAdapter,
    _resolve_endpoint,
    _thinking_budget_to_effort,
    _to_oai_messages,
)


def _write_auth(root, provider: str, key: str = "sk-test-openai-0000") -> str:
    auth = root / "auth.json"
    auth.write_text(json.dumps({provider: {"key": key}}), encoding="utf-8")
    return str(auth)


def _write_fleet(sandbox, gateway_url: str | None = None) -> None:
    (sandbox["configs"] / "fleet.yaml").write_text(
        "wire_overrides:\n  acme: openai-compat\n"
        + (f"gateway_urls:\n  acme: {gateway_url}\n" if gateway_url else ""),
        encoding="utf-8",
    )


def _write_open_code(root, provider: str, base_url: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    root.joinpath("opencode.jsonc").write_text(
        json.dumps(
            {
                "provider": {
                    provider: {
                        "options": {"baseURL": base_url},
                        "models": {f"{provider}/dummy": {"name": "dummy"}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def test_standard_thinking_budget_remains_bounded() -> None:
    # Given / When / Then
    assert _thinking_budget_to_effort(16_384) == "high"


def test_chat_defaults_match_adapter_protocol() -> None:
    # Given: the concrete OpenAI-compatible chat signature.
    parameters = inspect.signature(OpenAICompatAdapter.chat).parameters

    # When / Then: direct callers receive the shared adapter defaults.
    assert parameters["max_output"].default == 16_384
    assert parameters["timeout_s"].default == 600


def test_images_are_attached_to_the_last_user_message() -> None:
    # Given
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ack"},
        {"role": "user", "content": "inspect"},
    ]
    images = [{"data": "aGVsbG8=", "media_type": "image/png"}]

    # When
    translated = _to_oai_messages(messages, images)

    # Then
    assert translated[-1]["content"] == [
        {"type": "text", "text": "inspect"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,aGVsbG8="},
        },
    ]
    assert translated[0]["content"] == "first"


def test_list_models_supports_flat_provider_cache(tmp_path) -> None:
    # Given
    cache = tmp_path / "models.json"
    cache.write_text(
        json.dumps({"acme": {"models": {"fast": {}, "smart": {}}}}),
        encoding="utf-8",
    )

    # When
    models = OpenAICompatAdapter(opencode_config_path=str(cache)).list_models()

    # Then
    assert models == ["acme/fast", "acme/smart"]


# ---------------------------------------------------------------------------
# fallback chain
# ---------------------------------------------------------------------------
class TestBaseUrlChain:
    def test_override_wins(self, hr_sandbox):
        sandbox = hr_sandbox
        _write_fleet(sandbox)
        auth = _write_auth(sandbox["home"], "acme")
        ep = _resolve_endpoint(
            "acme/foo",
            auth_path=auth,
            base_url_override="https://override.invalid",
        )
        assert ep.url == "https://override.invalid/chat/completions"

    def test_models_cache_fallback(self, hr_sandbox):
        sandbox = hr_sandbox
        _write_fleet(sandbox)
        cache = sandbox["home"] / "models.json"
        cache.write_text(
            json.dumps({"acme": {"api": "https://cache.invalid"}}),
            encoding="utf-8",
        )
        auth = _write_auth(sandbox["home"], "acme")
        ep = _resolve_endpoint("acme/foo", config_path=str(cache), auth_path=auth)
        assert ep.url == "https://cache.invalid/chat/completions"

    def test_opencode_options_fallback(self, hr_sandbox, monkeypatch):
        sandbox = hr_sandbox
        _write_fleet(sandbox)
        _write_open_code(sandbox["config_dir"], "acme", "https://cfg.invalid")
        auth = _write_auth(sandbox["home"], "acme")
        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(sandbox["config_dir"]))
        ep = _resolve_endpoint("acme/foo", config_path="/nonexistent.json", auth_path=auth)
        assert ep.url == "https://cfg.invalid/chat/completions"

    def test_gateway_urls_fallback(self, hr_sandbox):
        sandbox = hr_sandbox
        _write_fleet(sandbox, gateway_url="https://gw.invalid")
        auth = _write_auth(sandbox["home"], "acme")
        ep = _resolve_endpoint(
            "acme/foo",
            config_path="/nonexistent.json",
            auth_path=auth,
        )
        assert ep.url == "https://gw.invalid/chat/completions"

    def test_nothing_declared_fails_loud(self, hr_sandbox):
        sandbox = hr_sandbox
        _write_fleet(sandbox)
        auth = _write_auth(sandbox["home"], "acme")
        with pytest.raises(Exception) as exc:
            _resolve_endpoint(
                "acme/foo",
                config_path="/nonexistent.json",
                auth_path=auth,
            )
        assert "No base URL declared" in str(exc.value)
        for site in ("opencode.jsonc", "configs/fleet.yaml", "models cache"):
            assert site in str(exc.value)

    def test_trailing_slash_stripped(self, hr_sandbox):
        sandbox = hr_sandbox
        _write_fleet(sandbox)
        auth = _write_auth(sandbox["home"], "acme")
        ep = _resolve_endpoint(
            "acme/foo",
            auth_path=auth,
            base_url_override="https://override.invalid//",
        )
        assert ep.url == "https://override.invalid/chat/completions"
