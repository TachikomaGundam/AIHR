"""Turnkey bundle install: ``hr install-post`` + the placement receipt.

The bundle archive owns ``D``'s content (``app/``, ``pg/``, ``share/aihr``);
install-post is the machine-local wiring step that follows extraction. It is
idempotent and records EVERY placement and mutation it performs in
``D/receipt.json`` — the exact reverse-playbook :mod:`hr.lifecycle_uninstall`
replays. Never blind-delete: what the receipt did not record, uninstall
refuses to touch and hands back as manual steps instead.

Receipt schema::

    {"version": "<hr version>", "ts": "<UTC ISO-8601>",
     "entries": [{"path": "<abs path>",
                  "kind": "file" | "dir" | "path-block" | "config-entry",
                  "value": "<placement detail / recorded string>",
                  "prior_sha256": "<content hash before first mutation>"}]}

Every subprocess in this layer is confined to :mod:`hr.pg_env` /
:mod:`hr.pg_launcher`; install-post itself only touches files, PATH rc
blocks (via :mod:`hr.setup_env`) and opencode JSONC configs (via
:mod:`hr.opencfg_editor`).
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from hr import pg_env, setup_env
from hr.config_resources import aihr_home_dir, opencode_config_dir
from hr.opencfg_editor import ensure_plugin_entry

HR_AGENT_PLUGIN = "opencode-hr-agent@latest"
FASTDRAW_PLUGIN = "opencode-fastdraw@latest"
RECEIPT_NAME = "receipt.json"

#: Bundle parts that extraction (not install-post) must have staged.
REQUIRED_PARTS: tuple[str, ...] = ("app", "pg/bin", "share/aihr")

KIND_FILE = "file"
KIND_DIR = "dir"
KIND_PATH_BLOCK = "path-block"
KIND_CONFIG_ENTRY = "config-entry"


def is_windows(platform: Optional[str] = None) -> bool:
    return (platform or sys.platform) == "win32"


@dataclass(frozen=True)
class Entry:
    path: str
    kind: str
    value: str
    prior_sha256: Optional[str] = None


def receipt_path(d: Path) -> Path:
    return d / RECEIPT_NAME


def sha256_file(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def save_receipt(d: Path, version: str, entries: list[Entry]) -> Path:
    payload = {
        "version": version,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "entries": [{k: v for k, v in asdict(e).items() if v is not None} for e in entries],
    }
    path = receipt_path(d)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def load_receipt(d: Path) -> Optional[dict]:
    try:
        data = json.loads(receipt_path(d).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("entries"), list) else None


def shim_path(d: Path, platform: Optional[str] = None) -> Path:
    return d / "bin" / ("hr.cmd" if is_windows(platform) else "hr")


SHIM_CMD_TEXT = '@echo off\r\n"%~dp0..\\app\\hr.exe" %*\r\n'
SHIM_LINK_TARGET = "../app/hr"


def place_shim(d: Path, platform: Optional[str] = None) -> tuple[Path, str, bool]:
    """Create ``D/bin/hr`` (POSIX relative symlink) or ``hr.cmd`` (Windows).

    Returns ``(path, recorded value, created-now)``; a shim that already
    matches is left untouched (idempotent re-run).
    """
    bin_dir = d / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if is_windows(platform):
        shim = bin_dir / "hr.cmd"
        payload = SHIM_CMD_TEXT.encode("utf-8")  # bytes: CRLF must survive verbatim
        existed = shim.is_file() and shim.read_bytes() == payload
        if not existed:
            shim.write_bytes(payload)
        return shim, SHIM_CMD_TEXT, not existed
    shim = bin_dir / "hr"
    if shim.is_symlink() and shim.readlink() == Path(SHIM_LINK_TARGET):
        return shim, f"symlink:{SHIM_LINK_TARGET}", False
    if shim.exists() or shim.is_symlink():
        shim.unlink()
    shim.symlink_to(SHIM_LINK_TARGET)
    return shim, f"symlink:{SHIM_LINK_TARGET}", True


def install_post(
    port: Optional[int] = None,
    *,
    d: Optional[Path] = None,
    platform: Optional[str] = None,
    say: Callable[[str], None] = print,
) -> int:
    """Wire an extracted bundle into this machine (idempotent).

    Expects ``D`` already populated by the bundle installer (``app/``,
    ``pg/``, ``share/aihr/``); seeds ``D/db.env``, installs the ``hr`` shim
    in ``D/bin``, registers ``D/bin`` on the user PATH (marked block, byte-
    reversible) and the two opencode plugin entries, then writes the receipt.
    """
    root = aihr_home_dir() if d is None else d
    missing = [part for part in REQUIRED_PARTS if not (root / part).is_dir()]
    if missing:
        say(f"error: bundle dir {root} is not staged (missing: {', '.join(missing)}).")
        say("extract the turnkey bundle archive into this directory first (see README).")
        return 1
    entries: list[Entry] = []
    env_file, effective, created = pg_env.ensure_env(root, port)
    say(
        f"db.env: {'wrote' if created else 'kept'} {env_file} "
        f"(random password kept, mode 0600, port {effective})"
    )
    entries.append(Entry(str(env_file), KIND_FILE, "db-env"))
    bin_dir = root / "bin"
    entries.append(Entry(str(bin_dir), KIND_DIR, "shim dir"))
    shim, shim_value, _ = place_shim(root, platform)
    say(f"shim: {shim} -> {shim_value}")
    entries.append(Entry(str(shim), KIND_FILE, shim_value))
    _changed, message = setup_env.persist_path(bin_dir, platform=platform)
    say(f"path: {message}")
    entries.append(Entry(str(bin_dir), KIND_PATH_BLOCK, message))
    config_dir = opencode_config_dir()
    for config_file, plugin in (
        (config_dir / "opencode.jsonc", HR_AGENT_PLUGIN),
        (config_dir / "tui.json", FASTDRAW_PLUGIN),
    ):
        prior = sha256_file(config_file)
        _added, note = ensure_plugin_entry(config_file, plugin)
        say(f"plugin: {note}")
        entries.append(Entry(str(config_file), KIND_CONFIG_ENTRY, plugin, prior_sha256=prior))
    from hr import __version__  # noqa: PLC0415 — avoid widening the package import surface for one string

    receipt = save_receipt(root, __version__, entries)
    say(f"receipt: wrote {receipt} ({len(entries)} entries)")
    say("install-post: done. Start the database with: hr db-up")
    return 0
