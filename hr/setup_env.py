"""PATH persistence for the ``hr`` console script — byte-reversible.

``hr setup`` uses this when pip installed ``aihr`` but ``hr`` is not
callable: the scripts directory (Windows ``…\\Scripts``, macOS user-site
``~/Library/Python/3.X/bin``, Linux ``~/.local/bin``) is appended to the
*user* PATH. Every write has an exact inverse in :func:`remove_path`,
driven by ``hr setup --uninstall`` — the no-residue contract.

OS scopes (never elevated, never system-wide):

* Windows: ``HKCU\\Environment`` ``Path`` only. The registry value type is
  preserved (``REG_EXPAND_SZ`` entries like ``%USERPROFILE%`` in the rest
  of PATH survive verbatim) and a best-effort ``WM_SETTINGCHANGE`` broadcast
  tells desktop shells to reload.
* POSIX: a marked block (``# BEGIN aihr PATH`` … ``# END aihr PATH``) is
  appended to every existing shell rc file among the candidates; removal
  deletes exactly the block that was written, nothing else. If no rc file
  exists at all, ``~/.profile`` is created (the sole file this module may
  create) and is deleted again by removal when left empty.

All impure inputs are injectable (``platform``, ``home``, ``environ``,
``reg_mod``) so unit tests on any machine can drive every OS branch without
a real registry, real rc files or a real pip install.
"""

from __future__ import annotations

import os
import sys
import sysconfig
from pathlib import Path
from types import ModuleType
from typing import Callable, Mapping, Optional, Protocol

MARK_BEGIN = "# BEGIN aihr PATH (hr setup)"
MARK_END = "# END aihr PATH (hr setup)"
_RC_CANDIDATES: tuple[str, ...] = (".zshrc", ".bashrc", ".profile", ".bash_profile")

_RESULT_NOT_FOUND = "not-found"
_RESULT_UNCHANGED = "unchanged"


def is_windows(platform: Optional[str] = None) -> bool:
    return (platform or sys.platform) == "win32"


def scripts_dir_candidates(platform: Optional[str] = None) -> list[Path]:
    """Directories pip drops ``hr``/``hr.exe`` into, most-likely first.

    ``scripts`` (current interpreter: venv/global) plus the per-OS user-site
    scheme (``nt_user`` / ``posix_user``) which is where ``pip install --user``
    and pip's automatic user-site fallback on unprotected system Python end
    up — the exact locations that are typically missing from PATH.
    """
    scheme = "nt_user" if is_windows(platform) else "posix_user"
    out: list[Path] = []
    raws: list[str] = []
    try:
        raws.append(sysconfig.get_path("scripts"))
    except KeyError:  # pragma: no cover — exotic builds without the scheme
        pass
    try:
        raws.append(sysconfig.get_path("scripts", scheme))
    except KeyError:  # pragma: no cover — exotic builds without the scheme
        pass
    for raw in raws:
        if raw:
            cand = Path(raw)
            if cand not in out:
                out.append(cand)
    return out


def console_script_dir(
    exe: str = "hr",
    platform: Optional[str] = None,
) -> Optional[Path]:
    """The scripts dir that actually holds the installed entry point, if any."""
    names = (f"{exe}.exe", exe) if is_windows(platform) else (exe, f"{exe}.exe")
    for cand in scripts_dir_candidates(platform):
        for name in names:
            if (cand / name).is_file():
                return cand
    return None


def path_entries(
    environ: Optional[Mapping[str, str]] = None,
    platform: Optional[str] = None,
) -> list[str]:
    env = environ if environ is not None else os.environ
    sep = ";" if is_windows(platform) else ":"
    return [p for p in env.get("PATH", "").split(sep) if p]


