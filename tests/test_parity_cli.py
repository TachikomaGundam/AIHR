"""hr parity fleet-source resolution: flags, fleet.yaml roster, precedence."""

from __future__ import annotations

from types import SimpleNamespace

from typer.testing import CliRunner

from hr.cli import app

runner = CliRunner()

_EMPTY_REPORT = SimpleNamespace(
    clean=["a-1"], drifted=[], incomparable=[], stale_manifest=None, seating_stale=False
)


def _capture_parity(monkeypatch) -> dict:
    from hr import calibration_items, parity

    seen: dict = {}

    def fake_check(anchors, ids):
        seen["ids"] = list(ids)
        return _EMPTY_REPORT

    monkeypatch.setattr(parity, "check_parity", fake_check)
    monkeypatch.setattr(calibration_items, "load_anchors", lambda: [])
    return seen


def _fleet_yaml(tmp_path, body: str) -> None:
    cfg = tmp_path / "configs"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "fleet.yaml").write_text(body, encoding="utf-8")


def test_parity_defaults_to_fleet_yaml_roster(tmp_path, monkeypatch):
    """191 ladder named gap (2026-11-02): repeated `hr parity --fleet-file …`
    calls on a box with a stable roster; fleet.yaml `fleet:` is the
    versioned default source (machine overlays win via the local-wins merge)."""
    seen = _capture_parity(monkeypatch)
    _fleet_yaml(tmp_path, "fleet:\n  - x/one\n  - y/two  # live anchor\n")
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    result = runner.invoke(app, ["parity"])
    assert result.exit_code == 0, result.output
    assert seen["ids"] == ["x/one", "y/two"]


def test_parity_flag_overrides_fleet_yaml(tmp_path, monkeypatch):
    seen = _capture_parity(monkeypatch)
    _fleet_yaml(tmp_path, "fleet:\n  - x/one\n")
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    result = runner.invoke(app, ["parity", "--fleet", "z/now"])
    assert result.exit_code == 0, result.output
    assert seen["ids"] == ["z/now"]


def test_parity_without_any_fleet_source_stays_rc2(tmp_path, monkeypatch):
    _capture_parity(monkeypatch)
    _fleet_yaml(tmp_path, "gateway_urls: {}\n")
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    result = runner.invoke(app, ["parity"])
    assert result.exit_code == 2
    assert "fleet:" in result.output
