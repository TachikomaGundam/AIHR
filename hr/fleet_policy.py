"""Provider wire derivation and fleet policy overrides."""

from __future__ import annotations

from dataclasses import dataclass

import yaml

from hr.config import load_yaml


_FLEET_LABEL = "configs/fleet.yaml"

NPM_WIRE: dict[str, str] = {
    "@ai-sdk/anthropic": "anthropic-compat",
    "@ai-sdk/openai": "openai-compat",
    "@ai-sdk/openai-compat": "openai-compat",
    "@ai-sdk/openai-compatible": "openai-compat",
}

VALID_TYPES: tuple[str, ...] = ("openai-compat", "anthropic-compat")


@dataclass(frozen=True)
class FleetOverrides:
    wire_overrides: dict[str, str]
    scope_excludes: frozenset[str]


def empty_overrides() -> FleetOverrides:
    return FleetOverrides(wire_overrides={}, scope_excludes=frozenset())


def read_overrides() -> FleetOverrides:
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
        excludes_raw = [provider for provider, enabled in excludes_raw.items() if enabled]
    if not isinstance(excludes_raw, list) or not all(
        isinstance(provider, str) for provider in excludes_raw
    ):
        raise ValueError(
            f"invalid {_FLEET_LABEL}: 'scope_excludes' must be a list of provider ids"
        )
    return FleetOverrides(
        wire_overrides=wires,
        scope_excludes=frozenset(excludes_raw),
    )


def resolve_wire(provider: str, block: dict, overrides: FleetOverrides) -> str:
    override = overrides.wire_overrides.get(provider)
    if override is not None:
        return override
    npm = block.get("npm")
    if isinstance(npm, str) and npm in NPM_WIRE:
        return NPM_WIRE[npm]
    raise ValueError(
        f"cannot derive wire type for provider {provider!r}: npm field is "
        f"{npm!r}, not in the known table ({', '.join(sorted(NPM_WIRE))}) "
        f"and no override is declared - add 'wire_overrides:' entry for "
        f"{provider!r} in {_FLEET_LABEL} (or fix the provider's 'npm' in "
        "the opencode config)"
    )
