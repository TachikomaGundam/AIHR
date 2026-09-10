"""Configuration resource discovery and YAML overlay loading.

Includes the AIHR turnkey-database resource layer: discovery/parsing of the
db env file (``docker/.env`` next to the shipped compose, with a data-dir
fallback) written by ``hr db-up``, and credential resolution from a legacy
docker-compose manifest (``HR_COMPOSE_FILE``).
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# AIHR turnkey database: shared defaults + db env file
# ---------------------------------------------------------------------------

DB_DEFAULT_PORT = 5433

# Defaults for the legacy HR_COMPOSE_FILE path: a machine opting into that
# fallback points at the pre-aihr shared Wiki.js deployment (role wikijs,
# database wiki, host-mapped port 5432), so building its DSN on the new
# aihr defaults would connect to the wrong database.
LEGACY_COMPOSE_DB_DEFAULTS = {
    "db_host": "localhost",
    "db_port": 5432,
    "db_name": "wiki",
    "db_user": "wikijs",
}


def read_root_toml() -> dict[str, Any]:
    """Secret-free ``hr.toml`` at the monorepo root ({} when missing)."""
    toml_path = hr_home() / "hr.toml"
    if not toml_path.exists():
        return {}
    with toml_path.open("rb") as fh:
        return dict(tomllib.load(fh))


def wiki_config() -> dict[str, Any] | None:
    """Optional Wiki.js publish target from the root ``hr.toml`` ``[wiki]`` section.

    ``hr publish`` skips cleanly (exit 0) when this returns ``None`` — the
    wiki is an optional, config-driven publish target. Recognized keys
    (all optional): ``graphql_url``, ``api_key_file``.
    """
    section = read_root_toml().get("wiki")
    if isinstance(section, dict) and section:
        return section
    return None


def db_env_file_candidates() -> tuple[Path, Path]:
    """``(primary, fallback)`` locations of the AIHR db env file.

    *primary* is ``<hr_home>/docker/.env``, next to the shipped compose file;
    when that directory is not writable (read-only install), db-up falls back
    to ``<XDG data dir or ~/.local/share>/aihr/db.env``.
    """
    primary = hr_home() / "docker" / ".env"
    xdg = os.environ.get("XDG_DATA_HOME")
    data_root = Path(xdg) if xdg else home_dir() / ".local" / "share"
    return primary, data_root / "aihr" / "db.env"


def _can_write_dir(path: Path) -> bool:
    """True when ``path`` is (or can be created inside) a writable directory."""
    try:
        probe = path
        while not probe.is_dir():
            parent = probe.parent
            if parent == probe:
                return False
            probe = parent
        return os.access(probe, os.W_OK | os.X_OK)
    except OSError:
        return False


def db_env_file(*, for_write: bool = False) -> Path | None:
    """Sole discovery point for the AIHR db env file (used by CLI and config).

    Read mode (``for_write=False``): the first candidate that exists, else
    ``None`` — never creates anything. Write mode: an existing candidate,
    else the primary path when its directory is writable, else the fallback.
    """
    primary, fallback = db_env_file_candidates()
    if primary.is_file():
        return primary
    if fallback.is_file():
        return fallback
    if not for_write:
        return None
    return primary if _can_write_dir(primary.parent) else fallback


def read_db_env_file(path: Path) -> dict[str, str]:
    """Parse the ``KEY=VALUE`` db env file (comments/blank lines skipped).

    Missing/unreadable files resolve to ``{}`` — discovery is never an error
    at the config layer; the DSN chain reports an unresolvable password.
    """
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if key.strip() and value:
            out[key.strip()] = value
    return out


# ---------------------------------------------------------------------------
# Legacy docker-compose credential discovery (HR_COMPOSE_FILE opt-in path)
# ---------------------------------------------------------------------------

def _compose_env_block(services: dict[str, Any], service: str) -> dict[str, str]:
    """Flatten one service's ``environment`` block to a key -> value map.

    Accepts both write-styles (they parse identically): the YAML mapping
    (``{KEY: value}``) and the list of ``KEY=VALUE`` strings.
    """
    block = (services.get(service) or {}).get("environment")
    found: dict[str, str] = {}
    if isinstance(block, dict):
        for key, value in block.items():
            if value not in (None, ""):
                found.setdefault(str(key), str(value))
    elif isinstance(block, list):
        for entry in block:
            if isinstance(entry, str) and "=" in entry:
                key, _, value = entry.partition("=")
                if value.strip():
                    found.setdefault(key.strip(), value.strip())
    return found


def compose_db_settings(compose_path: Path) -> dict[str, str]:
    """Resolve user/db/password from a docker-compose manifest.

    ``services.db.environment`` POSTGRES_USER/POSTGRES_DB/POSTGRES_PASSWORD
    form the base; ``services.wiki.environment`` DB_USER/DB_NAME/DB_PASS
    (then POSTGRES_PASSWORD) overlay them — the Wiki.js consumer block wins,
    matching what that stack actually connects with. Returns a dict with any
    of ``db_user`` / ``db_name`` / ``password`` (only keys the file defines).
    """
    with open(compose_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    services = data.get("services", {}) or {}

    def pick(env: dict[str, str], keys: tuple[str, ...]) -> str:
        for key in keys:
            value = env.get(key)
            if value:
                return value
        return ""

    db_env = _compose_env_block(services, "db")
    wiki_env = _compose_env_block(services, "wiki")
    settings: dict[str, str] = {}
    user = pick(wiki_env, ("DB_USER",)) or pick(db_env, ("POSTGRES_USER",))
    name = pick(wiki_env, ("DB_NAME",)) or pick(db_env, ("POSTGRES_DB",))
    password = (
        pick(wiki_env, ("DB_PASS", "POSTGRES_PASSWORD"))
        or pick(db_env, ("POSTGRES_PASSWORD",))
    )
    if user:
        settings["db_user"] = user
    if name:
        settings["db_name"] = name
    if password:
        settings["password"] = password
    return settings


def compose_db_password(compose_path: Path) -> str:
    """Password view of :func:`compose_db_settings` ("" when unresolvable)."""
    return compose_db_settings(compose_path).get("password", "")


def hr_home() -> Path:
    """Resolve resources from an override, source checkout, or installation."""
    env = os.environ.get("HR_HOME")
    if env:
        return Path(env).expanduser().resolve()
    source_root = Path(__file__).resolve().parent.parent
    if (source_root / "configs").is_dir():
        return source_root
    target_root = source_root / "share" / "aihr"
    if (target_root / "configs").is_dir():
        return target_root
    installed_root = Path(sys.prefix).resolve() / "share" / "aihr"
    if (installed_root / "configs").is_dir():
        return installed_root
    return source_root


def config_path(name: str) -> Path:
    return hr_home() / "configs" / name


def _local_overlay_path(name: str) -> Path:
    suffix = ".yaml"
    if not name.endswith(suffix):
        raise ValueError(f"overlay only supported for *.yaml configs: {name!r}")
    return config_path(name[: -len(suffix)] + ".local.yaml")


def _deep_merge(base: dict, overlay: dict) -> dict:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_yaml(name: str) -> dict:
    """Load a tracked YAML config with an optional local-wins overlay."""
    path = config_path(name)
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    overlay_path = _local_overlay_path(name)
    if overlay_path.exists():
        with overlay_path.open("r", encoding="utf-8") as stream:
            extra = yaml.safe_load(stream) or {}
        if not isinstance(extra, dict):
            raise ValueError(
                f"invalid local overlay (must be a mapping): {overlay_path}"
            )
        data = _deep_merge(data, extra)
    return data


def home_dir() -> Path:
    """HOME-respecting user home: ``$HOME`` when set, else ``Path.home()``.

    Resolved at call time, so redirected ``HOME`` env always wins — the
    hermeticity contract sandboxed tests rely on.
    """
    env = os.environ.get("HOME")
    if env:
        return Path(env)
    return Path.home()


def opencode_data_dir() -> Path:
    """opencode's per-user data dir (auth files) under the resolved HOME."""
    return home_dir() / ".local" / "share" / "opencode"


def opencode_config_dir() -> Path:
    env = os.environ.get("OPENCODE_CONFIG_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return home_dir() / ".config" / "opencode"
