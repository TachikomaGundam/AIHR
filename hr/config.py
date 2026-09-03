"""Runtime, database, and provider configuration.

Resource discovery and YAML overlays live in :mod:`hr.config_resources` and
are re-exported here as the package's stable configuration surface. Secrets
come only from environment variables, auth files, or an explicitly selected
compose file; runtime paths never rely on hardcoded machine locations.
"""
from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from pydantic import BaseModel

from hr.config_resources import config_path as config_path
from hr.config_resources import hr_home as hr_home
from hr.config_resources import load_yaml as load_yaml
from hr.config_resources import opencode_config_dir as opencode_config_dir
from hr.config_resources import opencode_data_dir as opencode_data_dir
from hr.opencode_auth import provider_api_key as provider_api_key

# ---------------------------------------------------------------------------
# Unified layer
# ---------------------------------------------------------------------------

_DEFAULT_DB_HOST = "localhost"
_DEFAULT_DB_PORT = 5432
_DEFAULT_DB_NAME = "wiki"
_DEFAULT_DB_USER = "wikijs"


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
    encoded_user = quote(str(user), safe="")
    encoded_password = quote(password, safe="")
    encoded_name = quote(str(name), safe="")
    return f"postgresql://{encoded_user}:{encoded_password}@{host}:{port}/{encoded_name}"


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
    """Legacy auth.json location — resolved at call time so HOME redirects
    count; auth-v2.json lives beside it under the same data dir."""
    return opencode_data_dir() / "auth.json"


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
    credential read from the auth files (auth-v2.json entry first, then the
    legacy ``auth.json`` entry — :func:`hr.opencode_auth.provider_api_key`).

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
        api_key = provider_api_key(provider) or ""
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
            "auth-v2.json/auth.json entry for the provider"
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


def output_root() -> Path:
    """Runtime output root for run artifacts (never the repo tree).

    Contract: generated artifacts (bench exports, calibration reports, sweep
    dumps, …) land OUTSIDE the repository — tests run in a staging workspace,
    and a live ``hr`` run must never dirty its own checkout. Resolution order:

      1. ``HR_OUTPUT_DIR`` env var (explicit override; CLI flags that name an
         explicit output path still win at the call site),
      2. platform cache dir: ``$XDG_CACHE_HOME/hr`` (Linux),
         ``~/Library/Caches/hr`` (macOS), ``%LOCALAPPDATA%/hr\\Cache`` (Windows),
      3. the system temp dir (``tempdir()/hr``) as last resort.

    Callers create/use subdirectories under the returned root; the root itself
    is not created here (resolution must stay side-effect free).
    """
    env = os.environ.get("HR_OUTPUT_DIR")
    if env:
        return Path(env).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Cache"
        return base / "hr"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "hr"
    base = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return base / "hr"
