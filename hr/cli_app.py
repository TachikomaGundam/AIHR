from __future__ import annotations

import sys
from typing import NoReturn

import typer
from rich.console import Console

from .decision import latest_sweep_id


def _harden_windows_streams() -> None:
    """Win32 only: replace (never crash) chars the console/pipe codec cannot
    encode. typer's rich help draws box-drawing glyphs that cp1252 lacks — a
    plain ``hr --help`` aborted with UnicodeEncodeError on the CI windows
    runner (release run 35728085524, B-5 first-fire courtroom)."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            pass


_harden_windows_streams()


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
