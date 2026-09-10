"""``hr db-up`` / ``hr db-down`` / ``hr db-status`` — turnkey database CLI.

Thin typer wrappers over :mod:`hr.db_admin` (all logic and its injectable
seams live there); wired onto the shipped app by ``register_db_commands``
(mirrors hr.cli_apply / hr.deployment_manager registration style).
"""

from __future__ import annotations

import sys
from typing import Optional

import typer

from .db_admin import db_down as _db_down
from .db_admin import db_status as _db_status
from .db_admin import db_up as _db_up


def db_up(
    port: Optional[int] = typer.Option(
        None,
        "--port",
        help="host port to publish on 127.0.0.1 (default: 5433, or the "
        "AIHR_DB_PORT of an existing db env file)",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="take over an existing aihr-db container even when a different "
        "install's compose file (or no compose label) manages it; overrides "
        "only that ownership refusal, nothing else",
    ),
) -> None:
    """Bring up the AIHR-owned PostgreSQL container and initialize the schema.

    Idempotent: generates and persists a random password (docker/.env, mode
    0600) only when missing; never rewrites it. Refuses to recreate an
    aihr-db container owned by another install unless --force is given.
    """
    raise typer.Exit(code=_db_up(port=port, force=force))


def db_down(
    purge: bool = typer.Option(
        False,
        "--purge",
        help="remove the container AND the aihr-db-data volume (needs --yes)",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="confirm destructive --purge; without it nothing is deleted",
    ),
) -> None:
    """Stop the AIHR database container (--purge --yes also drops its volume)."""
    raise typer.Exit(code=_db_down(purge=purge, yes=yes))


def db_status() -> None:
    """Container state + live SQL probe + hr-schema stats (secrets masked)."""
    raise typer.Exit(code=_db_status())


def register_db_commands(tp: typer.Typer) -> None:
    """Attach the database-lifecycle commands to a typer app."""
    tp.command(name="db-up")(db_up)
    tp.command(name="db-down")(db_down)
    tp.command(name="db-status")(db_status)


try:  # worktree wiring (mirrors hr.cli_apply): attach to the shipped typer app
    from .cli_app import app as _worktree_app  # noqa: PLC0415
except ModuleNotFoundError:  # pragma: no cover — fresh HEAD checkout (cli_app untracked)
    _worktree_app = None

if _worktree_app is not None:
    register_db_commands(_worktree_app)


if __name__ == "__main__":  # pragma: no cover — direct module smoke only
    sys.exit(0)
