"""hr preflight - S3 first-shot gate: run a budgeted control session and
decide, mechanically, whether the endpoint fleet and the measurement chain
are healthy enough for a real sweep to collect measurements."""
from __future__ import annotations

import json
from typing import Optional

import typer

from .cli_app import app, console


@app.command()
def preflight(
    models: Optional[str] = typer.Option(
        None, "--models", help="comma-separated anchor model ids (default: configs/preflight.yaml models)"),
    batteries: Optional[str] = typer.Option(
        None, "--batteries", help="comma-separated batteries (default: preflight.yaml batteries)"),
    as_json: bool = typer.Option(False, "--json", help="machine-readable verdict"),
) -> None:
    """Run the preflight control session.

    Exit 0 = gates pass, freshness marker written (a real sweep may
    start); 1 = a gate failed - the chain or an anchor band is broken;
    2 = cannot answer (infrastructure sick or budget cap reached) -
    affected cells are named for re-queue, never scored.
    """
    from hr.db import connect
    from hr.preflight import load_preflight_config, marker_age_hours, run_preflight, write_marker

    cfg = load_preflight_config()
    model_list = (
        [m.strip() for m in models.split(",") if m.strip()] if models else list(cfg.default_models)
    )
    battery_list = (
        [b.strip() for b in batteries.split(",") if b.strip()]
        if batteries else list(cfg.default_batteries)
    )
    if not model_list:
        console.print(
            "[red]no anchor models: pass --models or set models in configs/preflight.local.yaml[/red]"
        )
        raise typer.Exit(code=2)

    from hr.adapters import adapter_for
    from hr.bench.engine import LivebenchEngine
    from hr.models import BenchmarkCategory
    from hr.preflight import with_output_cap

    engine = LivebenchEngine(adapter_factory=with_output_cap(adapter_for, cfg.per_request_max_tokens))
    conn = connect()

    def store(sweep_id: str, model_id: str, battery_key: str, outcome) -> None:
        # The engine's own storage seam: scored cells write run+measurement
        # rows; inconclusive cells write a run row + infra incident and NO
        # measurement - the non-measurement invariant comes for free.
        engine.store(conn, sweep_id, model_id, BenchmarkCategory(battery_key), outcome)

    def read_back(sweep_id: str) -> int:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM hr.run WHERE sweep_id = %s", (sweep_id,))
            row = cur.fetchone()
            return int(row[0]) if row else 0

    run = run_preflight(
        engine,
        models=model_list,
        batteries=battery_list,
        cfg=cfg,
        store=store,
        read_back=read_back,
        marker_row=lambda sid: write_marker(conn, sid),
    )
    age = marker_age_hours(conn)
    payload = {
        "verdict": run.judgment.verdict,
        "sweep_id": run.sweep_id,
        "g1_violations": list(run.judgment.g1_violations),
        "g2_violations": list(run.judgment.g2_violations),
        "g3_readback_ok": run.g3_ok,
        "error_rate": round(run.judgment.error_rate, 4),
        "requeue": list(run.judgment.requeue),
        "cell_scores": {
            f"{c.model_id}|{c.battery}": (round(c.score, 4) if c.scored and c.score is not None else None)
            for c in run.cells
        },
        "requests_used": run.budget.requests,
        "tokens_used": run.budget.tokens,
        "marker_age_hours": None if age is None else round(age, 3),
        "summary": run.judgment.summary,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        console.print(f"preflight {run.judgment.verdict.upper()}: {run.judgment.summary}")
        for line in (*run.judgment.g1_violations, *run.judgment.g2_violations, *run.judgment.requeue):
            console.print(f"  - {line}")
    raise typer.Exit(code={"pass": 0, "fail": 1, "cannot-answer": 2}[run.judgment.verdict])


__all__ = ["preflight"]
