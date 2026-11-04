"""Three-layer config overlay: tracked < install-local < user-dir (upgrade-proof)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hr import config_resources


@pytest.fixture
def layers(tmp_path, monkeypatch):
    install = tmp_path / "install"
    (install / "configs").mkdir(parents=True)
    user = tmp_path / "aihr-home"
    (user / "configs").mkdir(parents=True)
    monkeypatch.setenv("HR_HOME", str(install))
    monkeypatch.setenv("AIHR_HOME_DIR", str(user))
    return install, user


def _write(dir_: Path, name: str, body: str) -> None:
    (dir_ / "configs" / name).write_text(body, encoding="utf-8")


def _demo(install: Path) -> None:
    _write(install, "demo.yaml", "top:\n  a: 1\n  b: base\nlist: [base]\n")


def test_user_dir_overlay_loads_without_install_side(layers):
    install, user = layers
    _demo(install)
    _write(user, "demo.local.yaml", "top:\n  b: user\n")
    assert config_resources.load_yaml("demo.yaml")["top"] == {"a": 1, "b": "user"}


def test_user_dir_layer_beats_install_layer(layers):
    install, user = layers
    _demo(install)
    _write(install, "demo.local.yaml", "top:\n  b: install\n  c: 3\n")
    _write(user, "demo.local.yaml", "top:\n  b: user\n")
    merged = config_resources.load_yaml("demo.yaml")
    assert merged["top"] == {"a": 1, "b": "user", "c": 3}


def test_machine_truth_survives_install_replacement(layers, tmp_path):
    install, user = layers
    _demo(install)
    _write(user, "demo.local.yaml", "top:\n  b: kept-through-upgrade\n")
    # simulate pip install -U: the install tree is replaced wholesale
    import shutil

    shutil.rmtree(install)
    fresh = install / "fresh"  # new prefix path, same role
    (fresh / "configs").mkdir(parents=True)
    _write(fresh, "demo.yaml", "top:\n  a: 1\n  b: base\nlist: [base]\n")
    from hr import config_resources as cr

    # re-point HR_HOME at the fresh tree (what a new site-packages means)
    import os

    os.environ["HR_HOME"] = str(fresh)
    data = cr.load_yaml("demo.yaml")
    assert data["top"]["b"] == "kept-through-upgrade"


def test_missing_tracked_still_raises_with_name(layers):
    with pytest.raises(FileNotFoundError, match="demo.yaml"):
        config_resources.load_yaml("demo.yaml")


def test_user_overlay_must_be_a_mapping(layers):
    install, user = layers
    _demo(install)
    _write(user, "demo.local.yaml", "- not a mapping\n")
    with pytest.raises(ValueError, match="mapping"):
        config_resources.load_yaml("demo.yaml")


def test_keyed_seats_merge_applies_to_user_layer(layers):
    install, user = layers
    _write(install, "seats.yaml", "seats:\n  - seat_code: alpha\n    tier: 1\n")
    _write(user, "seats.local.yaml", "seats:\n  - seat_code: alpha\n    tier: 2\n  - seat_code: beta\n    tier: 1\n")
    seats = {s["seat_code"]: s for s in config_resources.load_yaml("seats.yaml")["seats"]}
    assert seats["alpha"]["tier"] == 2 and seats["beta"]["tier"] == 1


def test_overlay_paths_ordering(layers):
    install, user = layers
    paths: Any = config_resources._local_overlay_paths("demo.yaml")
    assert paths[0] == install / "configs" / "demo.local.yaml"
    assert paths[1] == user / "configs" / "demo.local.yaml"


def test_preflight_config_sees_user_layer(tmp_path, monkeypatch):
    install = tmp_path / "install"
    (install / "configs").mkdir(parents=True)
    user = tmp_path / "aihr-home"
    (user / "configs").mkdir(parents=True)
    monkeypatch.setenv("HR_HOME", str(install))
    monkeypatch.setenv("AIHR_HOME_DIR", str(user))
    import shutil
    from pathlib import Path
    repo_cfg = Path(__file__).resolve().parent.parent / "configs" / "preflight.yaml"
    shutil.copy(repo_cfg, install / "configs" / "preflight.yaml")
    (user / "configs" / "preflight.local.yaml").write_text(
        "models: [userbox/anchor-x]\n", encoding="utf-8"
    )
    from hr.preflight import load_preflight_config

    cfg = load_preflight_config()
    assert list(cfg.default_models) == ["userbox/anchor-x"]
