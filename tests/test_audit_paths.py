from __future__ import annotations

from tests.test_audit_regressions import hr_env
from tests.test_audit_regressions import (
    Path,
    gateway_urls,
    hr_env,
    pytest,
    repo_root
)

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
