"""Capability overlay tests: models.yaml knowledge data (hr-unification todo 26).

The capability/pricing overlay lives in ``configs/models.yaml`` — the ONLY
overlay file (fleet.yaml carries no ``capabilities:`` section anymore; it is
overrides-only). These tests pin:

* override precedence: full ``provider/slug`` id > bare slug > defaults;
* unknown or malformed models resolve to conservative
  ``{thinking: False, vision: False}`` and never raise;
* provider wire routing is covered by ``tests/adapters/test_fleet_routing.py``
  (npm derivation + wire_overrides) — NOT by this file.

Pure offline: ``hr.config.config_path`` is redirected to a tmp configs/ dir,
no network and no DB involved.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hr.adapters.fleet import resolve_capabilities


@pytest.fixture
def configs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    hr_home = tmp_path / "hr"
    configs = hr_home / "configs"
    configs.mkdir(parents=True)
    monkeypatch.setenv("HR_HOME", str(hr_home))
    return configs


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# models.yaml overlay: id > slug > defaults
# ---------------------------------------------------------------------------
def test_full_id_entry_from_models_yaml(configs_dir: Path) -> None:
    _write_yaml(
        configs_dir / "models.yaml",
        {"capabilities": {"acme-ai/thinky": {"thinking": True, "vision": True}}},
    )
    assert resolve_capabilities("acme-ai/thinky") == {
        "thinking": True,
        "vision": True,
    }


def test_bare_slug_entry_applies_across_wires_from_models_yaml(configs_dir: Path) -> None:
    """A slug-keyed entry applies on every wire (provider prefix ignored)."""
    _write_yaml(
        configs_dir / "models.yaml",
        {"capabilities": {"kimi-k2.5": {"thinking": True, "vision": True}}},
    )
    assert resolve_capabilities("bailian-token-plan/kimi-k2.5") == {
        "thinking": True,
        "vision": True,
    }


def test_namespaced_override_beats_slug_from_models_yaml(configs_dir: Path) -> None:
    """The full provider/slug id wins over the bare-slug entry (per-wire
    capability data, e.g. deepseek-v4-flash thinking on the deepseek wire
    but not the bailian gateway)."""
    _write_yaml(
        configs_dir / "models.yaml",
        {
            "capabilities": {
                "deepseek-v4-flash": {"thinking": True, "vision": False},
                "bailian-token-plan/deepseek-v4-flash": {
                    "thinking": False,
                    "vision": False,
                },
            }
        },
    )
    assert resolve_capabilities("bailian-token-plan/deepseek-v4-flash") == {
        "thinking": False,
        "vision": False,
    }
    # another wire of the same slug keeps the slug-level values
    assert resolve_capabilities("deepseek/deepseek-v4-flash") == {
        "thinking": True,
        "vision": False,
    }


def test_unknown_model_defaults_to_conservative_false(configs_dir: Path) -> None:
    _write_yaml(configs_dir / "models.yaml", {"capabilities": {}})
    assert resolve_capabilities("acme-ai/mystery-model") == {
        "thinking": False,
        "vision": False,
    }


def test_missing_capabilities_section_defaults_to_false(configs_dir: Path) -> None:
    _write_yaml(configs_dir / "models.yaml", {"pricing": {}})
    assert resolve_capabilities("acme-ai/anything") == {
        "thinking": False,
        "vision": False,
    }


def test_non_dict_entry_treated_as_defaults(configs_dir: Path) -> None:
    _write_yaml(
        configs_dir / "models.yaml",
        {"capabilities": {"acme-ai/weird": "not-a-dict"}},
    )
    assert resolve_capabilities("acme-ai/weird") == {
        "thinking": False,
        "vision": False,
    }


def test_string_bool_values_coerced(configs_dir: Path) -> None:
    _write_yaml(
        configs_dir / "models.yaml",
        {"capabilities": {"model-a": {"thinking": "true", "vision": "no"}}},
    )
    assert resolve_capabilities("model-a") == {"thinking": True, "vision": False}


def test_missing_models_yaml_defaults_to_false(configs_dir: Path) -> None:
    assert resolve_capabilities("acme-ai/anything") == {
        "thinking": False,
        "vision": False,
    }
