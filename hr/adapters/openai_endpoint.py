from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from hr.adapters.base import AdapterError
from hr.adapters.fleet import VALID_TYPES, provider_type
from hr.opencode_auth import provider_api_key


@dataclass(frozen=True)
class Endpoint:
    provider: str
    url: str
    headers: dict[str, str]


def provider_for(model_id: str) -> str:
    provider = model_id.split("/", 1)[0] if "/" in model_id else model_id
    wire = provider_type(provider)
    if wire is None:
        raise ValueError(
            f"no provider type configured for {provider!r} (from model_id "
            f"{model_id!r}); valid types: {', '.join(VALID_TYPES)} — declare "
            "a 'wire_overrides:' entry in configs/fleet.yaml or add the "
            "provider to the opencode config with a known 'npm'"
        )
    if wire != "openai-compat":
        raise ValueError(
            f"provider {provider!r} is typed {wire!r}, not 'openai-compat' "
            f"(from model_id {model_id!r}); valid types: "
            f"{', '.join(VALID_TYPES)}"
        )
    return provider


def resolve_endpoint(
    model_id: str,
    *,
    config_path: str,
    auth_path: str,
    base_url_override: str | None = None,
) -> Endpoint:
    provider = provider_for(model_id)
    base = base_url_override
    if not base:
        base = _api_base_from_config(provider, config_path)
    if not base:
        base = _api_base_from_opencode_config(provider)
    if not base:
        base = _api_base_from_gateway_urls(provider)
    if not base:
        raise AdapterError(
            f"No base URL declared for provider {provider!r} (from model "
            f"{model_id!r}). Declare it in one of: the provider block "
            f"'options.baseURL' in opencode.jsonc; 'api'/'baseURL' for "
            f"{provider!r} in the opencode models cache at {config_path}; "
            f"or 'gateway_urls:' for {provider!r} in configs/fleet.yaml"
        )
    api_key = _api_key_from_auth(provider, auth_path)
    return Endpoint(
        provider=provider,
        url=base.rstrip("/") + "/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )


def provider_entry(config: dict, provider: str) -> dict | None:
    nested = (config.get("providers") or {}).get(provider)
    if nested:
        return nested
    top = config.get(provider)
    return top if isinstance(top, dict) else None


def _api_base_from_config(provider: str, config_path: str) -> str | None:
    try:
        with open(config_path, encoding="utf-8") as config_file:
            config = json.load(config_file)
    except (OSError, json.JSONDecodeError):
        return None
    entry = provider_entry(config, provider) or {}
    base = entry.get("api") or entry.get("baseURL")
    return base if isinstance(base, str) and base.strip() else None


def _api_base_from_opencode_config(provider: str) -> str | None:
    from hr import opencfg

    try:
        blocks = opencfg.read_providers()
    except (OSError, json.JSONDecodeError):
        return None
    options = (blocks.get(provider) or {}).get("options") or {}
    base = options.get("baseURL")
    return base if isinstance(base, str) and base.strip() else None


def _api_base_from_gateway_urls(provider: str) -> str | None:
    from hr import config

    try:
        urls = config.gateway_urls()
    except FileNotFoundError:
        return None
    base = urls.get(provider)
    return base if isinstance(base, str) and base.strip() else None


def _api_key_from_auth(provider: str, auth_path: str) -> str:
    """API key for ``provider`` from the opencode auth files.

    Resolution is provider-level, auth-v2 first: if ``auth-v2.json`` exists
    BESIDE ``auth_path`` (same data dir) and yields a usable api key for the
    provider, that wins; otherwise the provider entry in ``auth_path``
    itself (the legacy auth.json). An oauth ``token`` never qualifies. The
    injected-``auth_path`` signature is preserved so tests keep injecting
    fakes; both files are named in every error.
    """
    v2_key = provider_api_key(provider, data_dir=Path(auth_path).parent)
    if v2_key is not None:
        return v2_key
    try:
        with open(auth_path, encoding="utf-8") as auth_file:
            auth = json.load(auth_file)
    except OSError as exc:
        raise AdapterError(
            f"Cannot read auth.json at {auth_path} (no usable auth-v2.json "
            f"key beside it): {exc}"
        ) from exc
    entry = auth.get(provider)
    if not isinstance(entry, dict):
        raise AdapterError(
            f"No auth entry for provider '{provider}' in auth-v2.json beside "
            f"{auth_path} or in {auth_path}"
        )
    key = entry.get("key")
    if not isinstance(key, str) or not key:
        raise AdapterError(
            f"Auth entry for '{provider}' in {auth_path} has no 'key' field "
            f"(auth-v2.json beside it yielded no key either)"
        )
    return key.strip()
