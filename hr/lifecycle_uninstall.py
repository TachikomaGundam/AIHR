"""Receipt-driven bundle removal: ``hr self-uninstall`` + residual manifest.

The exact inverse of :func:`hr.lifecycle.install_post`: entries are replayed
back-to-front — config entries only when the recorded exact string is still
present, the PATH block through :func:`hr.setup_env.remove_path` (byte-
exact), then the placed files/dirs — before the owned directory ``D`` itself
goes. No receipt means no blind delete: uninstall refuses and prints the
exact manual steps. Whatever survives (bun plugin caches, a pip console
script, D leftovers) is REPORTED with per-OS removal commands, never
silently removed and never treated as an error.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hr import db_embedded, lifecycle, pg_launcher, setup_env
from hr.config_resources import aihr_home_dir, home_dir, opencode_config_dir
from hr.opencfg_editor import array_is_empty, remove_plugin_entry

RESIDUAL_GLOBS: tuple[str, ...] = ("opencode-hr-agent@*", "opencode-fastdraw@*")


@dataclass(frozen=True)
class _Record:
    path: str
    kind: str
    value: str
    prior_sha256: str | None = None


def _entries(receipt: dict) -> list[_Record]:
    out: list[_Record] = []
    for raw in receipt.get("entries", []):
        if isinstance(raw, dict) and isinstance(raw.get("path"), str):
            prior = raw.get("prior_sha256")
            out.append(
                _Record(
                    raw["path"],
                    str(raw.get("kind", "")),
                    str(raw.get("value", "")),
                    prior if isinstance(prior, str) else None,
                )
            )
    return out


def _rm_command(path: Path, windows: bool) -> str:
    text = str(path)
    return f'rmdir /s /q "{text}"' if windows else f"rm -rf '{text}'"


def _reverse(entry: _Record, *, windows: bool, say: Callable[[str], None]) -> None:
    path = Path(entry.path)
    if entry.kind == lifecycle.KIND_CONFIG_ENTRY:
        _changed, message = remove_plugin_entry(path, entry.value)
        say(f"replay: {message}")
        if entry.prior_sha256 is None and array_is_empty(path):
            path.unlink()  # the file itself was created by install-post: placement reversed
            say(f"replay: removed config file we created: {path}")
    elif entry.kind == lifecycle.KIND_PATH_BLOCK:
        _changed, message = setup_env.remove_path(path, platform="win32" if windows else None)
        say(f"replay: {message}")
    elif entry.kind == lifecycle.KIND_FILE and (path.is_file() or path.is_symlink()):
        path.unlink()
        say(f"replay: removed {path}")
    elif entry.kind == lifecycle.KIND_DIR and path.is_dir():
        try:
            path.rmdir()  # only when empty: user-placed content is never swept
            say(f"replay: removed empty dir {path}")
        except OSError:
            say(f"replay: kept non-empty dir {path}")


def _manual_steps(d: Path, *, windows: bool, say: Callable[[str], None]) -> None:
    say(f"error: no usable receipt at {lifecycle.receipt_path(d)} — refusing to delete anything blindly.")
    say("manual removal:")
    pg_ctl = d / "pg" / "bin" / ("pg_ctl.exe" if windows else "pg_ctl")
    say(f"  1. stop the embedded server (if running): {pg_ctl} -D {d / 'data' / 'pgdata'} stop -m fast")
    say(f"  2. remove the bundle dir: {_rm_command(d, windows)}")
    say(
        f"  3. delete the '# BEGIN aihr PATH' … '# END aihr PATH' block containing {d / 'bin'} "
        "from your shell rc files"
        if not windows
        else f"  3. remove {d / 'bin'} from your user PATH (HKCU\\Environment)"
    )
    say(
        f"  4. drop \"{lifecycle.HR_AGENT_PLUGIN}\" from {opencode_config_dir() / 'opencode.jsonc'} "
        f'and "{lifecycle.FASTDRAW_PLUGIN}" from {opencode_config_dir() / "tui.json"}'
    )


_CLEANUP_BAT = (
    "@echo off\r\n"
    'set "TARGET={target}"\r\n'
    "set /a tries=0\r\n"
    ":retry\r\n"
    "ping -n 4 127.0.0.1 >nul\r\n"
    'rmdir /s /q "%TARGET%"\r\n'
    'if exist "%TARGET%" (\r\n'
    "  set /a tries+=1\r\n"
    "  if %tries% lss 20 goto retry\r\n"
    ")\r\n"
    'del "%~f0" >nul 2>&1\r\n'
)


def _spawn_detached(argv: list[str]) -> None:
    subprocess.Popen(
        argv,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _schedule_deferred_removal(root: Path, *, temp_dir: Optional[Path] = None) -> Path:
    """Windows cannot unlink the images of the live process (run 35971371134:
    ``rmtree`` died on WinError 5 over the running exe's own package dir).
    Hand ``D`` to a detached batch outside ``D``: pause, retry ``rmdir`` up
    to 20 times (~60s, covers process teardown and transient AV locks), then
    best-effort self-delete. POSIX unlinks live images and removes ``D``
    inline instead (:func:`shutil.rmtree`)."""
    base = Path(tempfile.gettempdir()) if temp_dir is None else temp_dir
    script = base / f"aihr-cleanup-{os.getpid()}.cmd"
    script.write_text(_CLEANUP_BAT.format(target=root), encoding="ascii")
    _spawn_detached(["cmd", "/c", str(script)])
    return script


def residual_manifest(
    d: Path, *, windows: bool, say: Callable[[str], None], deferred: bool = False
) -> list[Path]:
    """Print (and return) leftovers owned by OTHER layers — reported, not errors."""
    found: list[tuple[str, Path]] = []
    cache_root = home_dir() / ".cache" / "opencode" / "packages"
    for pattern in RESIDUAL_GLOBS:
        found.extend(("bun cache", hit) for hit in sorted(cache_root.glob(pattern)))
    user_hr = home_dir() / ".local" / "bin" / "hr"
    if user_hr.exists():
        found.append(("console script", user_hr))
    if d.exists() and not deferred:
        found.append(("bundle dir", d))
    say("residual manifest (reported, not errors):")
    if not found:
        say("  clean — no residue detected")
    for kind, path in found:
        say(f"  [{kind}] {path} -> remove with: {_rm_command(path, windows)}")
    return [path for _kind, path in found]


def self_uninstall(
    yes: bool = False,
    *,
    d: Optional[Path] = None,
    platform: Optional[str] = None,
    pg_run: pg_launcher.PgRunner = pg_launcher.default_pg_runner,
    say: Callable[[str], None] = print,
) -> int:
    """Undo install-post (receipt-replay), delete ``D``, report residue."""
    root = aihr_home_dir() if d is None else d
    windows = lifecycle.is_windows(platform)
    if not yes:
        say(
            f"error: self-uninstall removes {root} (including the embedded database), "
            "the PATH block and the plugin entries; add --yes to confirm the data loss."
        )
        return 1
    receipt = lifecycle.load_receipt(root)
    if receipt is None:
        _manual_steps(root, windows=windows, say=say)
        return 1
    root_r = pg_launcher.pg_home(root)
    if root_r is not None and db_embedded.installed(root, root=root_r):
        say("embedded: stopping server before removal...")
        db_embedded.down(root, root=root_r, pg_run=pg_run, say=say)
    for entry in reversed(_entries(receipt)):
        _reverse(entry, windows=windows, say=say)
    deferred = False
    if root.exists():
        if windows:
            script = _schedule_deferred_removal(root)
            deferred = True
            say(f"deferred cleanup: {script} owns {root} (removes it within ~60s of process exit)")
        else:
            shutil.rmtree(root)
            say(f"removed bundle dir {root}")
    residual_manifest(root, windows=windows, say=say, deferred=deferred)
    say("self-uninstall: done. The engine wheel itself (if pip-installed): pip uninstall aihr")
    return 0
