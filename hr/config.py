"""Unified configuration layer for HR.

This module is THE config module for the hr package:

* ``db_dsn()``      — one DSN resolution chain:
  ``HR_DSN`` env -> ``hr.toml`` + ``HR_DB_PASSWORD`` env
  -> opt-in docker-compose fallback (``HR_COMPOSE_FILE`` env)
* ``hr_home()``     — monorepo root (``HR_HOME`` env > package auto-detect)
* ``config_path()`` — ``hr_home()/configs/<name>``
* ``load_yaml()``   — load a named YAML config through this layer only, then
  deep-merge the optional gitignored ``configs/<name>.local.yaml`` overlay
  (publish-safe seam: tracked configs ship placeholders, real deployment
  values stay local — local wins per key, dicts merge recursively,
  lists are replaced)
* ``opencode_config_dir()`` — ``OPENCODE_CONFIG_DIR`` env > ``~/.config/opencode``
* ``compose_db_password()`` — docker-compose password reader backing both
  ``db_dsn()`` and ``hr.db`` (opt-in via ``HR_COMPOSE_FILE``)
* ``get_provider_config()`` — generic per-provider endpoint + API key
  (opencode provider block -> ``gateway_urls`` in ``configs/fleet.yaml`` ->
  ``auth.json``), no provider-name special cases
* ``gateway_urls()`` — provider -> base URL map (routing data) from
  ``configs/fleet.yaml``
* ``itemrepo_path()`` — calibration item repo (``HR_ITEMREPO`` env >>
  ``HR_HOME/itemrepo``, fail loud naming the resolution)
* ``Settings`` / ``load_settings()`` — v1 backward-compat pydantic contract
  (consumed by ``hr.database``, ``hr.recommend``, ``hr.bench``), reimplemented
  on top of this layer.

Invariants (F2 universality gate):
* zero hardcoded absolute paths — every location derives from env vars,
  ``Path(__file__)`` or ``Path.home()``
* zero password literals — secrets only ever come from env vars or the
  opt-in docker-compose fallback
"""
from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Unified layer
# ---------------------------------------------------------------------------

_DEFAULT_DB_HOST = "localhost"
_DEFAULT_DB_PORT = 5432
_DEFAULT_DB_NAME = "wiki"
_DEFAULT_DB_USER = "wikijs"


