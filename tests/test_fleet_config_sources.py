from __future__ import annotations

from tests.test_fleet import fleet_env  # noqa: F401 (pytest fixture re-export; resolved by parameter name)

from tests.test_fleet import (
    _ACMESH_ANTHROPIC,
    _PROJECT_GLOBAL,
    _providers,
    _write_opencode,
    fleet,
    json,
    pytest
)

def test_new_provider_auto_inherits_scope(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
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

def test_project_opencode_jsonc_overrides_global(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
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

def test_project_dot_opencode_config_read(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
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

def test_missing_opencode_config_raises_with_path(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
    with pytest.raises(ValueError) as exc:
        fleet.fleet_models()
    msg = str(exc.value)
    assert "empty model fleet" in msg
    assert "opencode" in msg
