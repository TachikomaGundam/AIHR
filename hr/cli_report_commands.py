from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from .cli_app import (
    _resolve_sweep_id,
    _runtime_load_deployable,
    _with_conn,
    app,
)
from .cli_report_base import build_health_report, build_sweeps_report
from .cli_report_verdict import build_verdict_report

@app.command()
def verdict(
    sweep: Optional[str] = typer.Option(
        None, "--sweep", help="sweep id to mine (default: latest)"
    ),
    latest: bool = typer.Option(
        False, "--latest", help="use the sweep with the most measurements"
    ),
    include_retired: bool = typer.Option(
        False, "--include-retired",
        help="assign from the full pool anyway; retired entries are tagged ⚠ "
             "(default: never assign retired models, iron rule 5)",
    ),
) -> None:
    """Comprehensive verdict: capability averages + health + gates + assignment."""
    if sweep and latest:
        raise typer.BadParameter("--sweep and --latest are mutually exclusive")
    _with_conn(
        lambda conn: build_verdict_report(
            conn,
            _resolve_sweep_id(conn, sweep, latest),
            include_retired=include_retired,
            deployable=_runtime_load_deployable(),
        )
    )


@app.command()
def health(
    sweep: Optional[str] = typer.Option(
        None, "--sweep", help="sweep id to mine (default: latest)"
    ),
    latest: bool = typer.Option(
        False, "--latest", help="use the sweep with the most measurements"
    ),
    cap: Optional[int] = typer.Option(
        None, "--cap",
        help="fallback truncation cap (tokens_out) for rows that don't "
             "record their own requested max_output",
    ),
) -> None:
    """Full-pool behavioral-health markdown table (DB-only, zero API calls)."""
    if sweep and latest:
        raise typer.BadParameter("--sweep and --latest are mutually exclusive")
    _with_conn(
        lambda conn: build_health_report(
            conn,
            _resolve_sweep_id(conn, sweep, latest),
            cap=cap,
            deployable=_runtime_load_deployable(),
        )
    )


@app.command()
def sweeps() -> None:
    """List sweeps from the DB with run/model/measurement counts."""
    _with_conn(build_sweeps_report)


@app.command()
def calibrate(
    item_repo: Optional[Path] = typer.Option(
        None, "--item-repo", help="path to the item repo (default: HR_ITEMREPO env or HR_HOME/itemrepo)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="print the call plan WITHOUT calling APIs, and exit"
    ),
    anchors: Optional[str] = typer.Option(
        None, "--anchors", help="comma-separated anchor keys (e.g. 'cheap,mid,expensive')"
    ),
    batteries: Optional[str] = typer.Option(
        None, "--batteries", help="comma-separated battery names (default: all Stage-0)"
    ),
    token_cap: Optional[int] = typer.Option(
        None, "--token-cap", help="token budget cap (default: engine's TOKEN_CAP)"
    ),
    resume: bool = typer.Option(
        False, "--resume", help="skip (anchor, item) pairs already recorded"
    ),
    json_out: bool = typer.Option(
        False, "--json", help="print the JSON report after the run"
    ),
) -> None:
    """Run stage-0 anchor calibration (delegates to the hr.calibrate engine).

    The engine keeps its own arg parser (hr/calibrate.py, untouched); this
    command translates typer flags to the engine's argv and passes its exit
    code through. No argparse lives in this module.
    """
    argv: list[str] = []
    if item_repo is not None:
        argv += ["--item-repo", str(item_repo)]
    if dry_run:
        argv.append("--dry-run")
    if anchors is not None:
        argv += ["--anchors", anchors]
    if batteries is not None:
        argv += ["--batteries", batteries]
    if token_cap is not None:
        argv += ["--token-cap", str(token_cap)]
    if resume:
        argv.append("--resume")
    if json_out:
        argv.append("--json")
    from hr.calibrate import _cli

    raise typer.Exit(code=_cli(argv))

