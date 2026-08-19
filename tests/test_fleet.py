"""Dynamic fleet tests (hr-unification todo 23 rework).

The fleet is derived at RUNTIME from the opencode config files — there is
no fleet manifest to maintain. These tests prove: the inventory equals the
merged config's ``provider.*.models`` entries plus ``extra_deployable:``,
a NEW model appears in the stage pools with zero file edits, npm -> wire
derivation (unknown npm fails loud), the OPTIONAL overrides file
(``wire_overrides`` / ``scope_excludes``) is honored, extra_deployable
drift is rejected, and project configs override the global one.

Pure offline: OPENCODE_CONFIG_DIR / HOME / HR_HOME point at tmp trees, so
no real ~/.config and no network are touched.
"""

from __future__ import annotations

import json
import copy
from pathlib import Path

import pytest
import yaml

from hr import fleet
from hr.stage0 import build_call_plan, select_subsets


def _providers(provider_map: dict) -> dict:
    """opencode.jsonc payload — DEEP-copied so tests never mutate the shared
    fixture constants (a shallow copy would leak edits across tests)."""
    return {"provider": copy.deepcopy(provider_map)}


def _write_opencode(config_dir: Path, data: dict) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "opencode.jsonc").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


def _write_extras(rhome: Path, ids: list[str]) -> None:
    configs = rhome / "configs"
    configs.mkdir(parents=True, exist_ok=True)
    (configs / "deployable.yaml").write_text(
        yaml.safe_dump({"extra_deployable": ids}, sort_keys=False), encoding="utf-8"
    )


