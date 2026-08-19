"""Regression tests for the audit refactors (audit-driven, all hermetic).

Covers the five mandated behaviors:

1. calibration anchors come from ``configs/seats.yaml`` and fail loud when
   the section is missing;
2. the registry inventory is dynamic — a model added to a fixture opencode
   config flows into ``discover_models()`` (extras, overlays, metadata);
3. generic provider config access (opencode block -> gateway_urls ->
   auth.json) without provider-name literals;
4. recommend reads seat definitions from ``configs/seats.yaml``
   (``primary_capabilities`` drive the fit weights);
5. itemrepo resolves through ``HR_HOME`` with ``HR_ITEMREPO`` override and
   a fail-loud RuntimeError naming the resolution.

No fixture touches the real ``~/.config``, the repo configs, the DB or the
network: every test pins ``OPENCODE_CONFIG_DIR`` / ``HOME`` / ``HR_HOME``
to tmp dirs and the working directory to a tmp project.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hr.calibrate as calibrate
import hr.recommend as recommend
import hr.registry as registry
from hr.config import (
    config_path,
    gateway_urls,
    get_provider_config,
    itemrepo_path,
)
from hr.models import BenchmarkCategory as BC


# ---------------------------------------------------------------------------
# Shared hermetically-sealed environment
# ---------------------------------------------------------------------------
@pytest.fixture
def hr_env(hr_sandbox: dict, monkeypatch) -> dict[str, Path]:
    """Seal HOME, OPENCODE_CONFIG_DIR and HR_HOME into tmp; chdir to tmp.

    Returns ``{"home", "config_dir", "hr_home", "project"}``.
    """
    # itemrepo-resolution tests exercise the HR_HOME default explicitly,
    # so the staging workspace must not preset HR_ITEMREPO here.
    monkeypatch.delenv("HR_ITEMREPO", raising=False)
    return {
        "home": hr_sandbox["home"],
        "config_dir": hr_sandbox["config_dir"],
        "hr_home": hr_sandbox["hr_home"],
        "project": hr_sandbox["project"],
    }


def _write(hr_home: Path, name: str, text: str) -> Path:
    path = hr_home / "configs" / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. Calibration anchors from configs/seats.yaml — fail loud when missing
# ---------------------------------------------------------------------------
class TestCalibrationAnchorsFromConfig:
    def test_anchors_load_from_seats_yaml(self, hr_env) -> None:
        # Given: seats.yaml with a calibration_anchors section
        _write(
            hr_env["hr_home"],
            "seats.yaml",
            "seats: []\n"
            "calibration_anchors:\n"
            "  cheap: fixture-wire/cheap-model\n"
            "  expensive: fixture-wire/expensive-model\n",
        )
        # When: load_anchors()
        anchors = calibrate.load_anchors()
        # Then: the fixture anchors come back verbatim
        assert anchors == {
            "cheap": "fixture-wire/cheap-model",
            "expensive": "fixture-wire/expensive-model",
        }

    def test_missing_section_fails_loud_naming_seats_yaml(self, hr_env) -> None:
        # Given: seats.yaml with no calibration_anchors section
        _write(hr_env["hr_home"], "seats.yaml", "seats: []\n")
        # When/Then: RuntimeError naming the file
        with pytest.raises(RuntimeError, match="seats\\.yaml"):
            calibrate.load_anchors()

    def test_missing_file_fails_loud(self, hr_env) -> None:
        # Given: no seats.yaml at all
        # When/Then: RuntimeError naming the file
        with pytest.raises(RuntimeError, match="seats\\.yaml"):
            calibrate.load_anchors()

    def test_runner_defaults_to_config_anchors(self, hr_env) -> None:
        # Given: anchors in config and a runner constructed WITHOUT anchors=
        _write(
            hr_env["hr_home"],
            "seats.yaml",
            "seats: []\n"
            "calibration_anchors:\n"
            "  cheap: fixture-wire/cheap-model\n",
        )
        # When: the runner resolves its default anchors
        runner = calibrate.CalibrationRunner(
            adapter=object(),  # type: ignore[arg-type]  # not called in this test
            item_repo=hr_env["project"],
        )
        # Then: the anchors come from config, not from any code table
        assert runner.anchors == {"cheap": "fixture-wire/cheap-model"}


# ---------------------------------------------------------------------------
# 2. Registry dynamic inventory — a new config model flows into discover_models
# ---------------------------------------------------------------------------
class TestRegistryDynamicInventory:
    def _fleet(self, hr_env) -> None:
        (hr_env["config_dir"] / "opencode.jsonc").write_text(
            json.dumps(
                {
                    "provider": {
                        "fixture-wire": {
                            "npm": "@ai-sdk/anthropic",
                            "options": {"baseURL": "https://example.invalid/v1"},
                            "models": {
                                "fixture-model-a": {
                                    "name": "Fixture Model A",
                                    "attachment": True,
                                    "limit": {"context": 64000, "output": 8000},
                                },
                                "fixture-model-b": {
                                    "options": {"thinking": "enabled"}
                                },
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        _write(
            hr_env["hr_home"],
            "fleet.yaml",
            "wire_overrides:\n  fixture-extra-wire: anthropic-compat\n",
        )
        _write(
            hr_env["hr_home"],
            "deployable.yaml",
            "extra_deployable:\n  - fixture-extra-wire/fixture-extra\n",
        )
        _write(
            hr_env["hr_home"],
            "models.yaml",
            # namespaced overlay wins over config metadata: model-a attachment
            # says vision, the overlay says text-only
            "capabilities:\n"
            "  fixture-wire/fixture-model-a: {vision: false, thinking: false}\n"
            "  fixture-wire/fixture-model-b: {vision: true}\n"
            "  fixture-extra-wire/fixture-extra: {thinking: true, vision: false}\n",
        )

    def test_config_model_flows_into_inventory(self, hr_env) -> None:
        # Given: an opencode config declaring two models under a new provider
        self._fleet(hr_env)
        # When: discover_models()
        profiles = {p.model_id: p for p in registry.discover_models()}
        # Then: both config models are present with the config metadata
        a = profiles["fixture-model-a"]
        assert a.provider == "fixture-wire"
        assert a.display_name == "Fixture Model A"
        assert a.context_window == 64000
        assert a.max_output == 8000
        # config-declared capability facts hold; the models.yaml overlay can
        # only ADD capabilities it knows about, never remove a config fact
        assert a.supports_vision is True  # attachment: true in the config
        assert a.supports_thinking is False
        assert a.api_base_url == "https://example.invalid/v1"

        b = profiles["fixture-model-b"]
        assert b.context_window is None
        assert b.supports_thinking is True  # config options.thinking
        assert b.supports_vision is True  # bare-slug-ish overlay on this wire

    def test_extra_model_flows_into_inventory(self, hr_env) -> None:
        # Given: an extra_deployable model with a wire override
        self._fleet(hr_env)
        # When/Then: the extra is discoverable with the overlay applied
        profiles = {p.model_id: p for p in registry.discover_models()}
        extra = profiles["fixture-extra"]
        assert extra.provider == "fixture-extra-wire"
        assert extra.supports_thinking is True
        assert extra.supports_vision is False


# ---------------------------------------------------------------------------
# 3. Generic provider config access
# ---------------------------------------------------------------------------
class TestGenericProviderConfig:
    def test_from_opencode_block(self, hr_env) -> None:
        # Given: provider block with baseURL + apiKey
        (hr_env["config_dir"] / "opencode.jsonc").write_text(
            json.dumps(
                {
                    "provider": {
                        "fixture-provider": {
                            "options": {
                                "baseURL": "https://example.invalid/gw",
                                "apiKey": "sk-test-abc",
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        # When/Then: resolved from the block
        cfg = get_provider_config("fixture-provider")
        assert cfg.base_url == "https://example.invalid/gw"
        assert cfg.api_key == "sk-test-abc"

    def test_gateway_urls_fallback(self, hr_env) -> None:
        # Given: gateway_urls entry, key in auth.json, no config block
        _write(
            hr_env["hr_home"],
            "fleet.yaml",
            "gateway_urls:\n  fixture-gw: https://example.invalid/gw\n",
        )
        auth = hr_env["home"] / ".local" / "share" / "opencode"
        auth.mkdir(parents=True)
        (auth / "auth.json").write_text(
            json.dumps({"fixture-gw": {"key": " sk-test-key \n"}}),
            encoding="utf-8",
        )
        # When/Then: base from gateway_urls, key from auth.json (stripped)
        cfg = get_provider_config("fixture-gw")
        assert cfg.base_url == "https://example.invalid/gw"
        assert cfg.api_key == "sk-test-key"

    def test_unknown_provider_error_names_declaration_sites(self, hr_env) -> None:
        # Given: an empty fixture environment (no block, no gateway, no auth)
        # When/Then: explicit ValueError telling where to declare the provider
        with pytest.raises(ValueError, match="gateway_urls.*auth\\.json|declare"):
            get_provider_config("no-such-provider")

    def test_gateway_urls_validates_shape(self, hr_env) -> None:
        # Given: malformed gateway_urls
        _write(hr_env["hr_home"], "fleet.yaml", "gateway_urls: [not, a, map]\n")
        # When/Then: ValueError naming the file
        with pytest.raises(ValueError, match="fleet\\.yaml"):
            gateway_urls()

    def test_project_config_wins_over_global(self, hr_env) -> None:
        # Given: a global provider block and a project block overriding it
        (hr_env["config_dir"] / "opencode.jsonc").write_text(
            json.dumps(
                {"provider": {"fixture-p": {"options": {"baseURL": "https://global.invalid"}}}}
            ),
            encoding="utf-8",
        )
        (hr_env["project"] / "opencode.jsonc").write_text(
            json.dumps(
                {
                    "provider": {
                        "fixture-p": {
                            "options": {
                                "baseURL": "https://project.invalid",
                                "apiKey": "sk-project-key",
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        # When: resolution runs through the merged config (provider blocks
        # merge per-provider, project block replaces the global one)
        cfg = get_provider_config("fixture-p")
        # Then: the project block's values win
        assert cfg.base_url == "https://project.invalid"
        assert cfg.api_key == "sk-project-key"


# ---------------------------------------------------------------------------
# 4. Recommend reads seat definitions from seats.yaml
# ---------------------------------------------------------------------------
class TestRecommendReadsSeatsYaml:
    def _write_seats(self, hr_env, codes: list[tuple[str, list[str] | None]]) -> None:
        body = "seats:\n"
        for code, primaries in codes:
            body += f"  - seat_code: {code}\n    domain: general\n"
            if primaries is not None:
                body += f"    primary_capabilities: {primaries}\n"
        _write(hr_env["hr_home"], "seats.yaml", body)

    def test_seat_specs_follow_fixture(self, hr_env) -> None:
        # Given: two seats in the fixture
        self._write_seats(hr_env, [("alpha", None), ("beta", ["code_gen"])])
        # When: load_seat_specs()
        seats = recommend.load_seat_specs()
        # Then: exactly the fixture seats
        assert {s["seat_code"] for s in seats} == {"alpha", "beta"}

    def test_add_or_remove_seat_changes_behavior(self, hr_env) -> None:
        # Given: no seats.yaml at all
        # When: load_seat_specs() -> [] (never crash)
        assert recommend.load_seat_specs() == []
        # Given: a removed seat (only one remains)
        self._write_seats(hr_env, [("omega", None)])
        # When/Then: behavior follows the file
        assert [s["seat_code"] for s in recommend.load_seat_specs()] == ["omega"]

    def test_primary_capabilities_drive_weights(self, hr_env) -> None:
        # Given: a seat with explicit primary_capabilities
        self._write_seats(hr_env, [("zeta", ["code_gen", "tool_use"])])
        seat = recommend.load_seat_specs()[0]
        # When: _seat_capability_weights()
        weights = recommend._seat_capability_weights(seat)
        # Then: one equal weight per declared capability — no role table
        assert weights == {str(BC.code_gen): 0.5, str(BC.tool_use): 0.5}

    def test_domain_fallback_without_primary_capabilities(self, hr_env) -> None:
        # Given: a seat with no primary_capabilities
        self._write_seats(hr_env, [("eta", None)])
        seat = recommend.load_seat_specs()[0]
        # When/Then: the domain fallback translation is used
        weights = recommend._seat_capability_weights(seat)
        assert weights == {str(recommend._DOMAIN_CATEGORY["general"]): 1.0}


# ---------------------------------------------------------------------------
# 5. itemrepo via HR_HOME
# ---------------------------------------------------------------------------
class TestItemrepoResolution:
    def test_default_is_hr_home_itemrepo(self, hr_env) -> None:
        # Given: HR_HOME with an itemrepo directory (staging presets it)
        # When: itemrepo_path()
        # Then: the HR_HOME-derived default
        assert itemrepo_path() == hr_env["hr_home"] / "itemrepo"

    def test_hr_itemrepo_override(self, hr_env, tmp_path: Path) -> None:
        # Given: an HR_ITEMREPO env override (directory exists)
        custom = tmp_path / "custom-items"
        custom.mkdir()
        import os

        os.environ["HR_ITEMREPO"] = str(custom)
        # When/Then: the override wins over HR_HOME/itemrepo
        assert itemrepo_path() == custom.resolve()

    def test_missing_dir_fails_loud_naming_resolution(self, hr_env) -> None:
        # Given: no override and the default HR_HOME/itemrepo does not exist
        (hr_env["hr_home"] / "itemrepo").rmdir()
        # When/Then: RuntimeError naming the resolution chain
        with pytest.raises(RuntimeError, match="HR_HOME/itemrepo|HR_ITEMREPO"):
            itemrepo_path()

    def test_config_path_respects_hr_home(self, hr_env) -> None:
        # Given: HR_HOME pinned by the fixture
        # When/Then: config files resolve under the pinned HR_HOME
        assert config_path("seats.yaml") == hr_env["hr_home"] / "configs" / "seats.yaml"
# 6. local-overlay mechanism (configs/<name>.local.yaml deep-merge)
# ---------------------------------------------------------------------------
class TestLocalOverlay:
    def _write(self, hr_home: Path, name: str, data: str) -> None:
        cfg = hr_home / "configs"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / name).write_text(data, encoding="utf-8")

    def test_local_wins_per_key_recursive_dicts(self, hr_env) -> None:
        from hr.config import load_yaml

        self._write(
            hr_env["hr_home"], "demo.yaml",
            "top:\n  a: 1\n  nested:\n    x: 1\n    y: 2\n",
        )
        self._write(
            hr_env["hr_home"], "demo.local.yaml",
            "top:\n  nested:\n    y: 20\n  b: 3\n",
        )
        data = load_yaml("demo.yaml")
        # overlay wins per key; base keys absent from the overlay survive
        assert data == {"top": {"a": 1, "nested": {"x": 1, "y": 20}, "b": 3}}

    def test_lists_replaced_not_merged(self, hr_env) -> None:
        from hr.config import load_yaml

        self._write(hr_env["hr_home"], "demo.yaml", "items:\n  - a\n  - b\n")
        self._write(hr_env["hr_home"], "demo.local.yaml", "items:\n  - z\n")
        # the overlay list REPLACES the base list, never appends
        assert load_yaml("demo.yaml") == {"items": ["z"]}

    def test_missing_local_overlay_ok(self, hr_env) -> None:
        from hr.config import load_yaml

        self._write(hr_env["hr_home"], "demo.yaml", "a: 1\n")
        # no demo.local.yaml -> the tracked file is used as-is
        assert load_yaml("demo.yaml") == {"a": 1}

    def test_overlay_without_base_file_still_raises(self, hr_env) -> None:
        from hr.config import load_yaml

        self._write(hr_env["hr_home"], "demo.local.yaml", "a: 1\n")
        with pytest.raises(FileNotFoundError, match="demo"):
            load_yaml("demo.yaml")

    def test_invalid_overlay_raises_naming_file(self, hr_env) -> None:
        from hr.config import load_yaml

        self._write(hr_env["hr_home"], "demo.yaml", "a: 1\n")
        self._write(hr_env["hr_home"], "demo.local.yaml", "- just\n- a list\n")
        with pytest.raises(ValueError, match="local overlay|demo.local.yaml"):
            load_yaml("demo.yaml")

    def test_fleet_overlay_restores_real_wires_and_gateways(self, hr_env) -> None:
        from hr.config import gateway_urls, load_yaml

        self._write(
            hr_env["hr_home"], "fleet.yaml",
            "wire_overrides:\n  example-provider: anthropic-compat\n"
            "gateway_urls:\n  example-provider: https://gateway.example.invalid/v1\n",
        )
        self._write(
            hr_env["hr_home"], "fleet.local.yaml",
            "wire_overrides:\n  registry-only-provider: anthropic-compat\n"
            "gateway_urls:\n  registry-only-provider: https://registry.example.invalid/v1\n",
        )
        merged = load_yaml("fleet.yaml")
        # dict merge: tracked example key survives, overlay key added
        assert "example-provider" in merged["wire_overrides"]
        assert merged["wire_overrides"]["registry-only-provider"] == "anthropic-compat"
        assert (
            gateway_urls()["registry-only-provider"]
            == "https://registry.example.invalid/v1"
        )

    def test_anchors_overlay_replaces_examples(self, hr_env) -> None:
        from hr.calibrate import load_anchors

        self._write(
            hr_env["hr_home"], "seats.yaml",
            "calibration_anchors:\n  cheap: example-provider/example-model-a\n",
        )
        self._write(
            hr_env["hr_home"], "seats.local.yaml",
            "calibration_anchors:\n  cheap: fixture-provider/fixture-model\n",
        )
        assert load_anchors() == {"cheap": "fixture-provider/fixture-model"}


# ---------------------------------------------------------------------------
# 8. Runtime output root (staging-workspace contract)
# ---------------------------------------------------------------------------


class TestOutputRoot:
    def test_env_override_wins(self, hr_env, monkeypatch) -> None:
        from hr.config import output_root

        out = hr_env["project"] / "artifacts"
        monkeypatch.setenv("HR_OUTPUT_DIR", str(out))
        assert output_root() == out.resolve()

    def test_xdg_cache_default_never_repo(self, hr_env, monkeypatch) -> None:
        from hr.config import output_root

        monkeypatch.delenv("HR_OUTPUT_DIR", raising=False)
        monkeypatch.setenv("XDG_CACHE_HOME", str(hr_env["home"] / ".cache"))
        root = output_root()
        assert root == (hr_env["home"] / ".cache" / "hr").resolve()
        # the staging workspace is outside the repo by construction — and so is
        # the resolved root
        from pathlib import Path

        repo = Path(__file__).resolve().parents[1]
        sandbox_tmp = hr_env["project"].parent
        assert sandbox_tmp.resolve().is_relative_to(repo) is False
        assert root.is_relative_to(repo) is False

    def test_home_fallback_under_sealed_home(self, hr_env, monkeypatch) -> None:
        from hr.config import output_root

        monkeypatch.delenv("HR_OUTPUT_DIR", raising=False)
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        root = output_root()
        assert root == (hr_env["home"] / ".cache" / "hr").resolve()
        assert root.is_relative_to(repo_root()) is False


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]
