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

from hr.models import BenchmarkCategory as BC  # noqa: F401 (re-export; consumed by sibling test modules)

import json  # noqa: F401 (re-export; consumed by sibling test modules)

from pathlib import Path

import pytest

import hr.calibrate as calibrate  # noqa: F401 (re-export; consumed by sibling test modules)

import hr.recommend as recommend  # noqa: F401 (re-export; consumed by sibling test modules)

import hr.registry as registry  # noqa: F401 (re-export; consumed by sibling test modules)

from hr.config import (
    config_path,  # noqa: F401 (re-export; consumed by sibling test modules)
    gateway_urls,  # noqa: F401 (re-export; consumed by sibling test modules)
    get_provider_config,  # noqa: F401 (re-export; consumed by sibling test modules)
    itemrepo_path,  # noqa: F401 (re-export; consumed by sibling test modules)
)


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

def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]