def hr_home() -> Path:
    """Monorepo root: ``HR_HOME`` env override, else auto-detect.

    Auto-detect: parent of the parent of this package directory
    (``…/hr/config.py`` -> repo root), i.e.
    ``Path(__file__).resolve().parent.parent``. In this checkout that
    resolves to the repository root at runtime, wherever it is deployed.
    """
    env = os.environ.get("HR_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def config_path(name: str) -> Path:
    """Path of a named YAML config under the unified ``configs/`` directory."""
    return hr_home() / "configs" / name


def _local_overlay_path(name: str) -> Path:
    """Path of the optional local overlay for ``configs/<name>``.

    ``configs/fleet.yaml`` -> ``configs/fleet.local.yaml``. Overlays are
    gitignored (``*.local.yaml``): they carry THIS machine's real deployment
    values so the tracked configs can ship with placeholders only.
    """
    suffix = ".yaml"
    if not name.endswith(suffix):
        raise ValueError(f"overlay only supported for *.yaml configs: {name!r}")
    return config_path(name[: -len(suffix)] + ".local.yaml")


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursive dict merge with local-wins semantics.

    * dict values merge recursively, key by key (local keys win over base
      keys; base keys absent from the local file are preserved);
    * everything else (lists, scalars) is REPLACED by the local value,
      never merged/concatened.

    Returns a new dict; neither input is mutated.
    """
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_yaml(name: str) -> dict:
    """Load ``configs/<name>`` through this layer only, then deep-merge an
    optional local overlay.

    Overlay: when ``configs/<name-without-.yaml>.local.yaml`` exists, it is
    deep-merged over the tracked file — local wins per key, dict values
    merge recursively, lists are replaced (never merged). This is the
    publish-safe seam: real anchors / wire overrides / gateway URLs / extra
    models live in the gitignored ``*.local.yaml`` files, while the tracked
    configs ship with example placeholders only. A missing overlay is
    normal (the tracked file is used as-is); a missing tracked file still
    raises FileNotFoundError with the fully resolved path.
    """
    path = config_path(name)
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    overlay_path = _local_overlay_path(name)
    if overlay_path.exists():
        with overlay_path.open("r", encoding="utf-8") as fh:
            extra = yaml.safe_load(fh) or {}
        if not isinstance(extra, dict):
            raise ValueError(
                f"invalid local overlay (must be a mapping): {overlay_path}"
            )
        data = _deep_merge(data, extra)
    return data


def opencode_config_dir() -> Path:
    """opencode config dir: ``OPENCODE_CONFIG_DIR`` env > ``~/.config/opencode``."""
    env = os.environ.get("OPENCODE_CONFIG_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".config" / "opencode"


def wiki_config() -> dict[str, Any] | None:
    """Optional Wiki.js publish target from the root ``hr.toml`` ``[wiki]`` section.

    ``hr publish`` skips cleanly (exit 0) when this returns ``None`` — the
    wiki is an optional, config-driven publish target. Recognized keys
    (all optional): ``graphql_url``, ``api_key_file``.
    """
    section = _read_root_toml().get("wiki")
    if isinstance(section, dict) and section:
        return section
    return None


# ---------------------------------------------------------------------------
# DSN resolution
# ---------------------------------------------------------------------------

def _read_root_toml() -> dict[str, Any]:
    """Secret-free ``hr.toml`` at the monorepo root ({} when missing)."""
    toml_path = hr_home() / "hr.toml"
    if not toml_path.exists():
        return {}
    with toml_path.open("rb") as fh:
        return dict(tomllib.load(fh))


def _build_dsn(fields: dict[str, Any], password: str) -> str:
    """Build ``postgresql://user:pass@host:port/dbname`` from secret-free
    fields; env vars override the toml values."""
    host = os.environ.get("HR_DB_HOST") or fields.get("db_host", _DEFAULT_DB_HOST)
    port = int(os.environ.get("HR_DB_PORT") or fields.get("db_port", _DEFAULT_DB_PORT))
    name = os.environ.get("HR_DB_NAME") or fields.get("db_name", _DEFAULT_DB_NAME)
    user = os.environ.get("HR_DB_USER") or fields.get("db_user", _DEFAULT_DB_USER)
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


def compose_db_password(compose_path: Path) -> str:
    """Read the DB password from a docker-compose ``services.wiki`` block.

    Accepts both ``services.wiki`` write-styles (they parse identically):
    the ``environment`` block as a YAML mapping (``{DB_PASS: …}``) or as a
    list of ``KEY=VALUE`` strings. Looks for ``DB_PASS`` first, then
    ``POSTGRES_PASSWORD``. Returns "" when neither is present.
    """
    with open(compose_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    services = data.get("services", {}) or {}
    wiki = services.get("wiki") or {}
    env_block = wiki.get("environment")
    if isinstance(env_block, dict):
        for key in ("DB_PASS", "POSTGRES_PASSWORD"):
            value = env_block.get(key)
            if value:
                return str(value)
    elif isinstance(env_block, list):
        for key in ("DB_PASS", "POSTGRES_PASSWORD"):
            for entry in env_block:
                if isinstance(entry, str) and entry.startswith(f"{key}="):
                    return entry.split("=", 1)[1]
    return ""


def db_dsn() -> str:
    """Resolve the Postgres DSN for HR.

    Resolution order:
      1. ``HR_DSN`` env var — returned verbatim.
      2. ``hr.toml`` at the monorepo root (secret-free by contract:
         db_host/db_port/db_name/db_user only) + ``HR_DB_PASSWORD`` env var
         -> ``postgresql://user:pass@host:port/dbname``.
      3. docker-compose — a DOCUMENTED OPT-IN fallback active only when the
         ``HR_COMPOSE_FILE`` env var is set; reads ``DB_PASS`` /
         ``POSTGRES_PASSWORD`` from ``services.wiki.environment`` in that
         compose yaml and builds the same DSN shape.

    Raises RuntimeError listing every attempted step when nothing resolves.
    """
    steps: list[str] = []

    # (1) explicit DSN
    dsn = os.environ.get("HR_DSN")
    if dsn:
        return dsn
    steps.append("HR_DSN env var (unset)")

    # (2) secret-free hr.toml + HR_DB_PASSWORD env
    toml_path = hr_home() / "hr.toml"
    fields = _read_root_toml()
    if fields:
        steps.append(f"hr.toml {toml_path} (found; secret-free by contract)")
    else:
        steps.append(f"hr.toml {toml_path} (not found)")
    password = os.environ.get("HR_DB_PASSWORD")
    if fields and password:
        return _build_dsn(fields, password)
    if fields:
        steps.append(
            "HR_DB_PASSWORD env var (unset; hr.toml intentionally stores no password)"
        )
    else:
        steps.append("HR_DB_PASSWORD env var (unset)")

    # (3) documented opt-in compose fallback
    compose = os.environ.get("HR_COMPOSE_FILE")
    if compose:
        compose_path = Path(compose).expanduser()
        if compose_path.is_file():
            payload = compose_db_password(compose_path)
            if payload:
                return _build_dsn(fields, payload)
            steps.append(
                f"HR_COMPOSE_FILE={compose} "
                "(no DB_PASS/POSTGRES_PASSWORD under services.wiki.environment)"
            )
        else:
            steps.append(f"HR_COMPOSE_FILE={compose} (file not found)")
    else:
        steps.append("HR_COMPOSE_FILE env var (unset; compose fallback is opt-in)")

    attempted = "\n  ".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
    raise RuntimeError(
        "cannot resolve HR database DSN; attempted, in order:\n"
        f"  {attempted}\n"
        "Set HR_DSN, or create hr.toml at the monorepo root plus "
        "HR_DB_PASSWORD, or set HR_COMPOSE_FILE to enable the "
        "docker-compose password fallback."
    )


# ---------------------------------------------------------------------------
# opencode config readers (routed through opencode_config_dir)
# ---------------------------------------------------------------------------

def _auth_json_path() -> Path:
    """auth.json location — resolved at call time so HOME env changes count."""
    return Path.home() / ".local" / "share" / "opencode" / "auth.json"


def _read_auth_json() -> dict:
    """Read and parse opencode's auth.json."""
    auth_json = _auth_json_path()
    if not auth_json.exists():
        return {}
    return json.loads(auth_json.read_text(encoding="utf-8"))


class ProviderConfig(BaseModel):
    """Resolved endpoint + API key for a single provider."""

    model_config = {"frozen": True}

    base_url: str
    api_key: str


def gateway_urls() -> dict[str, str]:
    """Provider -> gateway base URL map from ``configs/fleet.yaml``.

    Endpoint URLs are routing data, not credentials. Used as the fallback
    when a provider has no ``options.baseURL`` in the opencode config.
    """
    try:
        data = load_yaml("fleet.yaml")
    except FileNotFoundError:
        return {}
    urls = data.get("gateway_urls") or {}
    if not isinstance(urls, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in urls.items()
    ):
        raise ValueError(
            "invalid configs/fleet.yaml: 'gateway_urls' must be a map of "
            "provider -> base URL"
        )
    return dict(urls)


def get_provider_config(provider: str) -> ProviderConfig:
    """Resolve a provider's endpoint + API key — generic, data-driven.

    Base URL: the provider's ``options.baseURL`` in the opencode config
    (merged global + project via :mod:`hr.opencfg`), else its
    ``gateway_urls`` entry in ``configs/fleet.yaml``.
    API key: the provider block's ``options.apiKey``, else the matching
    ``auth.json`` entry.

    There are no provider-name special cases: a provider with none of these
    sources configured is an explicit error naming exactly where to declare
    it (never a silent guess).
    """
    from hr import opencfg  # local import: opencfg imports hr.config

    block = opencfg.read_providers().get(provider)
    options = {}
    if isinstance(block, dict) and isinstance(block.get("options"), dict):
        options = block["options"]
    base_url = str(options.get("baseURL") or "")
    api_key = str(options.get("apiKey") or "")
    if not base_url:
        base_url = gateway_urls().get(provider, "")
    if not api_key:
        auth_entry = _read_auth_json().get(provider)
        if isinstance(auth_entry, dict) and isinstance(auth_entry.get("key"), str):
            api_key = auth_entry["key"].strip()
    if not base_url or not api_key:
        missing = [
            label
            for label, value in (("base URL", base_url), ("API key", api_key))
            if not value
        ]
        raise ValueError(
            f"provider {provider!r} has no configured {', '.join(missing)}: "
            "declare options baseURL/apiKey in the opencode config provider "
            "block, a 'gateway_urls:' entry in configs/fleet.yaml, and/or an "
            "auth.json entry for the provider"
        )
    return ProviderConfig(base_url=base_url.rstrip("/"), api_key=api_key)


def itemrepo_path() -> Path:
    """Calibration item repository: ``HR_ITEMREPO`` env, else ``HR_HOME/itemrepo``.

    Fails loud (RuntimeError naming the resolution) when the directory does
    not exist — a sweep over a phantom repository would silently grade
    nothing.
    """
    env = os.environ.get("HR_ITEMREPO")
    if env:
        path = Path(env).expanduser().resolve()
        label = f"HR_ITEMREPO env var ({path})"
    else:
        home = hr_home()
        path = home / "itemrepo"
        label = f"HR_HOME/itemrepo ({path})"
    if not path.is_dir():
        raise RuntimeError(
            f"item repository not found: resolved via {label}; point "
            "HR_ITEMREPO at a directory of calibration items"
        )
    return path


# ---------------------------------------------------------------------------
# v1 backward-compat Settings / load_settings (consumers: database, recommend,
# bench; contract: load_settings -> Settings(.dsn) used by database.get_connection)
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HR_",
        env_file=".env",
        extra="ignore",
    )

    dsn: str = ""  # override via HR_DSN env var (preferred); takes precedence over individual fields
    db_host: str = _DEFAULT_DB_HOST
    db_port: int = _DEFAULT_DB_PORT
    db_name: str = _DEFAULT_DB_NAME
    db_user: str = os.environ.get("HR_DB_USER", _DEFAULT_DB_USER)  # also resolvable via HR_DSN
    db_password: str = ""  # resolved via HR_DB_PASSWORD env var or HR_DSN; never hardcode


def load_settings() -> Settings:
    """Load settings, merging the root ``hr.toml`` defaults if present.

    Reads ``hr_home()/hr.toml`` through the unified layer; ``HR_*`` env vars
    (pydantic-settings) always win.
    """
    file_defaults: dict[str, object] = _read_root_toml()
    return Settings(**file_defaults)