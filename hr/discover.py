"""FastDraw-style enumeration of opencode providers/models for ``hr discover``.

The FastDraw TUI builds its model list from opencode's live runtime state
(``api.state.provider``). ``hr`` runs OUTSIDE opencode, so there is no
runtime state to read — the static equivalent is to parse the config files
themselves (same files, same precedence):

* global ``~/.config/opencode/opencode.jsonc`` (``OPENCODE_CONFIG_DIR``
  wins) plus the project ``opencode.jsonc`` / ``.opencode/opencode.jsonc``,
  project merged over global (the FastDraw origins precedence) —
  JSONC-tolerant parse via :mod:`hr.fleet`;
* auth presence from ``~/.local/share/opencode/auth-v2.json`` (falling back
  to ``auth.json``), treating an inline ``options.apiKey`` in the config as
  auth-present too;
* scope = every discovered provider minus the OPTIONAL ``scope_excludes:``
  list in ``configs/fleet.yaml`` (overrides-only file; new providers
  auto-inherit the default scope).

Documented limitation: npm-spec / remote-registry providers that live
ONLY outside the config files (e.g. ``kimi-for-coding`` / ``deepseek``,
which opencode loads from its own registry and whose keys sit in auth files)
are NOT enumerated — a static parse cannot see them. ``hr discover`` covers
precisely what ``opencode.jsonc`` declares; stage fleets sweep those
registry models via ``configs/deployable.yaml`` ``extra_deployable:``
(:func:`hr.fleet.fleet_models`).

Writes go to ``hr.provider`` / ``hr.model`` ONLY (idempotent
``ON CONFLICT DO NOTHING`` upserts) — no legacy v1 model table is ever
written from the discover path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from hr import fleet


def read_auth_providers() -> set[str]:
    """Provider ids with a credential in auth-v2.json (fallback auth.json).

    Paths resolve at CALL time (``Path.home()``), so tests can redirect
    HOME. auth-v2.json marks presence via its ``accounts`` / ``active``
    maps; the legacy auth.json marks a provider by a top-level key.
    """
    data_dir = Path.home() / ".local" / "share" / "opencode"
    auth_v2 = _read_auth_file(data_dir / "auth-v2.json")
    if auth_v2:
        present: set[str] = set()
        for section in ("accounts", "active"):
            entries = auth_v2.get(section)
            if isinstance(entries, dict):
                present.update(
                    pid for pid, payload in entries.items()
                    if _entry_has_credential(payload)
                )
        return present
    auth = _read_auth_file(data_dir / "auth.json")
    return {
        pid for pid, payload in auth.items() if isinstance(payload, dict)
    }


def _read_auth_file(path: Path) -> dict:
    """Read one auth file ({} when missing or unparseable)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _entry_has_credential(payload: object) -> bool:
    """Whether an auth entry carries usable key material (api key or oauth)."""
    if isinstance(payload, list):
        return any(_entry_has_credential(item) for item in payload)
    if isinstance(payload, dict):
        if str(payload.get("type", "")) == "api":
            return bool(str(payload.get("key", "")).strip())
        if isinstance(payload.get("key"), str) and payload["key"].strip():
            return True
        if isinstance(payload.get("token"), str) and payload["token"].strip():
            return True
        return bool(payload)  # e.g. oauth payloads keep pin-header/auth fields
    return False


def scope_providers() -> frozenset[str]:
    """Default discover/sweep scope: every discovered provider minus the
    OPTIONAL ``scope_excludes:`` overrides in ``configs/fleet.yaml``.

    New providers appearing in the opencode config automatically join the
    default scope — no file edit needed. An explicit exclusion is the only
    way to keep a provider out.
    """
    overrides = fleet.read_overrides()
    discovered = {m.provider for m in fleet.discovered_models(overrides=overrides)}
    return frozenset(discovered - overrides.scope_excludes)


@dataclass(frozen=True)
class DiscoveredModel:
    """One model enumerated from an opencode.jsonc provider block."""

    provider: str
    model_id: str  # slug within the provider (the full id is provider/model_id)
    display_name: str
    wire: str  # wire type derived from the provider's npm field / overrides
    in_scope: bool  # provider member of the effective scope set
    auth_present: bool  # credential visible in auth files or inline apiKey


def _inline_auth(providers: dict[str, dict]) -> dict[str, bool]:
    return {
        pid: bool(
            isinstance(cfg.get("options"), dict)
            and str(cfg.get("options", {}).get("apiKey", "")).strip()
        )
        for pid, cfg in providers.items()
        if isinstance(cfg, dict)
    }


def enumerate_models(scope: frozenset[str]) -> list[DiscoveredModel]:
    """Enumerate every provider/model declared in project + global configs.

    Model inventory and wire types come from :func:`hr.fleet.discovered_models`
    (project configs are merged over the global one). Each model is annotated
    with ``in_scope`` (provider member of ``scope``) and ``auth_present``.
    Raises ValueError naming the offending file on malformed JSON or an
    unresolvable wire; no live opencode runtime is consulted.
    """
    overrides = fleet.read_overrides()
    providers = fleet.read_providers()
    models = fleet.discovered_models(providers=providers, overrides=overrides)
    inline_auth = _inline_auth(providers)
    auth_providers = read_auth_providers()

    discovered: list[DiscoveredModel] = []
    for model in models:
        discovered.append(
            DiscoveredModel(
                provider=model.provider,
                model_id=model.model_id,
                display_name=model.display_name,
                wire=model.wire,
                in_scope=model.provider in scope,
                auth_present=(
                    inline_auth.get(model.provider, False)
                    or model.provider in auth_providers
                ),
            )
        )
    return discovered


def upsert_models(conn, models: list[DiscoveredModel]) -> tuple[int, int]:
    """Idempotently upsert provider and model rows (ON CONFLICT DO NOTHING).

    ``hr.model`` rows use the composite ``provider/model_id`` as their primary
    id, mirroring stage0's ``_ensure_provider_model_records`` convention.
    Provider display names come from the opencode config's ``name`` field.
    Returns ``(provider_rows, model_rows)`` — honest rowcounts, so a rerun
    yields 0/0: the idempotency is visible in the counts themselves.
    """
    provider_rows = 0
    model_rows = 0
    seen_providers: set[str] = set()
    names = fleet.provider_display_names()
    with conn.cursor() as cur:
        for model in models:
            if model.provider not in seen_providers:
                seen_providers.add(model.provider)
                cur.execute(
                    "INSERT INTO hr.provider (provider_id, name) VALUES (%s, %s) "
                    "ON CONFLICT (provider_id) DO NOTHING",
                    (model.provider, names.get(model.provider, model.provider)),
                )
                provider_rows += cur.rowcount or 0
        for model in models:
            cur.execute(
                "INSERT INTO hr.model (model_id, provider_fk, model_name) VALUES (%s, %s, %s) "
                "ON CONFLICT (model_id) DO NOTHING",
                (f"{model.provider}/{model.model_id}", model.provider, model.display_name),
            )
            model_rows += cur.rowcount or 0
    conn.commit()
    return provider_rows, model_rows
