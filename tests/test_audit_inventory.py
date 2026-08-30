from __future__ import annotations

from tests.test_audit_regressions import hr_env
from tests.test_audit_regressions import (
    _write,
    calibrate,
    hr_env,
    json,
    pytest,
    registry
)

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
