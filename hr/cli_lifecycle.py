"""``hr install-post`` / ``hr self-uninstall`` — turnkey bundle CLI.

Thin typer wrappers over :mod:`hr.lifecycle` and :mod:`hr.lifecycle_uninstall`
(all logic and its injectable seams live there); wired onto the shipped app
by ``register_lifecycle_commands`` (mirrors hr.cli_db registration style).
"""

from __future__ import annotations

import sys
from typing import Optional

import typer

from .lifecycle import install_post as _install_post
from .lifecycle_uninstall import self_uninstall as _self_uninstall
from .py_fetch import py_fetch_cli as _py_fetch_cli


def install_post(
    port: Optional[int] = typer.Option(
        None,
        "--port",
        help="embedded Postgres port to seed into the bundle db.env "
        "(default: 5433, or the AIHR_DB_PORT of an existing db.env)",
    ),
) -> None:
    """Wire an extracted turnkey bundle into this machine (idempotent).

    Expects the bundle archive already extracted into the owned dir D
    (POSIX ~/.aihr, Windows %LOCALAPPDATA%\\aihr): installs the ``hr`` shim
    into D/bin, puts D/bin on the user PATH, registers the opencode plugins,
    and records every placement in D/receipt.json for ``hr self-uninstall``.
    """
    raise typer.Exit(code=_install_post(port=port))


def self_uninstall(
    yes: bool = typer.Option(
        False,
        "--yes",
        help="confirm removal of the bundle dir D (including the embedded "
        "database data) and every receipt-recorded placement",
    ),
) -> None:
    """Undo install-post via the receipt, delete D, print the residual manifest."""
    raise typer.Exit(code=_self_uninstall(yes=yes))


def py_fetch(
    version: Optional[str] = typer.Option(
        None,
        "--version",
        help="release whose interpreter asset to fetch (default: the receipt version)",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="refetch even when an interpreter already sits at D/py",
    ),
) -> None:
    """Fetch the on-demand code-gen interpreter into the bundle dir (posix).

    Since 0.6.3 the turnkey bundle ships slim (~54MB): the vendored CPython
    that powers code_gen is a separate checksum-pinned release asset. Run
    this after an install-time fetch failed (or was skipped) to enable the
    battery; until then code_gen honestly records not_applicable.
    """
    raise typer.Exit(code=_py_fetch_cli(version=version, force=force))


def register_lifecycle_commands(tp: typer.Typer) -> None:
    """Attach the bundle-lifecycle commands to a typer app."""
    tp.command(name="install-post")(install_post)
    tp.command(name="self-uninstall")(self_uninstall)
    tp.command(name="py-fetch")(py_fetch)


try:  # worktree wiring (mirrors hr.cli_db): attach to the shipped typer app
    from .cli_app import app as _worktree_app  # noqa: PLC0415
except ModuleNotFoundError:  # pragma: no cover — fresh HEAD checkout (cli_app untracked)
    _worktree_app = None

if _worktree_app is not None:
    register_lifecycle_commands(_worktree_app)


if __name__ == "__main__":  # pragma: no cover — direct module smoke only
    sys.exit(0)
