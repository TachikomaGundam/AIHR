"""Every tracked shipped config must ride the wheel (share/aihr/configs).

The 0.6.9 pip-install smoke found preflight.yaml missing from
[tool.setuptools.data-files] (added after the list was written, invisible
in repo/HR_HOME installs). This pin makes that class of miss fail here,
not on a customer's box.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _shipped_configs() -> set[str]:
    with (REPO / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    entries = data["tool"]["setuptools"]["data-files"]["share/aihr/configs"]
    return {Path(e).name for e in entries}


def test_all_tracked_configs_are_shipped():
    tracked = {
        p.name for p in (REPO / "configs").glob("*.yaml")
        if not p.name.endswith(".local.yaml")
    }
    missing = tracked - _shipped_configs()
    assert not missing, f"configs on disk but not in data-files: {sorted(missing)}"
