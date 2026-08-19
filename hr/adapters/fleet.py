"""Data-driven provider routing + capability overlay.

Two questions the adapters used to answer with hardcoded heuristics:

- which wire does a provider speak? — derived at runtime from the opencode
  config (``provider.<name>.npm`` via the explicit table in :mod:`hr.fleet`)
  plus the OPTIONAL ``wire_overrides:`` map in ``configs/fleet.yaml``.
- what capabilities does a model have? — the ``capabilities:`` overlay in
  ``configs/models.yaml`` (knowledge data; anything unstated resolves to
  conservative False/False).

Routing contract: explicit derivation only — no model-name heuristics, no
implicit Anthropic fallthrough. A provider declared in the opencode config
whose npm is unknown and which has no override raises (fail loud, naming
the provider and where to declare its wire); a provider declared NOWHERE
resolves to None so the router can guide: ``declare it under
'wire_overrides:' in configs/fleet.yaml or add it to the opencode config
with a known 'npm'``. An unknown model resolves to conservative
False/False capabilities.
"""

from __future__ import annotations

from typing import Any

from hr import fleet
from hr.config import load_yaml

VALID_TYPES: tuple[str, ...] = fleet.VALID_TYPES

_MODELS_FILE = "models.yaml"

_DEFAULTS: dict[str, bool] = {"thinking": False, "vision": False}


def provider_type(provider: str) -> str | None:
    """Wire type of ``provider``: override first, else npm derivation.

    - provider covered by a ``wire_overrides:`` entry -> that wire;
    - provider declared in the opencode config -> its npm-derived wire
      (unknown npm raises — fail loud, never a guess);
    - provider declared nowhere -> None (the router prints the guidance).
    """
    overrides = fleet.read_overrides()
    override = overrides.wire_overrides.get(provider)
    if override is not None:
        return override
    providers = fleet.read_providers()
    block = providers.get(provider)
    if block is None:
        return None
    return fleet.resolve_wire(provider, block, overrides)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return bool(value)


def resolve_capabilities(model_id: str) -> dict[str, bool]:
    """Conservative capability overlay for ``model_id`` (never raises).

    Two-level lookup in ``configs/models.yaml``: an explicit
    ``capabilities[model_id]`` entry (namespaced overrides like
    ``bailian-token-plan/deepseek-v4-flash``) wins over the bare-slug entry;
    anything unstated defaults to thinking/vision False. An absent or
    malformed overlay never raises.
    """
    try:
        capabilities = load_yaml(_MODELS_FILE).get("capabilities")
    except FileNotFoundError:
        return dict(_DEFAULTS)
    if not isinstance(capabilities, dict):
        return dict(_DEFAULTS)
    entry = capabilities.get(model_id)
    if not isinstance(entry, dict):
        entry = capabilities.get(model_id.rsplit("/", 1)[-1])
    if not isinstance(entry, dict):
        return dict(_DEFAULTS)
    return {
        "thinking": _as_bool(entry.get("thinking", False)),
        "vision": _as_bool(entry.get("vision", False)),
    }