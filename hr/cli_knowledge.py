from __future__ import annotations

from typing import Optional

import typer

from .cli_app import _fail, app, console

@app.command()
def reference(
    model: Optional[str] = typer.Argument(
        None, help="model_id to show per-category reference scores for",
    ),
) -> None:
    """Reference knowledge: curated published-benchmark scores per model.

    The curated scores are the SINGLE knowledge store (hr/reference.py);
    without arguments this prints the store summary offline.
    """
    from hr.reference import (
        load_reference_scores,
    )
    scores = load_reference_scores()
    if model:
        cats = scores.get(model)
        if cats is None:
            _fail(f"error: {model!r} not in the reference store")
        console.print(f"[bold]{model}[/bold]")
        for category, (score, confidence, source) in cats.items():
            console.print(f"  • {category}: {score:.1f} (confidence {confidence:.2f}) — {source}")
        return
    console.print(f"[bold]Reference store[/bold]: {len(scores)} models, "
                  f"curated data in configs/knowledge.yaml")
    for model_id, cats in scores.items():
        bits = ", ".join(f"{c}={s:.0f}" for c, (s, _conf, _src) in cats.items())
        console.print(f"  • {model_id}: {bits}")


@app.command()
def research(
    model: Optional[str] = typer.Argument(
        None, help="model_id to show curated research findings for",
    ),
) -> None:
    """Research knowledge: qualitative findings per model (offline store summary).

    Benchmark numbers live in the reference store (configs/knowledge.yaml
    ``reference_scores``) — this command carries the qualitative layer it
    does not repeat.
    """
    from hr.research import load_findings
    findings = load_findings()
    if model:
        entries = findings.get(model)
        if entries is None:
            _fail(f"error: {model!r} not in the research store")
        console.print(f"[bold]{model}[/bold]")
        for category, finding, confidence, url in entries:
            conf = f" ({confidence:.2f})" if confidence else ""
            console.print(f"  • [{category}]{conf} {finding}")
        return
    total = sum(len(v) for v in findings.values())
    console.print(f"[bold]Research store[/bold]: {len(findings)} models, {total} findings "
                  f"(qualitative; benchmark numbers live in the reference store)")
    for model_id, entries in findings.items():
        kinds = ", ".join(sorted({e[0] for e in entries}))
        console.print(f"  • {model_id}: {len(entries)} findings ({kinds})")


@app.command()
def publish() -> None:
    """Publish evaluation reports to Wiki.js (optional target: the wiki
    section of hr.toml).

    Without a wiki section in the root hr.toml the command skips cleanly —
    the wiki is an optional publish target, not an error.
    """
    from hr.publish import publish_from_target, wiki_target

    target = wiki_target()
    if target is None:
        console.print(
            "[yellow]wiki not configured, skipping (add a wiki section to hr.toml "
            "with graphql_url / api_key_file to publish)[/yellow]"
        )
        return
    try:
        publish_from_target(target)
    except RuntimeError as exc:
        _fail(f"error: {exc}")
    console.print("[green]Published to Wiki.js[/green]")


@app.command()
def recommend(
    task: Optional[str] = typer.Option(
        None, "--task",
        help="describe a task to get per-task model rankings instead of seat recommendations",
    ),
    as_json: bool = typer.Option(
        False, "--json",
        help="emit the tri-state recommendation result as JSON (machine-readable)",
    ),
) -> None:
    """Seat recommendations from configs/seats.yaml + recent measurements.

    Derives the seat list from configs/seats.yaml ONLY (no code tables) and
    ranks models per seat under the blended capability prior (see
    docs/en/capability-prior.md). Read-only: verdict owns assignments.

    With --task the command emits a tri-state (eligible / excluded /
    indeterminate) evaluation under the default operational policy: a model
    with missing evidence for an enabled constraint lands in INDETERMINATE,
    never in the eligible list.
    """
    from hr.recommend import (
        RecommendationEngine,
        format_recommendation_result,
        load_seat_specs,
    )

    if task is None:
        try:
            seats = load_seat_specs()
        except Exception as exc:
            _fail(f"error: {exc}")
    else:
        seats = None
    try:
        engine = RecommendationEngine()
    except Exception as exc:
        _fail(f"error: {exc}")
    try:
        if task:
            result = engine.recommend(task)
            if not (result.eligible or result.excluded or result.indeterminate):
                console.print("[yellow](no recommendations returned)[/yellow]")
            else:
                console.print(
                    format_recommendation_result(
                        result, fmt="json" if as_json else "table"
                    )
                )
        else:
            console.print(engine.seat_recommendations(seats))
    except Exception as exc:
        _fail(f"error: {exc}")
    finally:
        engine._conn.close()
