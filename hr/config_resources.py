"""Configuration resource discovery and YAML overlay loading."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml


def hr_home() -> Path:
    """Resolve resources from an override, source checkout, or installation."""
    env = os.environ.get("HR_HOME")
    if env:
        return Path(env).expanduser().resolve()
    source_root = Path(__file__).resolve().parent.parent
    if (source_root / "configs").is_dir():
        return source_root
    target_root = source_root / "share" / "hr-agent"
    if (target_root / "configs").is_dir():
        return target_root
    installed_root = Path(sys.prefix).resolve() / "share" / "hr-agent"
    if (installed_root / "configs").is_dir():
        return installed_root
    return source_root


def config_path(name: str) -> Path:
    return hr_home() / "configs" / name


def _local_overlay_path(name: str) -> Path:
    suffix = ".yaml"
    if not name.endswith(suffix):
        raise ValueError(f"overlay only supported for *.yaml configs: {name!r}")
    return config_path(name[: -len(suffix)] + ".local.yaml")


def _deep_merge(base: dict, overlay: dict) -> dict:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_yaml(name: str) -> dict:
    """Load a tracked YAML config with an optional local-wins overlay."""
    path = config_path(name)
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    overlay_path = _local_overlay_path(name)
    if overlay_path.exists():
        with overlay_path.open("r", encoding="utf-8") as stream:
            extra = yaml.safe_load(stream) or {}
        if not isinstance(extra, dict):
            raise ValueError(
                f"invalid local overlay (must be a mapping): {overlay_path}"
            )
        data = _deep_merge(data, extra)
    return data


def opencode_config_dir() -> Path:
    env = os.environ.get("OPENCODE_CONFIG_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".config" / "opencode"
