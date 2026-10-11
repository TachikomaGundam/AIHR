"""``hr py-fetch`` — the on-demand interpreter lane (0.6.3 slim bundle).

The turnkey bundle no longer embeds the pruned vendored CPython (~29MB);
owner ruling 2026-10-10 made installs that never run code_gen stop paying
that bandwidth. The interpreter ships as its own checksum-pinned release
asset (``aihr-<ver>-py-<os>-<arch>.tar.gz`` + GitHub sidecar). install.sh
fetches it during install; this module is the retry path — download, verify
against the sidecar, place at ``D/py`` so code_gen scores instead of
recording not_applicable.

Fail-closed by construction: nothing is placed unless the sidecar digest
matches the bytes, an existing interpreter is never clobbered by a failed
fetch, and windows (no sandbox lane) refuses before any request.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Optional

REPO = "TachikomaGundam/AIHR"
PY_STAMP_NAME = ".aihr-py-stamp"


def py_asset_name(version: str, os_name: str, arch: str) -> str:
    """Mirror of scripts/bundle_core.py:py_archive_name — runtime cannot
    import the build tooling, so the format lives here and a drift-guard
    test binds the two (tests/test_py_fetch.py)."""
    return f"aihr-{version}-py-{os_name}-{arch}.tar.gz"


def platform_pair(sysname: str, machine: str) -> Optional[tuple[str, str]]:
    """Map (uname-s, uname-m) to the asset's (os, arch) pair; None on
    platforms with no sandbox lane (windows) or unknown CPUs."""
    os_map = {"Linux": "linux", "Darwin": "macos"}
    arch_map = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}
    os_name = os_map.get(sysname)
    if os_name is None:
        return None
    return os_name, arch_map.get(machine, machine)


def read_receipt_version(d: Path) -> Optional[str]:
    try:
        data = json.loads((d / "receipt.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return str(version) if version else None


class FetchError(RuntimeError):
    """Any failure on this lane — the caller reports, never crashes."""


def _requests_download(url: str) -> bytes:
    import requests

    resp = requests.get(url, timeout=180)
    if resp.status_code != 200:
        raise FetchError(f"HTTP {resp.status_code} for {url}")
    return resp.content


def fetch_interpreter(
    *,
    d: Path,
    version: str,
    os_name: str,
    arch: str,
    repo: str = REPO,
    download: Optional[Callable[[str], bytes]] = None,
    say: Callable[[str], None] = print,
) -> int:
    """Place the checksum-pinned interpreter at ``D/py``. Returns 0 on a
    placed (or already-current) interpreter, 1 on any failure — after a
    failure the previous state is untouched."""
    fetch = download or _requests_download
    dest = d / "py"
    stamp = dest / PY_STAMP_NAME
    asset = py_asset_name(version, os_name, arch)
    base = f"https://github.com/{repo}/releases/download/v{version}/{asset}"
    try:
        sidecar = fetch(base + ".sha256").decode("ascii", "replace").split()[0]
        say(f"downloading {asset} ...")
        blob = fetch(base)
        actual = hashlib.sha256(blob).hexdigest()
        if actual != sidecar:
            say(f"checksum mismatch: expected {sidecar}, got {actual} — nothing placed")
            return 1
        with tempfile.TemporaryDirectory(prefix="aihr-py-", dir=str(d)) as tmp:
            archive = Path(tmp) / asset
            archive.write_bytes(blob)
            root = Path(tmp) / "root"
            root.mkdir()
            with tarfile.open(archive, "r:gz") as tf:
                tf.extractall(root, filter="data")
            payload = root / "py"
            if not (payload / "bin" / "python3").is_file():
                say("asset layout unexpected (py/bin/python3 missing) — nothing placed")
                return 1
            # fsync-style durability is the filesystem's job here; the swap
            # is the atomic step that matters: old tree survives until the
            # new one is fully staged.
            shutil.rmtree(dest, ignore_errors=True)
            shutil.move(str(payload), str(dest))
        stamp.write_text(version + "\n", encoding="ascii")
        say(f"interpreter placed at {dest} (code_gen battery enabled)")
        return 0
    except (FetchError, OSError, ValueError) as exc:
        say(f"interpreter fetch failed: {exc} — code_gen stays not_applicable")
        return 1


def py_fetch_cli(*, version: Optional[str] = None, force: bool = False) -> int:
    """Runtime entry for ``hr py-fetch``: resolve D, platform and version,
    then run the lane. Exit codes only — the caller's console is the report."""
    from .config_resources import aihr_home_dir

    d = aihr_home_dir()
    pair = platform_pair(platform.system(), platform.machine())
    if pair is None:
        print("this platform ships no sandbox interpreter lane (posix-only; code_gen is "
              "not_applicable on windows by design)")
        return 1
    os_name, arch = pair
    if not (d / "app" / "hr").exists() and not (d / "app" / "hr.exe").exists():
        print(f"{d} does not look like an AIHR bundle dir (app/hr missing) — install first")
        return 1
    release = version or read_receipt_version(d)
    if not release:
        print("release version unknown — pass --version X.Y.Z (receipt has no version)")
        return 1
    dest = d / "py"
    if not force and (dest / "bin" / "python3").is_file():
        print(f"interpreter already placed at {dest} (use --force to refetch)")
        return 0
    return fetch_interpreter(d=d, version=release, os_name=os_name, arch=arch)
