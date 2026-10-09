from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .bench.engine_results import BenchOutcome

import typer

from .cli_app import (
    _ensure_schema,
    _fail,
    _runtime_connect,
    _runtime_load_deployable,
    app,
    console,
)
from .models import BenchmarkCategory

@app.command("db-dsn")
def db_dsn_explain() -> None:
    """Show WHICH resolution step supplies the database connection.

    Passwords are never printed; host:port/database only. A stale db.env or
    compose file silently outranking the intended target is a real failure
    mode observed on customer-shaped machines (dev-box archive 2026-10-05) —
    this makes the winner visible.
    """
    from .config import db_dsn_with_source

    try:
        dsn, source = db_dsn_with_source()
    except RuntimeError as exc:  # nothing resolved: the attempted-steps list IS the UX
        typer.echo(str(exc))
        raise typer.Exit(code=1) from None
    target = dsn.rsplit("@", 1)[-1] if "@" in dsn else dsn.split("://", 1)[-1]
    typer.echo(f"DSN source: {source}")
    typer.echo(f"target: {target}")


@app.command()
def discover(
    all_models: bool = typer.Option(
        False,
        "--all",
        help="list every configured provider/model, including out-of-scope "
             "ones (marked as such)",
    ),
) -> None:
    """Enumerate providers and models from opencode.jsonc into PostgreSQL.

    FastDraw-style config derivation: parses the project + global
    opencode.jsonc provider blocks (JSONC-tolerant) and annotates each model
    with scope (every discovered provider minus the OPTIONAL
    ``scope_excludes:`` list in configs/fleet.yaml — new providers
    auto-inherit the default scope) and auth presence (auth-v2.json,
    falling back to auth.json). Upserts into the model registry —
    idempotent (ON CONFLICT DO NOTHING); the legacy v1 model table is never
    written from this command.

    Limitation: npm-spec / remote-registry providers that live outside the
    config files (e.g. kimi-for-coding / deepseek, auth keys only) are NOT
    enumerated — hr runs outside opencode, so there is no live
    api.state.provider runtime state to read (static config parse only).
    Stage fleets still reach those models via configs/deployable.yaml
    ``extra_deployable:``.
    """
    from .discover import enumerate_models, scope_providers, upsert_models

    try:
        scope = scope_providers()
        models = enumerate_models(scope)
    except ValueError as exc:
        _fail(f"error: {exc}")
    if not all_models:
        models = [m for m in models if m.in_scope]
    if not models:
        console.print("[yellow]No models found in opencode.jsonc configs[/yellow]")
        return
    try:
        conn = _runtime_connect()
        try:
            providers, rows = upsert_models(conn, models)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — CLI boundary: report the error
        _fail(f"error: {exc}")
    for model in models:
        # parenthesized tags: square-bracket words would be eaten by rich markup
        scope_tag = "(in scope)" if model.in_scope else "(out of scope)"
        auth_tag = "(auth: yes)" if model.auth_present else "(auth: no)"
        console.print(
            f"  • [cyan]{model.provider}[/cyan]/{model.model_id} "
            f"{scope_tag} {auth_tag}",
        )
    console.print(
        f"[green]Discovered {len(models)} model(s); "
        f"upserted {providers} provider(s), {rows} model row(s) "
        f"into hr[/green]",
    )


@app.command()
def seed() -> None:
    """Initialize the schema and seed canonical seat definitions."""
    from hr.seats.seed import seed_seats

    _ensure_schema()
    conn = _runtime_connect()
    try:
        count = seed_seats(conn)
    finally:
        conn.close()
    console.print(f"[green]Seeded {count} seats[/green]")


from .cli_selection import _pick_models_interactive
def bench_row_markdown(model_id: str, code: str, outcome: object) -> str:
    """One table row, honest by status: SKIP and INCONCLUSIVE never render
    as a scored 0/FAIL (191 misread incident: capability skips displayed as
    'FAIL 0/13' and were read as model failure)."""
    status = getattr(outcome, "status", "scored")
    latency = getattr(outcome, "latency_ms", 0)
    tokens = getattr(outcome, "tokens_in", 0) + getattr(outcome, "tokens_out", 0)
    if status == "not_applicable":
        reason = (getattr(outcome, "raw_output", "") or "capability gap").removeprefix("SKIP: ")
        return f"| {model_id} | {code} | — | SKIP({reason}) | — | — | — |"
    if status == "inconclusive":
        reason = (getattr(outcome, "raw_output", "") or "no gradeable output").removeprefix("INCONCLUSIVE: ")
        return f"| {model_id} | {code} | — | INCONCLUSIVE({reason}) | — | {latency} | {tokens} |"
    items = getattr(outcome, "items", []) or []
    ok = len(items) and all(i.passed for i in items)
    items_txt = f"{sum(1 for i in items if i.passed)}/{len(items)}"
    score = getattr(outcome, "score", 0.0)
    return (
        f"| {model_id} | {code} | {score:.1f} | "
        f"{'PASS' if ok else 'FAIL'} | {items_txt} | {latency} | {tokens} |"
    )


def _tally(counts: tuple[int, int, int], outcome: BenchOutcome) -> tuple[int, int, int]:
    """#13: summary counts must mirror what store() persists — non-scored
    outcomes keep a run row but zero measurement rows, and a below-gate
    score is not an infrastructure failure. Pure so the rule is testable."""
    n_measurements, n_failed, n_skipped = counts
    if outcome.status == "scored":
        n_measurements += len(outcome.items)
        if not (len(outcome.items) and all(i.passed for i in outcome.items)):
            n_failed += 1
    else:
        n_skipped += 1
    return n_measurements, n_failed, n_skipped


