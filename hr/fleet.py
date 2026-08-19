"""Model fleet — derived at RUNTIME from opencode's live config.

The fleet is not a snapshot file. Every model, wire type and display name
is derived from the ``opencode.jsonc`` provider blocks opencode itself
loads (global config dir + the project's ``opencode.jsonc`` /
``.opencode/opencode.jsonc``, project merged over global — the same
precedence FastDraw's origins.ts implements; file reading lives in
:mod:`hr.opencfg`). Add a model to opencode's config and it flows into
the stage sweep pools, discover enumeration and adapter routing with
ZERO edits in this repo.

Wire types come from the provider's ``npm`` field via an explicit mapping
table (:data:`NPM_WIRE`); an npm-less / unknown-npm config provider is a
FAIL-LOUD error naming the provider and where to declare its wire — the
wire is never guessed. Providers opencode does not declare (registry-only
ones like ``kimi-for-coding`` / ``deepseek``, whose configs live in
opencode's own registry) get their wire from ``configs/fleet.yaml``
``wire_overrides:`` — and their MODELS from ``configs/deployable.yaml``
``extra_deployable:``, the only hand-maintained model list left (user
policy for externally-served models; an extra duplicating a config model
is rejected as drift).

``configs/fleet.yaml`` is now an OPTIONAL overrides-only file
(``wire_overrides:``, ``scope_excludes:``); ``configs/models.yaml`` holds
pricing + capability knowledge data with safe defaults for unknown models.

Consumers:
* ``hr.stage0`` / ``hr.stage1`` — sweep pool via :func:`fleet_models`;
* ``hr.discover`` — enumeration + default scope (all discovered providers
  minus ``scope_excludes``);
* ``hr.adapters.fleet`` — provider wire routing;
* ``hr.deployable`` — deployable set (config models + extras, drift-checked).
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

from hr.config import load_yaml
from hr.opencfg import opencode_config_files, parse_config_file, read_providers

_FLEET_LABEL = "configs/fleet.yaml"
_EXTRA_LABEL = "configs/deployable.yaml"

#: Known opencode provider npm packages -> hr wire type (explicit table; a
#: package outside it is an error, never a guess).
NPM_WIRE: dict[str, str] = {
    "@ai-sdk/anthropic": "anthropic-compat",
    "@ai-sdk/openai": "openai-compat",
    "@ai-sdk/openai-compat": "openai-compat",
    "@ai-sdk/openai-compatible": "openai-compat",
}

VALID_TYPES: tuple[str, ...] = ("openai-compat", "anthropic-compat")


@dataclass(frozen=True)
class FleetOverrides:
    """Optional policy overrides from ``configs/fleet.yaml`` (both sections
    optional; the whole file may be absent)."""

    wire_overrides: dict[str, str]  # provider -> wire when npm can't derive it
    scope_excludes: frozenset[str]  # providers kept out of the default scope


def empty_overrides() -> FleetOverrides:
    return FleetOverrides(wire_overrides={}, scope_excludes=frozenset())


def read_overrides() -> FleetOverrides:
    """``configs/fleet.yaml`` overrides; an absent file means no overrides.

    Malformed YAML or an invalid shape raises ValueError naming the file —
    explicit config validation, never a silent fallback.
    """
    try:
        data = load_yaml("fleet.yaml")
    except FileNotFoundError:
        return empty_overrides()
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid {_FLEET_LABEL}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"invalid {_FLEET_LABEL}: top level is not an object")
    return _overrides_from(data)


def _overrides_from(data: dict) -> FleetOverrides:
    wire = data.get("wire_overrides") or {}
    if not isinstance(wire, dict):
        raise ValueError(
            f"invalid {_FLEET_LABEL}: 'wire_overrides' must be a map of "
            "provider -> wire type"
        )
    wires: dict[str, str] = {}
    for provider, value in wire.items():
        wire_type = str(value)
        if wire_type not in VALID_TYPES:
            raise ValueError(
                f"invalid {_FLEET_LABEL}: wire for provider {provider!r} is "
                f"{wire_type!r}; valid types: {', '.join(VALID_TYPES)}"
            )
        wires[str(provider)] = wire_type

    excludes_raw = data.get("scope_excludes") or []
    if isinstance(excludes_raw, dict):
        excludes_raw = [p for p, v in excludes_raw.items() if v]
    if not isinstance(excludes_raw, list) or not all(
        isinstance(p, str) for p in excludes_raw
    ):
        raise ValueError(
            f"invalid {_FLEET_LABEL}: 'scope_excludes' must be a list of "
            "provider ids"
        )
    return FleetOverrides(
        wire_overrides=wires, scope_excludes=frozenset(excludes_raw)
    )


def resolve_wire(provider: str, block: dict, overrides: FleetOverrides) -> str:
    """Wire type for a config-declared provider: ``wire_overrides`` first,
    else the ``npm`` mapping. Unknown npm with no override raises (fail
    loud) naming the provider and where to declare its wire — never a guess.
    """
    override = overrides.wire_overrides.get(provider)
    if override is not None:
        return override
    npm = block.get("npm")
    if isinstance(npm, str) and npm in NPM_WIRE:
        return NPM_WIRE[npm]
    raise ValueError(
        f"cannot derive wire type for provider {provider!r}: npm field is "
        f"{npm!r}, not in the known table ({', '.join(sorted(NPM_WIRE))}) "
        f"and no override is declared — add 'wire_overrides:' entry for "
        f"{provider!r} in {_FLEET_LABEL} (or fix the provider's 'npm' in "
        "the opencode config)"
    )


@dataclass(frozen=True)
class FleetModel:
    """One model in the dynamic fleet (id convention: ``provider/model_id``)."""

    provider: str
    model_id: str  # slug within the provider
    display_name: str
    wire: str
    external: bool  # served outside opencode config (extra_deployable policy)


def provider_display_names(providers: dict[str, dict] | None = None) -> dict[str, str]:
    """provider -> display name from the config's ``name`` field (id as
    fallback). Registry-only providers are absent — callers fall back."""
    providers = read_providers() if providers is None else providers
    return {
        pid: (
            str(block["name"])
            if isinstance(block.get("name"), str) and block["name"].strip()
            else pid
        )
        for pid, block in providers.items()
    }


def discovered_models(
    providers: dict[str, dict] | None = None, overrides: FleetOverrides | None = None
) -> list[FleetModel]:
    """Every ``provider.models[*]`` entry of the merged config as FleetModel.

    Providers without a ``models`` block contribute nothing. An unresolvable
    wire raises (fail loud) — the inventory never carries a model whose
    routing is unknown.
    """
    providers = read_providers() if providers is None else providers
    overrides = read_overrides() if overrides is None else overrides
    names = provider_display_names(providers)
    models: list[FleetModel] = []
    for pid, block in providers.items():
        if not isinstance(block, dict):
            continue
        models_block = block.get("models")
        if not isinstance(models_block, dict) or not models_block:
            continue
        wire = resolve_wire(pid, block, overrides)
        for slug, entry in models_block.items():
            if not isinstance(entry, dict):
                continue
            models.append(
                FleetModel(
                    provider=pid,
                    model_id=slug,
                    display_name=str(entry.get("name") or slug),
                    wire=wire,
                    external=False,
                )
            )
    models.sort(key=lambda m: (m.provider, m.model_id))
    return models


def extra_models(
    overrides: FleetOverrides | None = None,
    providers: dict[str, dict] | None = None,
) -> list[FleetModel]:
    """``configs/deployable.yaml`` ``extra_deployable:`` entries (user policy
    for models served outside opencode config). Wire resolves like any other
    model: override first, else the provider's npm when it is config-declared,
    else fail loud (a routable wire is never guessed)."""
    try:
        data = load_yaml("deployable.yaml")
    except FileNotFoundError:
        return []
    overrides = read_overrides() if overrides is None else overrides
    providers = read_providers() if providers is None else providers
    extras = data.get("extra_deployable")
    if not isinstance(extras, list):
        return []
    models: list[FleetModel] = []
    for raw in extras:
        if not isinstance(raw, str) or "/" not in raw:
            raise ValueError(
                f"invalid {_EXTRA_LABEL}: extra_deployable entry {raw!r} is "
                "not a 'provider/model_id' string"
            )
        pid, slug = raw.split("/", 1)
        block = providers.get(pid)
        if block is not None:
            wire = resolve_wire(pid, block, overrides)
        else:
            wire = overrides.wire_overrides.get(pid)
            if wire is None:
                raise ValueError(
                    f"cannot derive wire type for extra model {raw!r}: provider "
                    f"{pid!r} is not declared in the opencode config and has no "
                    f"'wire_overrides:' entry in {_FLEET_LABEL}"
                )
        models.append(
            FleetModel(
                provider=pid,
                model_id=slug,
                display_name=slug,
                wire=wire,
                external=True,
            )
        )
    models.sort(key=lambda m: (m.provider, m.model_id))
    return models


def merge_with_extras(
    discovered: list[FleetModel],
    extras: list[FleetModel],
) -> list[FleetModel]:
    """Config models + extra models with the drift guard: an extra that
    duplicates a config-discovered model is a sync error, rejected loudly
    (the model must be deleted from ``extra_deployable`` instead)."""
    discovered_ids = {f"{m.provider}/{m.model_id}" for m in discovered}
    dupes = sorted(
        f"{m.provider}/{m.model_id}" for m in extras if f"{m.provider}/{m.model_id}" in discovered_ids
    )
    if dupes:
        raise ValueError(
            f"drift: extra_deployable entries duplicate models already "
            f"declared in the opencode config: {', '.join(dupes)} — remove "
            f"them from configs/deployable.yaml (they are derived "
            "automatically)"
        )
    return sorted(discovered + extras, key=lambda m: (m.provider, m.model_id))


def fleet_inventory() -> list[FleetModel]:
    """The full dynamic fleet: config-discovered models + extra policy.

    A completely empty result is a config error (nothing to sweep) — raises
    naming the searched config paths instead of silently returning nothing.
    """
    overrides = read_overrides()
    providers = read_providers()
    merged = merge_with_extras(
        discovered_models(providers=providers, overrides=overrides),
        extra_models(overrides=overrides, providers=providers),
    )
    if not merged:
        searched = ", ".join(str(p) for p in opencode_config_files())
        raise ValueError(
            "empty model fleet: no models found in the opencode config files "
            f"({searched}) and no extra_deployable entries — check "
            "OPENCODE_CONFIG_DIR / the global opencode config"
        )
    return merged


def fleet_models() -> tuple[str, ...]:
    """Sorted ids (``provider/model_id``) of the full dynamic fleet — the
    stage engines' sweep pool.

    Derived at CALL time (config files re-read each call), so a model added
    to opencode's config needs zero edits here: the next call includes it.
    """
    return tuple(f"{m.provider}/{m.model_id}" for m in fleet_inventory())


__all__ = [
    "FleetModel",
    "FleetOverrides",
    "NPM_WIRE",
    "VALID_TYPES",
    "discovered_models",
    "empty_overrides",
    "extra_models",
    "fleet_inventory",
    "fleet_models",
    "merge_with_extras",
    "provider_display_names",
    "read_overrides",
    "resolve_wire",
]