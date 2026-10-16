"""hr parity - generation-parity flag: calibration anchors vs a live fleet roster."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from .cli_app import app


@app.command()
def parity(
    fleet: Optional[str] = typer.Option(
        None, "--fleet", help="comma-separated live model ids (provider/model)"),
    fleet_file: Optional[Path] = typer.Option(
        None, "--fleet-file", help="file of model ids, one per line, # comments allowed"),
) -> None:
    """Flag anchor-generation drift against the fleet roster.

    Exit 0 = anchors same generation as fleet; 1 = drift or stale manifest
    (reseating needed); 2 = inputs unusable (fail loud, never a fake clean).
    """
    ids: list[str] = []
    if fleet:
        ids.extend(t.strip() for t in fleet.split(",") if t.strip())
    if fleet_file is not None:
        try:
            text = fleet_file.read_text(encoding="utf-8")
        except OSError as exc:
            typer.secho(f"fleet file unreadable: {exc}", fg="red", err=True)
            raise typer.Exit(code=2) from exc
        for line in text.splitlines():
            token = line.split("#", 1)[0].strip()
            if token:
                ids.extend(t.strip() for t in token.split(",") if t.strip())
    if not ids:
        typer.secho("no fleet ids given: pass --fleet or --fleet-file "
                    "(roster of model ids, one per line)", fg="red", err=True)
        raise typer.Exit(code=2)

    from .calibration_items import load_anchors
    from .parity import check_parity

    try:
        report = check_parity(load_anchors(), ids)
    except Exception as exc:  # gate boundary: report and fail loud, never guess
        typer.secho(f"parity check failed: {exc}", fg="red", err=True)
        raise typer.Exit(code=2) from exc

    def gen(g: object) -> str:
        version = getattr(g, "version", None)
        return ".".join(str(v) for v in version) if version else "?"

    for key in report.clean:
        typer.echo(f"clean:        {key}")
    for d in report.drifted:
        typer.secho(f"DRIFT:        {d.anchor_key}: {d.anchor.raw} (gen {gen(d.anchor)})"
                    f" -> {d.successor.raw} (gen {gen(d.successor)}) [{d.reason}]",
                    fg="yellow")
    for flag in report.incomparable:
        typer.secho(f"uncomparable: {flag}", fg="cyan")
    if report.stale_manifest:
        typer.secho(f"stale manifest: {report.stale_manifest}", fg="red")
    raise typer.Exit(code=1 if report.seating_stale else 0)
