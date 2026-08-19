"""hr2.deployable — the set of currently SERVED (deployable) models.

"iron rule 5": a verdict must never assign a model that cannot actually be
deployed. The deployable set is derived at runtime:

* every model under ``provider.*.models`` in the opencode config
  (``OPENCODE_CONFIG_DIR`` env > ``~/.config/opencode``, the same path the
  rest of hr resolves via :func:`hr.config.opencode_config_dir`);
* plus ``extra_deployable:`` in ``configs/deployable.yaml`` — the ONLY
  hand-maintained model list, carrying models served OUTSIDE the opencode
  config (registry-only providers like kimi-for-coding / deepseek). An
  extra entry that duplicates a config-declared model is rejected as drift
  (the model must be removed from the YAML, not duplicated).

The returned ids use the DB convention ``{provider}/{model_id}`` so a sweep's
queried ``run.model_id`` values compare directly against this set.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union

import yaml

from hr import fleet
from hr.config import config_path, load_yaml, opencode_config_dir


def strip_jsonc_comments(text: str) -> str:
    """Remove // and /* */ comments from JSONC text, preserving string values.

    Scans char by char, tracking string state (with backslash escapes), so
    comment markers inside string literals (e.g. a URL containing "//") are
    left untouched.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    quote = ""
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_string:
            out.append(ch)
            if ch == "\\":
                out.append(nxt)
                i += 2
                continue
            if ch == quote:
                in_string = False
            i += 1
            continue
        if ch == '"' or ch == "'":
            in_string = True
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i < n and not (text[i] == "*" and text[i + 1 : i + 2] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _models_from_config(path: Path) -> set[str]:
    """Every ``provider/{slug}`` id declared by one opencode config file."""
    data = fleet.parse_config_file(path)
    providers = data.get("provider") or {}
    return {
        f"{pid}/{slug}"
        for pid, pconf in providers.items()
        for slug in (pconf.get("models") or {})
        if isinstance(pconf, dict)
    }


def _extra_ids(path: Path) -> set[str]:
    """extra_deployable ids from one YAML file (empty when absent)."""
    if not path.is_file():
        return set()
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return _extra_ids_from_data(data)


def _extra_ids_from_data(data: dict) -> set[str]:
    """extra_deployable ids from a parsed YAML mapping (empty when absent)."""
    extras = data.get("extra_deployable") or []
    return {str(item) for item in extras if isinstance(item, str)}


def load_deployable(
    opencode_jsonc_path: Optional[Union[str, os.PathLike]] = None,
    extra_config_path: Optional[Union[str, os.PathLike]] = None,
) -> set[str]:
    """Return every currently-served model id (``{provider}/{model_id}``).

    Union of two sources, both drift-checked:

    * ``opencode_jsonc_path`` (default ``<opencode config dir>/opencode.jsonc``)
      — every model under every ``provider.{id}.models`` entry. Missing file
      is an error: this machine always has it, and a silent empty set would
      retire every model in the fleet.
    * ``extra_config_path`` (default ``configs/deployable.yaml``) — models
      served outside opencode.jsonc. A missing YAML only means "no extras";
      an extra duplicating a config-declared model raises (drift).
    """
    oc_path = Path(
        str(opencode_jsonc_path)
        if opencode_jsonc_path is not None
        else opencode_config_dir() / "opencode.jsonc"
    )
    if not oc_path.is_file():
        raise FileNotFoundError(
            f"deployable source missing: {oc_path} "
            "(refusing to treat the fleet as empty — pass an explicit path "
            "in tests or restore the file)"
        )
    discovered = _models_from_config(oc_path)

    if extra_config_path is None:
        # Default path goes through hr.config.load_yaml so the gitignored
        # configs/deployable.local.yaml overlay (real extra_deployable list)
        # deep-merges over the tracked file, which ships an empty list.
        # A missing tracked YAML still means "no extras" (historic contract).
        extra_path = config_path("deployable.yaml")
        extras_raw = (
            _extra_ids_from_data(load_yaml("deployable.yaml"))
            if extra_path.is_file()
            else set()
        )
    else:
        extra_path = Path(str(extra_config_path))
        extras_raw = _extra_ids(extra_path)
    extras: list[fleet.FleetModel] = []
    for raw in sorted(extras_raw):
        pid, slug = raw.split("/", 1)
        extras.append(
            fleet.FleetModel(
                provider=pid,
                model_id=slug,
                display_name=slug,
                wire="",
                external=True,
            )
        )
    merged = fleet.merge_with_extras(
        [
            fleet.FleetModel(
                provider=pid,
                model_id=slug,
                display_name=slug,
                wire="",
                external=False,
            )
            for pid, slug in (item.split("/", 1) for item in sorted(discovered))
        ],
        extras,
    )
    return {f"{m.provider}/{m.model_id}" for m in merged}


__all__ = ["load_deployable", "strip_jsonc_comments"]