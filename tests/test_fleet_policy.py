from __future__ import annotations

from tests.test_fleet import fleet_env  # noqa: F401 (pytest fixture re-export; resolved by parameter name)

from tests.test_fleet import (
    _ACMESH_ANTHROPIC,
    _providers,
    _write_extras,
    _write_fleet_overrides,
    _write_opencode,
    build_call_plan,
    fleet,
    load_item_repo_batteries,
    pytest,
    select_subsets
)

def test_fleet_models_derived_from_opencode_config(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
    _write_opencode(fleet_env["config_dir"], _providers(_ACMESH_ANTHROPIC))
    assert fleet.fleet_models() == ("alpha/m1", "alpha/m2")

def test_new_model_appears_without_any_file_edit(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
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

def test_npm_wire_table_maps_each_known_package(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
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

def test_unknown_npm_fails_loud_naming_provider(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
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

def test_config_provider_without_npm_fails_loud(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
    _write_opencode(
        fleet_env["config_dir"],
        _providers({"alpha": {"models": {"m1": {}}}}),
    )
    with pytest.raises(ValueError) as exc:
        fleet.fleet_models()
    assert "alpha" in str(exc.value)
    assert "wire_overrides" in str(exc.value)

def test_wire_override_file_honored_for_registry_provider(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
    """A provider NOT in the opencode config routes via wire_overrides."""
    _write_fleet_overrides(
        fleet_env["rhome"],
        {"wire_overrides": {"registry-routed": "anthropic-compat"}},
    )
    from hr.adapters.fleet import provider_type

    assert provider_type("registry-routed") == "anthropic-compat"

def test_wire_override_resolves_unknown_npm(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
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

def test_wire_override_wins_over_npm(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
    _write_opencode(fleet_env["config_dir"], _providers(_ACMESH_ANTHROPIC))
    _write_fleet_overrides(
        fleet_env["rhome"],
        {"wire_overrides": {"alpha": "openai-compat"}},
    )
    assert [m.wire for m in fleet.fleet_inventory()] == [
        "openai-compat",
        "openai-compat",
    ]

def test_invalid_override_wire_value_raises(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
    _write_fleet_overrides(
        fleet_env["rhome"], {"wire_overrides": {"alpha": "bogus-wire"}}
    )
    with pytest.raises(ValueError, match="fleet.yaml"):
        fleet.read_overrides()

def test_corrupt_overrides_file_raises_naming_file(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
    configs = fleet_env["rhome"] / "configs"
    configs.mkdir(parents=True, exist_ok=True)
    (configs / "fleet.yaml").write_text("wire_overrides: [unclosed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="fleet.yaml"):
        fleet.read_overrides()

def test_extra_deployable_merged_into_fleet(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
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

def test_extra_deployable_duplicating_config_model_is_drift(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
    """REGRESSION guard: an extra that duplicates a config-declared model is
    rejected loudly — the model must be removed from deployable.yaml."""
    _write_opencode(fleet_env["config_dir"], _providers(_ACMESH_ANTHROPIC))
    _write_extras(fleet_env["rhome"], ["alpha/m1"])
    with pytest.raises(ValueError) as exc:
        fleet.fleet_models()
    msg = str(exc.value)
    assert "alpha/m1" in msg
    assert "extra_deployable" in msg

def test_missing_extras_file_means_config_only(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
    _write_opencode(fleet_env["config_dir"], _providers(_ACMESH_ANTHROPIC))
    assert fleet.fleet_models() == ("alpha/m1", "alpha/m2")

def test_malformed_extra_entry_raises(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
    _write_extras(fleet_env["rhome"], ["no-provider-slash"])
    with pytest.raises(ValueError, match="deployable.yaml"):
        fleet.fleet_models()

def test_extra_without_override_fails_loud(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
    _write_extras(fleet_env["rhome"], ["registry-routed/slug"])
    with pytest.raises(ValueError) as exc:
        fleet.fleet_models()
    assert "registry-routed" in str(exc.value)
    assert "wire_overrides" in str(exc.value)

def test_default_scope_is_all_discovered_providers(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
    from hr.discover import scope_providers

    _write_opencode(
        fleet_env["config_dir"],
        _providers({**_ACMESH_ANTHROPIC, "beta": {"npm": "@ai-sdk/openai", "models": {"x": {}}}}),
    )
    assert scope_providers() == frozenset({"alpha", "beta"})

def test_scope_excludes_removes_provider(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
    from hr.discover import scope_providers

    _write_opencode(
        fleet_env["config_dir"],
        _providers({**_ACMESH_ANTHROPIC, "beta": {"npm": "@ai-sdk/openai", "models": {"x": {}}}}),
    )
    _write_fleet_overrides(fleet_env["rhome"], {"scope_excludes": ["beta"]})
    assert scope_providers() == frozenset({"alpha"})
