from __future__ import annotations

import sys
from typing import NoReturn

import typer
from rich.console import Console

from .decision import latest_sweep_id


class _PinnedNameTyper(typer.Typer):
    def __call__(self, *args: object, **kwargs: object) -> object:
        if kwargs.get("prog_name") is None:
            kwargs["prog_name"] = self.info.name or "hr"
        return super().__call__(*args, **kwargs)


app = _PinnedNameTyper(
    name="hr",
    help="HR Agent (人事) — model evaluation and role assignment for oh-my-openagent",
    no_args_is_help=True,
    epilog="Legacy v1 commands evaluate/report/run_all retired; verdict supersedes.",
)
console = Console()


def _runtime_connect():
    from . import cli

    return cli.connect()


def _runtime_load_deployable() -> set[str]:
    from . import cli

    return cli.load_deployable()


def _ensure_schema() -> None:
    from .db import init_schema

    init_schema()


def _fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise typer.Exit(code=1)


def _with_conn(builder) -> None:
    try:
        conn = _runtime_connect()
        try:
            console.print(builder(conn))
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        _fail(f"error: {exc}")


def _resolve_sweep_id(conn, sweep_id: str | None, latest: bool) -> str:
    if not sweep_id or latest:
        return latest_sweep_id(conn)
    return sweep_id
