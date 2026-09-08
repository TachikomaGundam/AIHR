"""``hr setup`` — bootstrap the OpenCode plugin half after ``pip install aihr``.

The engine (this wheel) and the npm plugins ship separately; before this
command a user had to run npm by hand AND edit two opencode config files.
``hr setup`` chains both: install the plugin pair from the npm registry at
``@latest`` (fresh installs and upgrades always resolve to the newest
published release — no stale pins to chase), then delegate config
registration to the ``opencode-hr`` CLI that ships inside
``opencode-hr-agent`` (its own tested merge logic — this module never edits
JSON itself, keeping one implementation of the contract).

Security posture (mirrors AGENTS contract, reviewed 2026-09-06): stdlib +
typer + rich only; every subprocess is an argv LIST executed without a shell;
no network access beyond what ``npm install`` itself performs; output never
suggests elevated installs — the EACCES path points at the user-level prefix
recipe documented in the README.

Plugin specs float at ``@latest`` on purpose: one-click setup must never
ship a stale pin. The resolved versions are reported after install
(``_report_resolved_versions``) so a run stays auditable after the fact.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import typer

from . import setup_env

PLUGIN_NAMES: tuple[str, ...] = ("opencode-hr-agent", "opencode-fastdraw")

REGISTRAR_BIN = "opencode-hr"
_NPM_TIMEOUT_S = 600
_CLI_TIMEOUT_S = 60


@dataclass(frozen=True)
class CommandResult:
    rc: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], int], CommandResult]


def default_runner(argv: list[str], timeout: int) -> CommandResult:
    proc = subprocess.run(  # noqa: S603 — argv list, no shell, argv fully controlled above
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def plugin_specs(npm: str = "npm") -> list[str]:
    """Full npm-install argv: floating ``@latest`` specs, PLUGIN_NAMES order.

    ``--include-workspace-root false``: npm >=11 workspaces-install a root
    package.json in the CWD whenever no lockfile is present (npm default
    ``include-workspace-root=true``) — on an aihr checkout that silently also
    installed the ROOT project with the -g plugins (and failed outright on
    the root's ``workspace = true`` marker). Explicit false behaves on npm
    10 and 11; it is a flag-only no-op when the CWD is not a package.
    """
    extra = ["--include-workspace-root", "false"] if Path(npm).name == "npm" else []
    return [npm, "install", "-g", *extra, *[f"{name}@latest" for name in PLUGIN_NAMES]]


def _tail(text: str, limit: int = 3) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return " | ".join(lines[-limit:]) if lines else "(no output)"


def _call(runner: Runner, argv: list[str], timeout: int) -> CommandResult:
    try:
        return runner(argv, timeout)
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"timed out after {timeout}s: {' '.join(argv)}")


def _find_registrar(runner: Runner) -> Optional[str]:
    """Locate the opencode-hr CLI: npm global bin first, then PATH.

    npm puts global executables in ``<prefix>/bin`` on POSIX but in the
    prefix root itself on Windows, named ``opencode-hr.cmd`` — a
    prefix/bin-only lookup could never find the CLI on Windows, which made
    ``hr setup`` fail there even after a successful npm install.
    """
    prefix = _call(runner, ["npm", "prefix", "-g"], _CLI_TIMEOUT_S)
    if prefix.rc == 0 and prefix.stdout.strip():
        root = Path(prefix.stdout.strip())
        names = (f"{REGISTRAR_BIN}.cmd", REGISTRAR_BIN) if os.name == "nt" else (REGISTRAR_BIN,)
        dirs = [root] if os.name == "nt" else [root / "bin"]
        for directory in dirs:
            for name in names:
                candidate = directory / name
                if candidate.exists():
                    return str(candidate)
    return shutil.which(REGISTRAR_BIN)


def _is_eacces(stderr: str) -> bool:
    low = stderr.lower()
    return "eacces" in low or "eperm" in low


def _report_resolved_versions(runner: Runner, say: Callable[[str], None]) -> None:
    """Print the concrete versions ``@latest`` resolved to.

    A floating spec trades startup-time reproducibility for never shipping a
    stale pin; snapshotting the resolved versions right after install puts the
    "auditable after the fact" guarantee back without re-introducing drift.
    """
    res = _call(runner, ["npm", "ls", "-g", "--depth=0", *PLUGIN_NAMES], _CLI_TIMEOUT_S)
    for raw in (res.stdout or res.stderr).splitlines():
        # npm ls decorates lines with tree glyphs ("├── name@1.2.3"); take the
        # name@version token itself, whatever the decoration.
        line = raw.strip()
        for name in PLUGIN_NAMES:
            idx = line.find(name)
            if idx != -1:
                say(f"resolved: {line[idx:].split()[0]}")
                break


def ensure_script_on_path(say: Callable[[str], None], *, persist: bool) -> None:
    """Make ``hr`` callable from fresh shells; every write undone by ``--uninstall``.

    This is the "pip install aihr then ``hr`` is not found" fix: on Windows the
    scripts dir lives in ``…\\Scripts``, on macOS user-site installs in
    ``~/Library/Python/3.X/bin``, and on minimal Linux systems ``~/.local/bin``
    is missing from PATH when no desktop profile adds it — none of those are on
    PATH by default. venv/pipx installs already are, so this is a no-op there.
    """
    scripts_dir = setup_env.console_script_dir()
    if scripts_dir is None:
        say("note: no pip scripts dir with an 'hr' entry point found (venv/pipx install?) — skipping PATH step.")
        return
    if setup_env.on_path(scripts_dir):
        say(f"hr is already on PATH ({scripts_dir}).")
        return
    if not persist:
        say(f"hr is not on PATH; the scripts dir is {scripts_dir}.")
        say("Add it to PATH yourself, or re-run without --no-path (user-scope write, undone by hr setup --uninstall).")
        return
    _changed, message = setup_env.persist_path(scripts_dir)
    say(f"PATH: {message}")


def _npm_argv(action: str) -> list[str]:
    npm = "npm"
    extra = ["--include-workspace-root", "false"] if Path(npm).name == "npm" else []
    return [npm, action, "-g", *extra, *PLUGIN_NAMES]


def _cache_package_dirs() -> list[Path]:
    """opencode's boot-time installs of our two plugins, inside its package cache.

    Matched by exact package prefix only — other plugins cached beside them are
    never touched.
    """
    cache_root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "opencode" / "packages"
    if not cache_root.is_dir():
        return []
    return [
        entry
        for entry in cache_root.iterdir()
        if entry.is_dir() and any(entry.name == name or entry.name.startswith(f"{name}@") for name in PLUGIN_NAMES)
    ]


def _opencode_config_dir() -> Path:
    return Path(os.environ.get("OPENCODE_CONFIG_DIR") or Path.home() / ".config" / "opencode")


def run_uninstall(runner: Runner = default_runner) -> int:
    """Reverse everything ``hr setup`` and the installers added — no residue.

    Order: unregister configs (needs the registrar, so runs before npm
    removal) → drop both npm globals → delete opencode's cached copies of our
    plugins → delete the skill/agent config copies → remove the persisted PATH
    entries → point at the shell-installer layout if present. The engine wheel
    itself is the one thing this command cannot remove while running; the final
    line hands the user the exact ``pip uninstall`` (their step, by design).
    """
    say = print
    rc = 0
    if shutil.which("npm") is None:
        say("SKIP npm steps: npm not found on PATH (uninstall the plugin globals yourself if any).")
    else:
        registrar = _find_registrar(runner)
        if registrar is None:
            say("SKIP config unregister: opencode-hr CLI not found (already removed?).")
        else:
            res = _call(runner, [registrar, "uninstall"], _CLI_TIMEOUT_S)
            for line in (res.stdout or res.stderr).splitlines():
                if line.strip():
                    say(line)
            rc = rc or res.rc
        res = _call(runner, _npm_argv("uninstall"), _NPM_TIMEOUT_S)
        if res.rc != 0:
            say(f"npm uninstall failed (rc={res.rc}): {_tail(res.stderr or res.stdout)}")
            rc = rc or res.rc
        else:
            say("removed npm globals: " + ", ".join(PLUGIN_NAMES))
    for cached in _cache_package_dirs():
        shutil.rmtree(cached)
        say(f"DEL  {cached}")
    cfg = _opencode_config_dir()
    for copied in (cfg / "skills" / "hr-workflow.md", cfg / "agents" / "hr.md"):
        if copied.is_file():
            copied.unlink()
            say(f"DEL  {copied}")
    for scripts_dir in setup_env.scripts_dir_candidates():
        if not scripts_dir.is_dir():
            continue
        changed, message = setup_env.remove_path(scripts_dir)
        if changed:
            say(f"PATH: {message}")
    if (cfg / "plugins" / "fastdraw").is_dir():
        say(f"note: git-path FastDraw layout detected at {cfg / 'plugins' / 'fastdraw'} —")
        say("       run its uninstaller: bash <(curl -fsSL https://raw.githubusercontent.com/TachikomaGundam/AIHR/main/fastdraw/uninstall.sh)")
    say("last step (yours): remove the engine itself with: pip uninstall aihr")
    return rc


def run_setup(runner: Runner = default_runner, install_npm: bool = True, persist_user_path: bool = True) -> int:
    """Execute the whole bootstrap; returns the process exit code."""
    say = print
    if shutil.which("npm") is None:
        say("error: npm not found — install Node.js 22.18+ first (README: Install → Node.js).")
        return 1

    if install_npm:
        say(f"installing plugins: {' '.join(plugin_specs())}")
        res = _call(runner, plugin_specs(), _NPM_TIMEOUT_S)
        if res.rc != 0:
            if _is_eacces(res.stderr):
                say("npm rejected the global install (directory not writable).")
                say("Fix at user level — see README 'Install': set a user npm prefix")
                say("(npm config set prefix ~/.npm-global) and re-run hr setup. Do not run npm as root.")
            else:
                say(f"npm install failed (rc={res.rc}): {_tail(res.stderr or res.stdout)}")
            return 1
        _report_resolved_versions(runner, say)

    registrar = _find_registrar(runner)
    if registrar is None:
        say(f"error: {REGISTRAR_BIN} CLI not found after install — the installed")
        say("opencode-hr-agent may predate plugin auto-registration; run:")
        say(f"  {' '.join(plugin_specs())}   then re-run: hr setup --no-npm")
        return 1

    res = _call(runner, [registrar, "install"], _CLI_TIMEOUT_S)
    for line in (res.stdout or res.stderr).splitlines():
        if line.strip():
            say(line)
    if res.rc != 0:
        return res.rc

    check = _call(runner, [registrar, "status"], _CLI_TIMEOUT_S)
    if check.rc != 0:
        for line in (check.stdout or check.stderr).splitlines():
            if line.strip():
                say(line)
        say("setup incomplete — configs do not list the plugins yet (see MISS lines above).")
        return check.rc
    ensure_script_on_path(say, persist=persist_user_path)
    say("done. Restart opencode; hr_* and fastdraw_* tools (and /fastdraw) should now be present.")
    say("If 'hr' was just added to PATH, open a NEW terminal for the command to resolve.")
    return 0


def setup(
    no_npm: bool = typer.Option(
        False,
        "--no-npm",
        help="Skip the npm install step; only (re-)run opencode config registration.",
    ),
    no_path: bool = typer.Option(
        False,
        "--no-path",
        help="Do not persist the pip scripts dir onto the user PATH; print guidance only.",
    ),
    uninstall: bool = typer.Option(
        False,
        "--uninstall",
        help="Remove everything setup/installers added (configs, npm globals, cache copies, "
        "PATH entries, skill/agent copies). The engine wheel itself: pip uninstall aihr.",
    ),
) -> None:
    """Install the latest OpenCode plugins and register them — one-shot bootstrap."""
    if uninstall:
        raise typer.Exit(code=run_uninstall())
    raise typer.Exit(code=run_setup(install_npm=not no_npm, persist_user_path=not no_path))


try:  # worktree wiring (mirrors hr.cli_apply): attach to the shipped typer app
    from .cli_app import app as _worktree_app  # noqa: PLC0415
except ModuleNotFoundError:  # pragma: no cover — fresh HEAD checkout (cli_app untracked)
    _worktree_app = None

if _worktree_app is not None:
    _worktree_app.command(name="setup")(setup)


if __name__ == "__main__":  # pragma: no cover — direct module smoke only
    sys.exit(run_setup())
