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

from hr import fleet  # noqa: F401 (re-export; consumed by sibling test modules)

from hr.stage0 import build_call_plan, select_subsets  # noqa: F401 (re-export; consumed by sibling test modules)

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

def load_item_repo_batteries() -> dict:
    from hr.calibrate import load_item_repo
    from hr.stage0 import STAGE0_BATTERIES

    return load_item_repo(
        Path(__file__).resolve().parents[1] / "itemrepo",
        batteries=list(STAGE0_BATTERIES),
    )

_PROJECT_GLOBAL = {
    "alpha": {"npm": "@ai-sdk/anthropic", "models": {"m1": {}}},
    "shared": {"npm": "@ai-sdk/openai", "models": {"base": {}}},
}