def _write_fleet_overrides(rhome: Path, data: dict) -> None:
    configs = rhome / "configs"
    configs.mkdir(parents=True, exist_ok=True)
    (configs / "fleet.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )


@pytest.fixture
def fleet_env(hr_sandbox: dict) -> dict:
    """Isolated opencode config + repo configs tree (staging workspace)."""
    return {
        "config_dir": hr_sandbox["config_dir"],
        "rhome": hr_sandbox["hr_home"],
        "tmp_path": hr_sandbox["tmp_path"],
    }


_ACMESH_ANTHROPIC = {
    "alpha": {
        "npm": "@ai-sdk/anthropic",
        "models": {"m1": {"name": "M1"}, "m2": {"name": "M2"}},
    }
}


def test_fleet_models_derived_from_opencode_config(fleet_env) -> None:
    _write_opencode(fleet_env["config_dir"], _providers(_ACMESH_ANTHROPIC))
    assert fleet.fleet_models() == ("alpha/m1", "alpha/m2")


def test_new_model_appears_without_any_file_edit(fleet_env) -> None:
    """REGRESSION (the point of the whole rework): a model added to the
    opencode config flows into the stage pool with zero edits elsewhere."""
    cfg = fleet_env["config_dir"]
    _write_opencode(cfg, _providers(_ACMESH_ANTHROPIC))
    assert fleet.fleet_models() == ("alpha/m1", "alpha/m2")

    # simulate someone editing opencode.jsonc to add a model (same file, no
    # other file touched)
    data = _providers({**_ACMESH_ANTHROPIC})
    data["provider"]["alpha"]["models"]["m3"] = {"name": "M3"}
    _write_opencode(cfg, data)
    assert fleet.fleet_models() == ("alpha/m1", "alpha/m2", "alpha/m3")

    # and the stage0 plan picks the new model up immediately
    items = select_subsets(
        load_item_repo_batteries()
    )
    plan = build_call_plan(items)
    assert "alpha/m3" in plan.models


def load_item_repo_batteries() -> dict:
    from hr.calibrate import load_item_repo
    from hr.stage0 import STAGE0_BATTERIES

    return load_item_repo(
        Path(__file__).resolve().parents[1] / "itemrepo",
        batteries=list(STAGE0_BATTERIES),
    )


# ---------------------------------------------------------------------------
# npm -> wire derivation (explicit table; unknown npm fails loud)
# ---------------------------------------------------------------------------
def test_npm_wire_table_maps_each_known_package(fleet_env) -> None:
    _write_opencode(
        fleet_env["config_dir"],
        _providers(
            {
                "anthropic": {"npm": "@ai-sdk/anthropic", "models": {"a": {}}},
                "oai": {"npm": "@ai-sdk/openai", "models": {"b": {}}},
                "oai-compat": {"npm": "@ai-sdk/openai-compat", "models": {"c": {}}},
                "oai-compatible": {
                    "npm": "@ai-sdk/openai-compatible",
                    "models": {"d": {}},
                },
            }
        ),
    )
    wires = {m.provider: m.wire for m in fleet.fleet_inventory()}
    assert wires == {
        "anthropic": "anthropic-compat",
        "oai": "openai-compat",
        "oai-compat": "openai-compat",
        "oai-compatible": "openai-compat",
    }


def test_unknown_npm_fails_loud_naming_provider(fleet_env) -> None:
    _write_opencode(
        fleet_env["config_dir"],
        _providers(
            {
                "alpha": {
                    "npm": "@ai-sdk/mystery-vendor",
                    "models": {"m1": {}},
                }
            }
        ),
    )
    with pytest.raises(ValueError) as exc:
        fleet.fleet_models()
    msg = str(exc.value)
    assert "alpha" in msg
    assert "@ai-sdk/mystery-vendor" in msg
    assert "wire_overrides" in msg


def test_config_provider_without_npm_fails_loud(fleet_env) -> None:
    _write_opencode(
        fleet_env["config_dir"],
        _providers({"alpha": {"models": {"m1": {}}}}),
    )
    with pytest.raises(ValueError) as exc:
        fleet.fleet_models()
    assert "alpha" in str(exc.value)
    assert "wire_overrides" in str(exc.value)


# ---------------------------------------------------------------------------
# OPTIONAL overrides file (configs/fleet.yaml)
# ---------------------------------------------------------------------------
def test_wire_override_file_honored_for_registry_provider(fleet_env) -> None:
    """A provider NOT in the opencode config routes via wire_overrides."""
    _write_fleet_overrides(
        fleet_env["rhome"],
        {"wire_overrides": {"registry-routed": "anthropic-compat"}},
    )
    from hr.adapters.fleet import provider_type

    assert provider_type("registry-routed") == "anthropic-compat"


def test_wire_override_resolves_unknown_npm(fleet_env) -> None:
    _write_opencode(
        fleet_env["config_dir"],
        _providers(
            {"alpha": {"npm": "mystery-pkg", "models": {"m1": {}}}}
        ),
    )
    _write_fleet_overrides(
        fleet_env["rhome"],
        {"wire_overrides": {"alpha": "anthropic-compat"}},
    )
    assert [m.wire for m in fleet.fleet_inventory()] == ["anthropic-compat"]


def test_wire_override_wins_over_npm(fleet_env) -> None:
    _write_opencode(fleet_env["config_dir"], _providers(_ACMESH_ANTHROPIC))
    _write_fleet_overrides(
        fleet_env["rhome"],
        {"wire_overrides": {"alpha": "openai-compat"}},
    )
    assert [m.wire for m in fleet.fleet_inventory()] == [
        "openai-compat",
        "openai-compat",
    ]


def test_invalid_override_wire_value_raises(fleet_env) -> None:
    _write_fleet_overrides(
        fleet_env["rhome"], {"wire_overrides": {"alpha": "bogus-wire"}}
    )
    with pytest.raises(ValueError, match="fleet.yaml"):
        fleet.read_overrides()


def test_corrupt_overrides_file_raises_naming_file(fleet_env) -> None:
    configs = fleet_env["rhome"] / "configs"
    configs.mkdir(parents=True, exist_ok=True)
    (configs / "fleet.yaml").write_text("wire_overrides: [unclosed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="fleet.yaml"):
        fleet.read_overrides()


# ---------------------------------------------------------------------------
# extra_deployable merge + drift guard
# ---------------------------------------------------------------------------
def test_extra_deployable_merged_into_fleet(fleet_env) -> None:
    _write_opencode(fleet_env["config_dir"], _providers(_ACMESH_ANTHROPIC))
    _write_extras(
        fleet_env["rhome"],
        ["kimi-for-coding/k3", "deepseek/deepseek-v4-flash"],
    )
    _write_fleet_overrides(
        fleet_env["rhome"],
        {
            "wire_overrides": {
                "kimi-for-coding": "anthropic-compat",
                "deepseek": "openai-compat",
            }
        },
    )
    models = fleet.fleet_models()
    assert models == (
        "alpha/m1",
        "alpha/m2",
        "deepseek/deepseek-v4-flash",
        "kimi-for-coding/k3",
    )
    external = {f"{m.provider}/{m.model_id}": m.external for m in fleet.fleet_inventory()}
    assert external["deepseek/deepseek-v4-flash"] is True
    assert external["alpha/m1"] is False


def test_extra_deployable_duplicating_config_model_is_drift(fleet_env) -> None:
    """REGRESSION guard: an extra that duplicates a config-declared model is
    rejected loudly — the model must be removed from deployable.yaml."""
    _write_opencode(fleet_env["config_dir"], _providers(_ACMESH_ANTHROPIC))
    _write_extras(fleet_env["rhome"], ["alpha/m1"])
    with pytest.raises(ValueError) as exc:
        fleet.fleet_models()
    msg = str(exc.value)
    assert "alpha/m1" in msg
    assert "extra_deployable" in msg


def test_missing_extras_file_means_config_only(fleet_env) -> None:
    _write_opencode(fleet_env["config_dir"], _providers(_ACMESH_ANTHROPIC))
    assert fleet.fleet_models() == ("alpha/m1", "alpha/m2")


def test_malformed_extra_entry_raises(fleet_env) -> None:
    _write_extras(fleet_env["rhome"], ["no-provider-slash"])
    with pytest.raises(ValueError, match="deployable.yaml"):
        fleet.fleet_models()


def test_extra_without_override_fails_loud(fleet_env) -> None:
    _write_extras(fleet_env["rhome"], ["registry-routed/slug"])
    with pytest.raises(ValueError) as exc:
        fleet.fleet_models()
    assert "registry-routed" in str(exc.value)
    assert "wire_overrides" in str(exc.value)


# ---------------------------------------------------------------------------
# scope: all discovered providers minus overrides' scope_excludes
# ---------------------------------------------------------------------------
def test_default_scope_is_all_discovered_providers(fleet_env) -> None:
    from hr.discover import scope_providers

    _write_opencode(
        fleet_env["config_dir"],
        _providers({**_ACMESH_ANTHROPIC, "beta": {"npm": "@ai-sdk/openai", "models": {"x": {}}}}),
    )
    assert scope_providers() == frozenset({"alpha", "beta"})


def test_scope_excludes_removes_provider(fleet_env) -> None:
    from hr.discover import scope_providers

    _write_opencode(
        fleet_env["config_dir"],
        _providers({**_ACMESH_ANTHROPIC, "beta": {"npm": "@ai-sdk/openai", "models": {"x": {}}}}),
    )
    _write_fleet_overrides(fleet_env["rhome"], {"scope_excludes": ["beta"]})
    assert scope_providers() == frozenset({"alpha"})


def test_new_provider_auto_inherits_scope(fleet_env) -> None:
    """REGRESSION: adding a provider to the opencode config puts it in the
    default scope with zero edits — exclusions are opt-out only."""
    from hr.discover import enumerate_models, scope_providers

    cfg = fleet_env["config_dir"]
    _write_opencode(cfg, _providers(_ACMESH_ANTHROPIC))
    assert sorted({m.provider for m in enumerate_models(scope_providers())}) == ["alpha"]

    data = _providers(
        {**_ACMESH_ANTHROPIC, "gamma": {"npm": "@ai-sdk/anthropic", "models": {"g1": {}}}}
    )
    _write_opencode(cfg, data)
    assert sorted({m.provider for m in enumerate_models(scope_providers())}) == [
        "alpha",
        "gamma",
    ]


# ---------------------------------------------------------------------------
# project config precedence (global < project < .opencode/)
# ---------------------------------------------------------------------------
_PROJECT_GLOBAL = {
    "alpha": {"npm": "@ai-sdk/anthropic", "models": {"m1": {}}},
    "shared": {"npm": "@ai-sdk/openai", "models": {"base": {}}},
}


def test_project_opencode_jsonc_overrides_global(fleet_env) -> None:
    _write_opencode(fleet_env["config_dir"], _providers(_PROJECT_GLOBAL))
    proj = fleet_env["tmp_path"] / "proj"
    proj.mkdir(exist_ok=True)
    (proj / "opencode.jsonc").write_text(
        json.dumps(
            _providers(
                {
                    "alpha": {
                        "npm": "@ai-sdk/anthropic",
                        "models": {"m1": {}, "m2": {}},
                    }
                }
            )
        ),
        encoding="utf-8",
    )
    # project alpha replaces the global block; global-only 'shared' survives
    assert fleet.fleet_models() == (
        "alpha/m1",
        "alpha/m2",
        "shared/base",
    )


def test_project_dot_opencode_config_read(fleet_env) -> None:
    _write_opencode(fleet_env["config_dir"], _providers(_ACMESH_ANTHROPIC))
    proj = fleet_env["tmp_path"] / "proj"
    (proj / ".opencode").mkdir(parents=True, exist_ok=True)
    (proj / ".opencode" / "opencode.jsonc").write_text(
        json.dumps(
            _providers({"zeta": {"npm": "@ai-sdk/openai", "models": {"z1": {}}}})
        ),
        encoding="utf-8",
    )
    assert fleet.fleet_models() == ("alpha/m1", "alpha/m2", "zeta/z1")


def test_missing_opencode_config_raises_with_path(fleet_env) -> None:
    with pytest.raises(ValueError) as exc:
        fleet.fleet_models()
    msg = str(exc.value)
    assert "empty model fleet" in msg
    assert "opencode" in msg