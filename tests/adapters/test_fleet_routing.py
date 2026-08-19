"""Data-driven provider routing + capability overlay tests.

Routing derives from the opencode config (npm field -> wire) with the
OPTIONAL ``wire_overrides:`` map in configs/fleet.yaml; capabilities come
from configs/models.yaml. Pure offline: OPENCODE_CONFIG_DIR / HOME /
HR_HOME point at tmp trees, no real config and no network is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import hr.config as config
from hr import fleet
from hr.adapters import adapter_for
from hr.adapters.anthropic_compat import AnthropicCompatAdapter
from hr.adapters.fleet import VALID_TYPES, provider_type, resolve_capabilities
from hr.adapters.openai_compat import OpenAICompatAdapter


@pytest.fixture
def route_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict:
    """Isolated opencode config + configs tree (all envs redirected)."""
    config_dir = tmp_path / "opencode"
    config_dir.mkdir()
    rhome = tmp_path / "hr"
    configs = rhome / "configs"
    configs.mkdir(parents=True)
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HR_HOME", str(rhome))
    (tmp_path / "proj").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path / "proj")
    monkeypatch.setattr(config, "config_path", lambda name: configs / name)
    return {"config_dir": config_dir, "rhome": rhome, "configs": configs}


def _write_providers(route_env: dict, provider_map: dict) -> None:
    (route_env["config_dir"] / "opencode.jsonc").write_text(
        json.dumps({"provider": provider_map}, indent=2), encoding="utf-8"
    )


def _write_overrides(route_env: dict, data: dict) -> None:
    (route_env["rhome"] / "configs" / "fleet.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Routing: npm derivation decides the adapter (no name heuristics)
# ---------------------------------------------------------------------------
def test_npm_anthropic_wire_routes_to_anthropic_adapter(route_env) -> None:
    _write_providers(
        route_env,
        {"acme-ai": {"npm": "@ai-sdk/anthropic", "models": {"generic-model": {}}}},
    )
    assert isinstance(
        adapter_for("acme-ai/generic-model"), AnthropicCompatAdapter
    )


def test_npm_openai_wire_routes_to_openai_adapter(route_env) -> None:
    _write_providers(
        route_env,
        {"acme-ai": {"npm": "@ai-sdk/openai-compatible", "models": {"m": {}}}},
    )
    assert isinstance(adapter_for("acme-ai/m"), OpenAICompatAdapter)


def test_bare_model_id_routes_by_own_provider_key(route_env) -> None:
    _write_providers(
        route_env,
        {"kimi-for-coding": {"npm": "@ai-sdk/anthropic", "models": {"kimi-for-coding": {}}}},
    )
    assert isinstance(adapter_for("kimi-for-coding"), AnthropicCompatAdapter)


def test_registry_provider_routes_via_wire_override(route_env) -> None:
    """kimi-for-coding is NOT in the opencode config — its wire comes from
    the wire_overrides map (this is the real-world kimi/deepseek path)."""
    _write_overrides(route_env, {"wire_overrides": {"deepseek": "openai-compat"}})
    assert isinstance(adapter_for("deepseek/deepseek-v4-pro"), OpenAICompatAdapter)


def test_unknown_provider_raises_with_guidance(route_env) -> None:
    with pytest.raises(ValueError) as exc:
        adapter_for("ghost-provider/qwen3.7-max")
    msg = str(exc.value)
    assert "ghost-provider" in msg
    assert "wire_overrides" in msg
    for t in VALID_TYPES:
        assert t in msg


def test_unknown_npm_fails_loud_naming_provider(route_env) -> None:
    _write_providers(
        route_env,
        {"acme-ai": {"npm": "@ai-sdk/mystery", "models": {"m": {}}}},
    )
    with pytest.raises(ValueError) as exc:
        adapter_for("acme-ai/m")
    msg = str(exc.value)
    assert "acme-ai" in msg
    assert "wire_overrides" in msg


def test_config_provider_without_npm_fails_loud(route_env) -> None:
    _write_providers(route_env, {"acme-ai": {"models": {"m": {}}}})
    with pytest.raises(ValueError) as exc:
        adapter_for("acme-ai/m")
    assert "acme-ai" in str(exc.value)


def test_provider_type_none_when_declared_nowhere(route_env) -> None:
    assert provider_type("nowhere-provider") is None


def test_override_wire_beats_npm(route_env) -> None:
    _write_providers(
        route_env,
        {"acme-ai": {"npm": "@ai-sdk/anthropic", "models": {"m": {}}}},
    )
    _write_overrides(route_env, {"wire_overrides": {"acme-ai": "openai-compat"}})
    assert provider_type("acme-ai") == "openai-compat"


def test_valid_types_are_the_two_wires() -> None:
    assert set(VALID_TYPES) == {"openai-compat", "anthropic-compat"}


# ---------------------------------------------------------------------------
# Capability overlay: conservative defaults, namespaced overrides (models.yaml)
# ---------------------------------------------------------------------------
def test_unknown_model_capabilities_default_to_false(route_env) -> None:
    (route_env["configs"] / "models.yaml").write_text(
        yaml.safe_dump({"capabilities": {}}, sort_keys=False), encoding="utf-8"
    )
    assert resolve_capabilities("acme-ai/mystery-model") == {
        "thinking": False,
        "vision": False,
    }


def test_slug_entry_applies_across_wires(route_env) -> None:
    (route_env["configs"] / "models.yaml").write_text(
        yaml.safe_dump(
            {
                "capabilities": {"kimi-k2.5": {"thinking": True, "vision": True}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    assert resolve_capabilities("bailian-token-plan/kimi-k2.5") == {
        "thinking": True,
        "vision": True,
    }


def test_namespaced_override_beats_slug_entry(route_env) -> None:
    (route_env["configs"] / "models.yaml").write_text(
        yaml.safe_dump(
            {
                "capabilities": {
                    "deepseek-v4-flash": {"thinking": True, "vision": False},
                    "bailian-token-plan/deepseek-v4-flash": {
                        "thinking": False,
                        "vision": False,
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    assert resolve_capabilities("bailian-token-plan/deepseek-v4-flash") == {
        "thinking": False,
        "vision": False,
    }


def test_missing_models_yaml_defaults_to_false(route_env) -> None:
    assert resolve_capabilities("acme-ai/mystery-model") == {
        "thinking": False,
        "vision": False,
    }


def test_string_bool_values_accepted(route_env) -> None:
    (route_env["configs"] / "models.yaml").write_text(
        yaml.safe_dump(
            {
                "capabilities": {
                    "model-a": {"thinking": "true", "vision": "no"},
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    assert resolve_capabilities("model-a") == {"thinking": True, "vision": False}


# ---------------------------------------------------------------------------
# inventory-level wire surface (fleet module)
# ---------------------------------------------------------------------------
def test_fleet_inventory_carries_derived_wires(route_env) -> None:
    _write_providers(
        route_env,
        {
            "anth": {"npm": "@ai-sdk/anthropic", "models": {"a1": {}}},
            "oai": {"npm": "@ai-sdk/openai", "models": {"b1": {}}},
        },
    )
    assert {m.provider: m.wire for m in fleet.fleet_inventory()} == {
        "anth": "anthropic-compat",
        "oai": "openai-compat",
    }