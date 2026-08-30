from __future__ import annotations

from tests.test_audit_regressions import hr_env
from tests.test_audit_regressions import (
    BC,
    Path,
    _write,
    config_path,
    gateway_urls,
    get_provider_config,
    hr_env,
    itemrepo_path,
    json,
    pytest,
    recommend
)

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
