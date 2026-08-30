"""Model registry — a thin adapter over the dynamic fleet.

The registry owns no model data. The fleet inventory comes from
:mod:`hr.fleet` (opencode config provider blocks + ``configs/deployable.yaml``
extras, drift-guarded); per-model metadata is attached here from the merged
opencode provider blocks (``limit.context`` / ``contextWindow``,
``limit.output`` / ``maxOutputTokens``, ``attachment`` / ``modalities``,
``options.thinking``) plus the ``configs/models.yaml`` capability overlay;
the result is presented as :class:`~hr.models.ModelProfile`, the shape the
v1 seeders consume. A model added to the opencode config (or to
``extra_deployable``) flows into every registry consumer with zero edits in
this repo.

Consumers use these profiles for discovery and presentation.
"""

from __future__ import annotations

from hr import fleet
from hr.adapters.fleet import resolve_capabilities
from hr.models import ModelProfile
from hr.opencfg import read_providers


def _context_window(model_cfg: dict) -> int | None:
    limit = model_cfg.get("limit")
    if isinstance(limit, dict) and "context" in limit:
        return int(limit["context"])
    if "contextWindow" in model_cfg:
        return int(model_cfg["contextWindow"])
    return None


def _max_output(model_cfg: dict) -> int | None:
    limit = model_cfg.get("limit")
    if isinstance(limit, dict) and "output" in limit:
        return int(limit["output"])
    if "maxOutputTokens" in model_cfg:
        return int(model_cfg["maxOutputTokens"])
    return None


def _config_vision(model_cfg: dict) -> bool:
    if model_cfg.get("attachment") is True:
        return True
    modalities = model_cfg.get("modalities")
    return isinstance(modalities, dict) and "image" in (modalities.get("input") or [])


def _config_thinking(model_cfg: dict) -> bool:
    options = model_cfg.get("options")
    return isinstance(options, dict) and "thinking" in options


def discover_models() -> list[ModelProfile]:
    """A ModelProfile per model in the dynamic fleet (bare-slug ids).

    Fleet from :func:`hr.fleet.fleet_inventory`; vision/thinking resolve to
    the config metadata OR the ``configs/models.yaml`` capability overlay
    (namespaced ``provider/slug`` entries win over bare-slug ones). An empty
    fleet raises naming the searched config files — the registry never
    pretends a configured fleet exists when the config says otherwise.
    """
    providers = read_providers()
    profiles: list[ModelProfile] = []
    for fm in fleet.fleet_inventory():
        block = providers.get(fm.provider)
        model_cfg = {}
        if isinstance(block, dict):
            models_block = block.get("models")
            if isinstance(models_block, dict):
                entry = models_block.get(fm.model_id)
                if isinstance(entry, dict):
                    model_cfg = entry
        overlay = resolve_capabilities(f"{fm.provider}/{fm.model_id}")
        base_url = ""
        if isinstance(block, dict):
            options = block.get("options")
            if isinstance(options, dict):
                base_url = str(options.get("baseURL") or "")
        profiles.append(
            ModelProfile(
                provider=fm.provider,
                model_id=fm.model_id,
                display_name=fm.display_name,
                context_window=_context_window(model_cfg),
                max_output=_max_output(model_cfg),
                supports_vision=_config_vision(model_cfg) or overlay["vision"],
                supports_thinking=_config_thinking(model_cfg) or overlay["thinking"],
                api_base_url=base_url,
            )
        )
    return profiles


__all__ = ["discover_models"]
