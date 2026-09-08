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

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import typer

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
    """Locate the opencode-hr CLI: npm global bin first, then PATH."""
    prefix = _call(runner, ["npm", "prefix", "-g"], _CLI_TIMEOUT_S)
    if prefix.rc == 0 and prefix.stdout.strip():
        candidate = Path(prefix.stdout.strip()) / "bin" / REGISTRAR_BIN
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


def run_setup(runner: Runner = default_runner, install_npm: bool = True) -> int:
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
    say("done. Restart opencode; hr_* and fastdraw_* tools (and /fastdraw) should now be present.")
    return 0


def setup(
    no_npm: bool = typer.Option(
        False,
        "--no-npm",
        help="Skip the npm install step; only (re-)run opencode config registration.",
    ),
) -> None:
    """Install the latest OpenCode plugins and register them — one-shot bootstrap."""
    raise typer.Exit(code=run_setup(install_npm=not no_npm))


try:  # worktree wiring (mirrors hr.cli_apply): attach to the shipped typer app
    from .cli_app import app as _worktree_app  # noqa: PLC0415
except ModuleNotFoundError:  # pragma: no cover — fresh HEAD checkout (cli_app untracked)
    _worktree_app = None

if _worktree_app is not None:
    _worktree_app.command(name="setup")(setup)


if __name__ == "__main__":  # pragma: no cover — direct module smoke only
    sys.exit(run_setup())
