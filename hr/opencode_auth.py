"""opencode credential file access: auth-v2-first presence and api-key reads.

Two distinct predicates over the SAME opencode auth files under the HOME
data dir (``~/.local/share/opencode``):

* :func:`providers_with_credentials` — PRESENCE: which providers carry ANY
  credential (api key or oauth token), applying opencode's FILE-level rule:
  when ``auth-v2.json`` parses non-empty the legacy ``auth.json`` is not
  consulted at all.
* :func:`provider_api_key` — KEY EXTRACTION: api-key material for ONE
  provider with PROVIDER-level fallback — the auth-v2.json entry for that
  provider first, else the legacy ``auth.json`` entry. An oauth ``token`` is
  never treated as an api key, so hybrid stores keep both kinds of entries
  working.

Documented limitation (HOME-first): paths resolve from ``$HOME`` on every
platform; no Windows ``LOCALAPPDATA`` conventions are implemented here.
opencode stores auth under ``%APPDATA%`` on Windows — out of scope for this
resolver, which mirrors the Linux/macOS ``.local/share/opencode`` layout.

Every JSON read is tolerant (missing / unreadable / malformed behaves as
empty), so a corrupt ``auth-v2.json`` can never raise out of these
predicates — it falls through to whatever the legacy file provides.

The module is stdlib-only and imports nothing from ``hr``: it is the
cycle-free leaf every credential reader plugs into.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _data_dir() -> Path:
    """HOME-derived opencode data dir, resolved at call time."""
    home = Path(os.environ["HOME"]) if os.environ.get("HOME") else Path.home()
    return home / ".local" / "share" / "opencode"


def _read_auth_file(path: Path) -> dict:
    """Tolerant auth-file read: missing / unreadable / malformed -> empty."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _entry_has_credential(payload: object) -> bool:
    """True when a v2 entry carries any credential material.

    Verbatim port of discover.py's historical rule (oauth semantics): a
    ``type:"api"`` entry with a non-empty ``key``, any non-empty ``key`` or
    ``token`` string, or any other non-empty dict/list payload counts as a
    credential.
    """
    if isinstance(payload, list):
        return any(_entry_has_credential(item) for item in payload)
    if not isinstance(payload, dict):
        return False
    if str(payload.get("type", "")) == "api":
        return bool(str(payload.get("key", "")).strip())
    if isinstance(payload.get("key"), str) and payload["key"].strip():
        return True
    if isinstance(payload.get("token"), str) and payload["token"].strip():
        return True
    return bool(payload)


def providers_with_credentials() -> set[str]:
    """Providers holding ANY credential across the auth files.

    File-level rule: a non-empty ``auth-v2.json`` shadows ``auth.json``
    entirely; v2 providers come from the ``accounts`` then ``active``
    sections (``_entry_has_credential`` decides per entry), else the flat
    top-level keys of the legacy file.
    """
    data_dir = _data_dir()
    auth_v2 = _read_auth_file(data_dir / "auth-v2.json")
    if auth_v2:
        present: set[str] = set()
        for section in ("accounts", "active"):
            entries = auth_v2.get(section)
            if isinstance(entries, dict):
                present.update(
                    pid
                    for pid, payload in entries.items()
                    if _entry_has_credential(payload)
                )
        return present
    auth = _read_auth_file(data_dir / "auth.json")
    return {pid for pid, payload in auth.items() if isinstance(payload, dict)}


def _v2_entry_key(entry: object) -> str | None:
    """Non-empty ``key`` string from a v2 provider entry (dict or list)."""
    candidates = entry if isinstance(entry, list) else [entry]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        key = candidate.get("key")
        if isinstance(key, str) and key.strip():
            return key.strip()
    return None


def _legacy_entry_key(auth: dict, provider: str) -> str | None:
    """Non-empty ``key`` string from a legacy auth.json provider entry."""
    entry = auth.get(provider)
    if not isinstance(entry, dict):
        return None
    key = entry.get("key")
    if isinstance(key, str) and key.strip():
        return key.strip()
    return None


def provider_api_key(provider: str, data_dir: Path | None = None) -> str | None:
    """Api key for ONE provider: auth-v2 entry first, legacy file fallback.

    Provider-level fallback: an absent / oauth-token-only auth-v2 entry for
    this provider does NOT block the legacy file. Returns None when nothing
    usable exists (oauth material never qualifies).

    ``data_dir`` is injecded by callers resolving against a known auth
    directory (adapters with injected ``auth_path``); HOME when absent.
    """
    directory = data_dir if data_dir is not None else _data_dir()
    auth_v2 = _read_auth_file(directory / "auth-v2.json")
    if auth_v2:
        for section in ("accounts", "active"):
            entries = auth_v2.get(section)
            if isinstance(entries, dict) and provider in entries:
                key = _v2_entry_key(entries[provider])
                if key is not None:
                    return key
    auth = _read_auth_file(directory / "auth.json")
    return _legacy_entry_key(auth, provider)