@app.command()
def bench(
    models: Optional[str] = typer.Option(
        None, "--models",
        help="comma-separated model ids to benchmark "
             "(default: the deployable fleet from opencode.jsonc + configs/deployable.yaml)",
    ),
    battery: Optional[BenchmarkCategory] = typer.Option(
        None, "--battery",
        help="run a single benchmark battery (default: all 10 livebench batteries)",
    ),
    quick: bool = typer.Option(
        False, "--quick",
        help="fast lane for release windows: 5 decision-critical batteries "
             "(speed, instruction_follow, reasoning, tool_use, long_context), "
             "one round each (~30-60 min vs full ~2h); --battery wins if both given",
    ),
    pick: bool = typer.Option(
        False, "--pick",
        help="interactively pick models from the discovered opencode.jsonc "
             "model list (numbered menu, comma/range selection)",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="print the selected models and exit (no engine run, no DB writes)",
    ),
) -> None:
    """Run the live capability benchmarks and record measurement rows.

    Batteries (results land under the matching livebench_<name> battery):
    code_gen (13 tests + SIGALRM perf gate), reasoning (13 questions),
    instruction_follow (16 constraints), tool_use (calculate loop, 105.63),
    long_context (3 needles + 3 decoys @ 240K chars),
    attention_probe (8 probes: 5-band position sweep + assoc pair +
    distractor resistance @ 240K chars),
    attention_stress (4 checkpoints: 5-constraint survival over a 20-turn
    scripted conversation), vision (PNG 2x2), speed (tok/s tiers),
    long_horizon (CPM over 6 tasks).

    Model selection: --pick reads a numbered menu (the same in-scope
    enumeration ``hr discover`` prints) and lets you pick by comma/range
    (e.g. ``1,3,5-7``) from stdin; --models is the non-interactive
    alternative; neither means the deployable fleet. --dry-run prints the
    resolved selection and exits — no engine, no DB writes.

    All model calls go through the unified hr.adapters (ChatRequest, no
    temperature).
    """
    from hr.bench import LIVEBENCH_BATTERIES, LivebenchEngine, battery_code, make_sweep_id

    batteries: list[BenchmarkCategory] = (
        [battery] if battery is not None
        else (
            [
                BenchmarkCategory.speed,
                BenchmarkCategory.instruction_follow,
                BenchmarkCategory.reasoning,
                BenchmarkCategory.tool_use,
                BenchmarkCategory.long_context,
            ]
            if quick
            else list(LIVEBENCH_BATTERIES)
        )
    )
    if pick and models:
        _fail("error: --pick and --models are mutually exclusive "
              "(pick interactively or pass --models)")
    if pick:
        from .discover import enumerate_models, scope_providers

        try:
            discovered = [
                m for m in enumerate_models(scope_providers()) if m.in_scope
            ]
        except ValueError as exc:
            _fail(f"error: {exc}")
        if not discovered:
            _fail("error: no models discovered in opencode.jsonc configs "
                  "(nothing to pick)")
        model_ids = _pick_models_interactive(discovered)
    else:
        model_ids = (
            [m.strip() for m in models.split(",") if m.strip()]
            if models
            else sorted(_runtime_load_deployable())
        )
    if not model_ids:
        _fail("error: no models to benchmark (pass --models or populate the deployable set)")

    if dry_run:
        # selection preview only — everything below needs the engine/DB
        console.print("[green]# dry-run[/green]")
        for model_id in model_ids:
            console.print(f"  • {model_id}", markup=False)
        console.print(
            f"  batteries: {', '.join(battery_code(x) for x in batteries)}",
            markup=False,
        )
        return

    engine = LivebenchEngine()
    try:
        engine.require_thresholds(batteries)
    except Exception as exc:  # config guard: name the missing battery
        _fail(f"error: {exc}")

    try:
        conn = _runtime_connect()
    except Exception as exc:
        _fail(f"error: {exc}")

    sweep_id = None
    try:
        engine.ensure_registered(conn)
        sweep_id = make_sweep_id()
        manifest = engine.manifest(model_ids, batteries)
        manifest_stored = False
        header = (
            f"# livebench run sweep={sweep_id}\n"
            f"models={len(model_ids)} batteries={len(batteries)}\n"
            "| model | battery | score | passed | items | latency_ms | tokens |"
        )
        console.print(header)
        n_measurements = 0
        n_failed = 0
        n_skipped = 0
        for model_id in model_ids:
            for b in batteries:
                outcome = engine.run_battery(model_id, b)
                engine.store(conn, sweep_id, model_id, b, outcome)
                if not outcome.items:
                    err = (getattr(outcome, "raw_output", "") or "").strip()
                    if err:
                        head = "\n".join(err.splitlines()[:8])
                        console.print(
                            f"[yellow]no measurements for {model_id}/"
                            f"{battery_code(b)} - recorded error:[/yellow]\n{head}"
                        )
                if not manifest_stored:
                    engine.store_manifest(conn, sweep_id, manifest)
                    manifest_stored = True
                console.print(bench_row_markdown(model_id, battery_code(b), outcome))
                # #13: the summary must not overstate what store() persisted —
                # non-scored outcomes keep a run row but zero measurement rows.
                n_measurements, n_failed, n_skipped = _tally(
                    (n_measurements, n_failed, n_skipped), outcome
                )
        console.print(
            f"[green]wrote {n_measurements} measurements to sweep {sweep_id}"
            f" ({n_failed} below-gate runs, {n_skipped} non-scored)[/green]"
        )
    except Exception as exc:
        _fail(f"error: {exc}")
    finally:
        conn.close()