def on_path(
    target: Path,
    platform: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> bool:
    """Exact-directory membership test (case-insensitive on Windows)."""
    want = _norm(target, platform)
    return any(_norm_str(entry, platform) == want for entry in path_entries(environ, platform))


def _norm(path: Path, platform: Optional[str]) -> str:
    return _norm_str(str(path), platform)


def _norm_str(value: str, platform: Optional[str]) -> str:
    text = value.rstrip("\\/").rstrip(";")
    return text.lower() if is_windows(platform) else text


# --------------------------------------------------------------------------
# POSIX: marked rc-file blocks
# --------------------------------------------------------------------------


def _rc_block(target: Path) -> str:
    line = str(target)
    return (
        f"{MARK_BEGIN}\n"
        f'case ":$PATH:" in *":{line}:"*) ;;\n'
        f'*) export PATH="{line}:$PATH";;\n'
        f"esac\n"
        f"{MARK_END}\n"
    )


def posix_rc_files(home: Path) -> list[Path]:
    """Existing rc candidates; ``~/.profile`` when the home has none."""
    found = [home / name for name in _RC_CANDIDATES if (home / name).is_file()]
    return found or [home / ".profile"]


def persist_posix(target: Path, home: Path) -> tuple[bool, str]:
    """Append the marked block to each candidate rc file (idempotent)."""
    block = _rc_block(target)
    touched: list[str] = []
    for rc in posix_rc_files(home):
        if not rc.is_file():
            rc.parent.mkdir(parents=True, exist_ok=True)
            rc.write_text("", encoding="utf-8")
        text = rc.read_text(encoding="utf-8")
        if block in text:
            continue
        prefix = "" if (text == "" or text.endswith("\n")) else "\n"
        rc.write_text(text + prefix + block, encoding="utf-8")
        touched.append(rc.name)
    if not touched:
        return False, f"PATH block already present (target: {target})"
    return True, f"added PATH block to {', '.join(touched)} (target: {target}) — new shells pick it up"


def remove_posix(target: Path, home: Path) -> tuple[bool, str]:
    """Delete exactly the block persist_posix wrote; drop ``.profile`` it created empty."""
    block = _rc_block(target)
    removed_from: list[str] = []
    for rc in [home / name for name in _RC_CANDIDATES]:
        if not rc.is_file():
            continue
        text = rc.read_text(encoding="utf-8")
        if block not in text:
            continue
        rc.write_text(text.replace(block, "", 1), encoding="utf-8")
        removed_from.append(rc.name)
    if not removed_from:
        return False, f"no PATH block to remove (target: {target})"
    # A .profile created by persist_posix and now empty again is our residue.
    profile = home / ".profile"
    if profile.is_file() and profile.read_text(encoding="utf-8").strip() == "":
        profile.unlink()
        removed_from.append(".profile (empty, deleted)")
    return True, f"removed PATH block from {', '.join(removed_from)}"


# --------------------------------------------------------------------------
# Windows: HKCU\Environment\Path
# --------------------------------------------------------------------------


class _WinRegLike(Protocol):
    """Structural contract for the winreg module or an injected test fake.

    Registry constants (``HKEY_CURRENT_USER``, ``REG_*``, ``KEY_*``) are
    deliberately NOT declared: injected fakes may type them loosely (e.g.
    ``HKEY_CURRENT_USER = "HKCU"``), so callers keep reading them through
    ``getattr(reg, name, default)``.

    Parameters are positional-only on purpose: the real ``winreg`` module and
    test fakes name these arguments differently; every call site passes them
    positionally, so matching by position is the correct structural contract.
    """

    def OpenKey(self, root: object, sub: str, reserved: int, flags: int, /) -> object: ...
    def QueryValueEx(self, key: object, name: str, /) -> tuple[object, int]: ...
    def SetValueEx(self, key: object, name: str, reserved: int, typ: int, value: str, /) -> None: ...
    def CloseKey(self, key: object, /) -> None: ...


def persist_win(
    target: Path,
    reg_mod: _WinRegLike | None = None,
    broadcaster: Optional[Callable[[], object]] = None,
) -> tuple[bool, str]:
    reg = _import_winreg(reg_mod)
    entries, value_type = _read_user_path(reg)
    want = _norm(target, "win32")
    if any(_norm_str(entry, "win32") == want for entry in entries):
        return False, f"{target} already on user PATH (HKCU)"
    _write_user_path(reg, [*entries, str(target)], value_type)
    _broadcast(reg, broadcaster)
    return True, f"appended {target} to user PATH (HKCU\\Environment) — reopen terminals"


def remove_win(
    target: Path,
    reg_mod: _WinRegLike | None = None,
    broadcaster: Optional[Callable[[], object]] = None,
) -> tuple[bool, str]:
    reg = _import_winreg(reg_mod)
    entries, value_type = _read_user_path(reg)
    want = _norm(target, "win32")
    kept = [e for e in entries if _norm_str(e, "win32") != want]
    if len(kept) == len(entries):
        return False, f"{target} not present in user PATH (HKCU) — nothing to remove"
    _write_user_path(reg, kept, value_type)
    _broadcast(reg, broadcaster)
    return True, f"removed {target} from user PATH (HKCU\\Environment); other entries untouched"


def _import_winreg(reg_mod: _WinRegLike | None) -> _WinRegLike:
    if reg_mod is not None:
        return reg_mod
    import winreg  # noqa: PLC0415 — Windows-only; injected fakes elsewhere

    return _WinregAdapter(winreg)


class _WinregAdapter:
    """Positional adapter making the real ``winreg`` module satisfy :class:`_WinRegLike`.

    ``winreg``'s module-level functions name/type their parameters
    differently (and are checked contravariantly by the type checker), so we
    forward positionally instead of asserting compatibility. Constants stay
    ``getattr``-read through ``__getattr__``.
    """

    def __init__(self, mod: ModuleType) -> None:
        self._mod = mod

    def __getattr__(self, name: str) -> object:
        return getattr(self._mod, name)

    def OpenKey(self, root: object, sub: str, reserved: int, flags: int, /) -> object:  # noqa: N802
        return self._mod.OpenKey(root, sub, reserved, flags)

    def QueryValueEx(self, key: object, name: str, /) -> tuple[object, int]:  # noqa: N802
        return self._mod.QueryValueEx(key, name)

    def SetValueEx(self, key: object, name: str, reserved: int, typ: int, value: str, /) -> None:  # noqa: N802
        self._mod.SetValueEx(key, name, reserved, typ, value)

    def CloseKey(self, key: object, /) -> None:  # noqa: N802
        self._mod.CloseKey(key)


def _read_user_path(reg: _WinRegLike) -> tuple[list[str], int]:
    key = _open_env_key(reg)
    try:
        value, value_type = reg.QueryValueEx(key, "Path")
    except FileNotFoundError:
        value, value_type = "", int(getattr(reg, "REG_EXPAND_SZ", 2))
    finally:
        reg.CloseKey(key)
    return [p for p in str(value).split(";") if p], int(value_type)


def _write_user_path(reg: _WinRegLike, entries: list[str], value_type: int) -> None:
    # Expandable type keeps %VAR% entries written by other tools alive.
    safe_type = value_type if value_type in (int(getattr(reg, "REG_SZ", 1)), int(getattr(reg, "REG_EXPAND_SZ", 2))) else int(getattr(reg, "REG_EXPAND_SZ", 2))
    key = _open_env_key(reg)
    try:
        reg.SetValueEx(
            key, "Path", 0, safe_type, ";".join(entries)
        )
    finally:
        reg.CloseKey(key)


def _open_env_key(reg: _WinRegLike) -> object:
    return reg.OpenKey(
        getattr(reg, "HKEY_CURRENT_USER"),
        "Environment",
        0,
        getattr(reg, "KEY_QUERY_VALUE", 1) | getattr(reg, "KEY_SET_VALUE", 2),
    )


def _broadcast(reg: _WinRegLike, broadcaster: Optional[Callable[[], object]]) -> None:
    """WM_SETTINGCHANGE so running desktop shells reload HKCU Environment.

    Best-effort by design: a failure here never fails setup — worst case the
    user reopens a terminal (documented). ``broadcaster`` is the test seam.
    """
    del reg  # the seam replaces the whole behaviour; real path uses ctypes below
    if broadcaster is not None:
        broadcaster()
        return
    try:
        import ctypes  # noqa: PLC0415

        hwnd_broadcast = 0xFFFF
        wm_settingchange = 0x001A
        smto_abort_if_timeout = 0x0002
        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined]
            hwnd_broadcast,
            wm_settingchange,
            0,
            ctypes.c_wchar_p("Environment"),
            smto_abort_if_timeout,
            5000,
            ctypes.byref(result),
        )
    except Exception:  # noqa: BLE001 — headless/session-0/any failure is fine
        pass


# --------------------------------------------------------------------------
# OS dispatch facade (used by hr/cli_setup)
# --------------------------------------------------------------------------


def persist_path(
    target: Path,
    *,
    platform: Optional[str] = None,
    home: Optional[Path] = None,
    reg_mod: _WinRegLike | None = None,
) -> tuple[bool, str]:
    if is_windows(platform):
        return persist_win(target, reg_mod=reg_mod)
    return persist_posix(target, home if home is not None else Path.home())


def remove_path(
    target: Path,
    *,
    platform: Optional[str] = None,
    home: Optional[Path] = None,
    reg_mod: _WinRegLike | None = None,
) -> tuple[bool, str]:
    if is_windows(platform):
        return remove_win(target, reg_mod=reg_mod)
    return remove_posix(target, home if home is not None else Path.home())
