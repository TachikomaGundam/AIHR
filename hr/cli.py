"""HR Agent (人事) — unified Typer CLI (single entry for the whole monorepo).

Merges the two legacy CLI frameworks that used to ship alongside each other:

* hr2 argparse CLI (build_parser) — the report commands (``sweeps``,
  ``health``, ``verdict``) are ported verbatim: same underlying functions,
  same defaults, same ``_KNOB_TO_BATTERY`` mapping, identical report text.
* v1 typer CLI (local vault harness/hr-archive-v1/v1-cli.py, out of tree) — ``discover`` is rewritten as static
  FastDraw-style enumeration of opencode.jsonc provider blocks into
  ``hr2.provider``/``hr2.model`` (the legacy v1 model-table write path is
  gone); ``seed`` is ported (v1 legacy path). The v1
  ``evaluate``/``report``/``run_all`` commands are RETIRED
  (see the ``--help`` epilog; ``verdict`` supersedes them).

The 13-command inventory is final — no more, no less:
``discover, seed, bench, verdict, health, sweeps, calibrate, reference,
research, publish, recommend, status, apply``. The v1 subsystem engines
(reference/research/publish/recommend) are wired to their real engines on
top of the unified config layer; ``apply`` bridges the verdict seating into
FastDraw presets/state over the plain JSON file contract (see hr/apply.py).

Report commands are pure DB reads: no model/API calls (zero new cost is the
whole point — every metric is mined from already-run measurements).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import NoReturn, Optional

import numpy as np
import typer
from rich.console import Console
from rich.table import Table

from .assign.ranker import CandidateModel, RankerResult, rank
from .config import load_yaml
from .db import connect
from .deployable import load_deployable
from .health import HealthReport, summary_table, sweep_health
from .models import BenchmarkCategory
from .seats.health_gates import SEAT_HEALTH_GATE, GATES, evaluate_gate
from .seats.rolespec import DEFAULT_BATTERY_BY_SEAT, SEAT_CODES

logger = logging.getLogger(__name__)

# Documented simplification for the verdict's recommended-assignment table:
# rolespec seat weights use capability knobs ({top_tool_fraction, longctx,
# reasoning, speed_cost, coverage}) while the DB stores per-battery scores
# (batteries discovered at runtime, incl. the livebench_* ones registered by
# `hr bench`). We map the knobs onto the batteries; *longctx* and *speed_cost*
# map onto the livebench long-context / speed batteries (restored; the same
# mapping is re-declared in configs/thresholds.yaml `knob_battery:` and the
# config wins whenever present). The ranking input is
# fitness = weighted battery means (a single-element score array per battery).
# A knob whose battery has no data in the sweep contributes 0 (warned once).
_KNOB_TO_BATTERY: dict[str, str | None] = {
    "reasoning": "reasoning",
    "top_tool_fraction": "tool_a",
    "coverage": "hallucination",
    "longctx": "livebench_long_context",
    "speed_cost": "livebench_speed",
}


def _apply_knob_battery_overrides() -> None:
    """Data-driven knob→battery overrides from configs/thresholds.yaml.

    The ``knob_battery:`` section re-declares the knob→battery mapping in
    config, so the two livebench-backed knobs can be repointed without code
    edits (config wins whenever present). Unknown knobs and non-string values
    are ignored; a missing file or section leaves the code defaults standing
    (the restored livebench mapping above) — never a crash.
    """
    try:
        overrides = load_yaml("thresholds.yaml").get("knob_battery", {})
    except FileNotFoundError:
        return
    for knob, battery_code in overrides.items():
        if knob in _KNOB_TO_BATTERY and isinstance(battery_code, str):
            _KNOB_TO_BATTERY[knob] = battery_code


_apply_knob_battery_overrides()

# (knob, battery_code) pairs already warned about missing sweep data — the
# per-seat fit runs once per seat per verdict, so warn once globally, not
# 18× per report.
_WARNED_MISSING_BATTERY: set[tuple[str, str]] = set()

_SWEEPS_SQL = """
    SELECT s.sweep_id,
           s.created_at,
           COUNT(DISTINCT r.run_id)::int                 AS runs,
           COUNT(DISTINCT r.model_id)::int               AS models,
           COUNT(m.measurement_id)::int                  AS measurements,
           COUNT(m.response_text)::int                   AS with_text
      FROM hr2.sweep s
      JOIN hr2.run r        ON r.sweep_id = s.sweep_id
      LEFT JOIN hr2.measurement m ON m.run_id = r.run_id
     GROUP BY s.sweep_id, s.created_at
     ORDER BY COUNT(m.measurement_id) DESC, s.created_at DESC
"""


# ---------------------------------------------------------------------------
# sql helpers
# ---------------------------------------------------------------------------

def _fetch(conn, sql: str, params: tuple | None = None) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return list(cur.fetchall())


def latest_sweep_id(conn) -> str:
    """Sweep with the most measurements; newest wins ties."""
    rows = _fetch(
        conn,
        """
        SELECT s.sweep_id
          FROM hr2.sweep s
          JOIN hr2.run r ON r.sweep_id = s.sweep_id
          LEFT JOIN hr2.measurement m ON m.run_id = r.run_id
         GROUP BY s.sweep_id, s.created_at
         ORDER BY COUNT(m.measurement_id) DESC, s.created_at DESC
         LIMIT 1
        """,
    )
    if not rows:
        raise ValueError("no sweeps found in hr2.sweep")
    return rows[0][0]


def measurement_count(conn, sweep_id: str) -> int:
    rows = _fetch(
        conn,
        """
        SELECT COUNT(m.measurement_id)::int
          FROM hr2.measurement m
          JOIN hr2.run r ON r.run_id = m.run_id
         WHERE r.sweep_id = %s
        """,
        (sweep_id,),
    )
    return rows[0][0] if rows else 0


def capability_means(conn, sweep_id: str) -> dict[str, dict[str, float]]:
    """Per-model per-battery mean score: {model_id: {battery_code: mean}}."""
    rows = _fetch(
        conn,
        """
        SELECT r.model_id, b.battery_code, AVG(m.score)::float8
          FROM hr2.measurement m
          JOIN hr2.run     r ON r.run_id = m.run_id
          JOIN hr2.battery b ON b.battery_id = r.battery_id
         WHERE r.sweep_id = %s
         GROUP BY r.model_id, b.battery_code
        """,
        (sweep_id,),
    )
    out: dict[str, dict[str, float]] = {}
    for model_id, battery_code, mean in rows:
        out.setdefault(model_id, {})[battery_code] = float(mean)
    return out


def battery_codes(conn) -> list[str]:
    rows = _fetch(conn, "SELECT battery_code FROM hr2.battery ORDER BY battery_code")
    return [r[0] for r in rows]


def seat_rows(conn) -> dict[str, dict]:
    rows = _fetch(
        conn,
        """
        SELECT seat_code, required_capabilities, ctx_p95_tokens
          FROM hr2.seat
        """,
    )
    out: dict[str, dict] = {}
    for seat_code, caps, ctx in rows:
        out[seat_code] = {
            "seat_code": seat_code,
            "required_capabilities": list(caps or []),
            "ctx_p95": int(ctx) if ctx is not None else None,
        }
    return out


def model_capabilities(conn) -> dict[str, dict]:
    rows = _fetch(conn, "SELECT model_id, capabilities FROM hr2.model")
    return {model_id: dict(caps or {}) for model_id, caps in rows}


# ---------------------------------------------------------------------------
# report builders
# ---------------------------------------------------------------------------

def build_sweeps_report(conn) -> str:
    rows = _fetch(conn, _SWEEPS_SQL)
    if not rows:
        return "no sweeps found in hr2.sweep"
    largest = max(rows, key=lambda r: r[4])
    latest = max(rows, key=lambda r: r[1])
    lines = [
        "sweep_id | runs | models | measurements | response_text rows",
        "---|---|---|---|---|",
    ]
    for r in rows:
        marks = []
        if r[4] == largest[4]:
            marks.append("largest")
        if r[1] == latest[1]:
            marks.append("latest")
        tag = f" ({', '.join(marks)})" if marks else ""
        lines.append(
            f"{r[0]}{tag} | {r[2]} | {r[3]} | {r[4]} | {r[5]}"
        )
    return "\n".join(lines)


def build_health_report(
    conn, sweep_id: str, cap: int | None = None, *, deployable: set[str] | None = None
) -> str:
    reports = sweep_health(conn, sweep_id, cap=cap)
    if not reports:
        return f"no measurements for sweep {sweep_id}"
    header = (
        f"# Health report — sweep {sweep_id}\n"
        f"# n measurements: {measurement_count(conn, sweep_id)} "
        "(zero new API calls — mined from existing measurements)"
    )
    table = summary_table(reports)
    if deployable is not None:
        retired = sorted(set(reports) - set(deployable))
        if retired:
            table += "\n\n⚠ retired models in this sweep: " + ", ".join(retired)
    return f"{header}\n\n{table}"


def _fit_weights(seat_code: str, available_batteries: set[str]) -> dict[str, float]:
    """Map rolespec knob weights onto DB battery codes present at runtime."""
    knobs = DEFAULT_BATTERY_BY_SEAT[seat_code]
    out: dict[str, float] = {}
    for knob, battery_code in _KNOB_TO_BATTERY.items():
        if battery_code is None:
            continue
        if battery_code not in available_batteries:
            if (knob, battery_code) not in _WARNED_MISSING_BATTERY:
                _WARNED_MISSING_BATTERY.add((knob, battery_code))
                logger.warning(
                    "verdict knob %r maps to battery %r but that battery has "
                    "no data in this sweep; knob contributes 0 to fitness",
                    knob, battery_code,
                )
            continue
        out[battery_code] = out.get(battery_code, 0.0) + knobs.get(knob, 0.0)
    total = sum(out.values())
    if total <= 0:
        return {}
    return {bc: w / total for bc, w in out.items()}


def seat_assignments(pool, means, reports, seat_db, caps_db, codes,
                     retired_set, include_retired) -> list[dict[str, object]]:
    """Structured recommended assignment per seat (rank each seat once).

    One entry per seat in SEAT_CODES order, with keys: seat_code, gate_level,
    primary (model id str or None), fallbacks ([(model, label), ...]),
    eliminated ([(model, reason), ...]), unassigned (None when a primary was
    picked, else the reason string). ``pool`` is the deployable model set;
    retired models are excluded unless ``include_retired``.

    This is the single source of the verdict seating: the display rows
    (``_verdict_seats``) and the FastDraw bridge (``hr apply``) both read it.
    """
    available = set(codes)
    out: list[dict[str, object]] = []
    for seat_code in SEAT_CODES:
        weights = _fit_weights(seat_code, available)
        candidates: list[CandidateModel] = []
        for model_id in sorted(means):
            if model_id not in pool:
                continue
            battery_means = means[model_id]
            if not battery_means:
                continue
            scores = {bc: np.array([v]) for bc, v in battery_means.items() if bc in available}
            if not scores:
                continue
            candidates.append(
                CandidateModel(
                    model_id=model_id,
                    provider_id="",
                    capabilities=caps_db.get(model_id, {}),
                    ctx_p95_tokens=0,
                    scores=scores,
                    cost_per_task=0.0,
                    health=reports.get(model_id),
                )
            )
        gate_level = SEAT_HEALTH_GATE[seat_code]
        seats_found = seat_db.get(seat_code)
        seat = seats_found or {
            "seat_code": seat_code,
            "required_capabilities": [],
            "ctx_p95": None,
        }
        primary = None
        fallbacks: list[tuple[str, str]] = []
        eliminated: list[tuple[str, str]] = []
        unassigned: str | None = None
        if not candidates:
            unassigned = "no candidates with battery data"
        else:
            try:
                result: RankerResult = rank(
                    candidates, seat, weights,
                    separation_pairs=None, gate_level=gate_level,
                )
            except ValueError as exc:
                unassigned = f"none pass gates ({exc})"
            else:
                primary = result.primary
                fallbacks = list(result.fallbacks[:2])
                eliminated = list(result.eliminated)
        out.append(
            {
                "seat_code": seat_code,
                "gate_level": gate_level,
                "primary": primary,
                "fallbacks": fallbacks,
                "eliminated": eliminated,
                "unassigned": unassigned,
            }
        )
    return out


def _verdict_seats(pool, means, reports, seat_db, caps_db, codes,
                   retired_set, include_retired) -> list[list[str]]:
    """Recommended assignment rows: display rendering of ``seat_assignments``.

    ``pool`` is the deployable model set; retired models are excluded unless
    ``include_retired`` (then their ids get a ⚠ tag).
    """
    rows: list[list[str]] = []
    for a in seat_assignments(pool, means, reports, seat_db, caps_db, codes,
                              retired_set, include_retired):
        seat_code = a["seat_code"]
        gate_level = a["gate_level"]
        if a["primary"] is None:
            rows.append([seat_code, gate_level, a["unassigned"] or "no candidates with battery data", "—", "—"])
            continue
        eliminated = a["eliminated"]
        gate_note = "; ".join(
            f"{_tag(mid, retired_set, include_retired)}:{reason}"
            for mid, reason in eliminated
        ) if eliminated else ""
        fallbacks = " / ".join(
            f"{_tag(mid, retired_set, include_retired)} ({label})"
            for mid, label in a["fallbacks"]
        )
        rows.append(
            [seat_code, gate_level, _tag(a["primary"], retired_set, include_retired),
             fallbacks, gate_note]
        )
    return rows


_GATE_LABELS = ("strict", "moderate", "lenient")


def _verdict_gates(reports: dict[str, HealthReport]) -> list[list[str]]:
    """Per-model gate status: PASS/FAIL(+reasons) per level, across models.

    Seat-aware like the ranker: each model is evaluated under EVERY seat of
    the level with ``evaluate_gate(..., seat_code=...)``, so TIER-1 per-role
    hard vetoes (ROLE_HARD_VETOS) surface in the displayed status — a model
    the ranker would hard-veto for any seat at the level shows FAIL with the
    veto reason instead of a misleading PASS. A model passes the level row
    only when it passes under every seat of that level.
    """
    by_level: dict[str, list[str]] = {}
    for seat_code, level in SEAT_HEALTH_GATE.items():
        by_level.setdefault(level, []).append(seat_code)
    rows: list[list[str]] = []
    for level in _GATE_LABELS:
        cells: list[str] = []
        for model_id in sorted(reports):
            report = reports[model_id]
            if not report.n_measurements:
                cells.append(f"{model_id}: no data")
                continue
            violations: dict[str, list[str]] = {}
            for seat_code in by_level.get(level, []):
                passed, notes = evaluate_gate(report, level, seat_code)
                if not passed:
                    violation = next(
                        (n for n in notes if "not measured" not in n), "gate failed"
                    )
                    violations.setdefault(violation, []).append(seat_code)
            if violations:
                parts = [
                    f"{reason} [{'/'.join(sorted(seats))}]"
                    for reason, seats in violations.items()
                ]
                cells.append(f"{model_id}: FAIL ({'; '.join(parts)})")
            else:
                cells.append(f"{model_id}: PASS")
        rows.append([f"{level} ({len(cells)} models)", "<br>".join(cells)])
    return rows


def _tag_retired_rows(table: str, retired: set[str], suffix: str = " ⚠ retired") -> str:
    out = []
    for line in table.splitlines():
        for mid in retired:
            marker = f"| {mid} |"
            if line.startswith(marker):
                line = marker + suffix + line[len(marker):]
                break
        out.append(line)
    return "\n".join(out)


def _tag(mid: str, retired_set: set[str], include_retired: bool) -> str:
    if include_retired and mid in retired_set:
        return mid + " ⚠"
    return mid


def build_verdict_report(
    conn, sweep_id: str, *, include_retired: bool = False,
    deployable: set[str] | None = None,
) -> str:
    dep = deployable if deployable is not None else load_deployable()
    means = capability_means(conn, sweep_id)
    reports = sweep_health(conn, sweep_id)
    codes = battery_codes(conn)
    n_meas = measurement_count(conn, sweep_id)
    seat_db = seat_rows(conn)
    caps_db = model_capabilities(conn)

    model_ids = sorted(means)
    retired = sorted(m for m in model_ids if m not in dep)
    retired_set = set(retired)
    pool = set(model_ids) if include_retired else set(model_ids) - retired_set

    header = (
        f"# Verdict — sweep {sweep_id}\n"
        f"n measurements: {n_meas} · "
        "zero new API calls (mined from existing measurements) · "
        f"deployable pool: {len(pool)}/{len(model_ids)} models "
        "(iron rule 5: retired models never assigned)"
    )

    # (a) capability battery averages
    cap_lines = ["| model | " + " | ".join(codes) + " |"]
    cap_lines.append("|---" * (len(codes) + 1) + "|")
    for model_id in model_ids:
        shown = model_id + (" ⚠ retired" if model_id in retired_set else "")
        cap_lines.append(
            "| " + shown + " | "
            + " | ".join(
                f"{means[model_id].get(bc, 0.0):.3f}" if bc in means[model_id] else "—"
                for bc in codes
            )
            + " |"
        )
    cap_table = "\n".join(cap_lines)

    # (b) health
    health_table = (
        _tag_retired_rows(summary_table(reports), retired_set)
        if reports else "_no health data_"
    )

    # (c) gates
    gate_table = "\n".join(
        f"| {level} | {cells} |" for level, cells in _verdict_gates(reports)
    ) or "_no models_"

    # (c2) retired section
    retired_table = ""
    if retired:
        retired_table = (
            "| model | sweep measurements | status |\n|---|---|---|\n"
            + "\n".join(
                f"| {mid} | {reports[mid].n_measurements if mid in reports else '—'} "
                f"| retired from opencode.jsonc; excluded from assignment |"
                for mid in retired
            )
        )

    # (d) seats
    seat_note = (
        "Recommended assignments use rank() with a documented simplified "
        "fitness: rolespec knob weights mapped onto runtime DB battery codes "
        "({reasoning→reasoning, top_tool_fraction→tool_a, "
        "coverage→hallucination, longctx→livebench_long_context, "
        "speed_cost→livebench_speed} — repointable via the `knob_battery:` "
        "section of configs/thresholds.yaml; a knob whose battery has no "
        "data contributes 0 with a logged warning); ranking input = weighted "
        "battery means. Separation data not "
        "available → top-by-fitness. Models failing a seat's health gate are "
        "excluded (gate_level per seat). "
        "Candidates limited to the deployable set (iron rule 5); "
        f"{'retired models included and tagged ⚠' if include_retired else 'retired models are never assigned'}."
    )
    seat_rows_out = _verdict_seats(
        pool, means, reports, seat_db, caps_db, codes, retired_set, include_retired
    )
    seat_lines = [
        "| seat | gate level | primary | fallback 1/2 | eliminated |",
        "|---|---|---|---|---|",
    ]
    for r in seat_rows_out:
        seat_lines.append("| " + " | ".join(str(c) for c in r) + " |")
    seat_table = "\n".join(seat_lines)

    sections = [
        header,
        f"## Capability battery averages (per model)\n{cap_table}",
        f"## Health (full pool)\n{health_table}",
        f"## Health gate status per level\n{gate_table}",
    ]
    if retired_table:
        sections.append(f"## Retired models (excluded from assignment)\n{retired_table}")
    sections.append(f"## Recommended seat assignment\n{seat_note}\n{seat_table}")
    return "\n\n".join(sections)


def build_status_report(conn) -> str:
    """DB-only status (hr2 spine style): sweeps + latest-sweep capability means.

    Zero API calls — everything is mined from already-run measurements,
    mirroring ``sweeps``/``verdict``. Retired models (not in the deployable
    pool) are tagged ⚠ like ``build_verdict_report`` does.
    """
    sweep_id = latest_sweep_id(conn)
    means = capability_means(conn, sweep_id)
    codes = battery_codes(conn)
    deployable = set(load_deployable())
    model_ids = sorted(means)
    retired = sorted(m for m in model_ids if m not in deployable)

    cap_lines = ["| model | " + " | ".join(codes) + " |"]
    cap_lines.append("|---" * (len(codes) + 1) + "|")
    for model_id in model_ids:
        shown = model_id + (" ⚠ retired" if model_id in retired else "")
        cap_lines.append(
            "| " + shown + " | "
            + " | ".join(
                f"{means[model_id].get(bc, 0.0):.3f}" if bc in means[model_id] else "—"
                for bc in codes
            )
            + " |"
        )
    cap_table = "\n".join(cap_lines)

    return (
        f"# Status — latest sweep {sweep_id}\n"
        f"deployable pool: {len(set(model_ids) & deployable)}/{len(model_ids)} "
        "models · zero new API calls (mined from existing measurements)\n\n"
        f"{build_sweeps_report(conn)}\n\n"
        f"## Capability battery averages (per model)\n{cap_table}"
    )


# ---------------------------------------------------------------------------
# CLI plumbing (typer)
# ---------------------------------------------------------------------------

class _PinnedNameTyper(typer.Typer):
    """Typer that fixes the program name shown by click-derived help.

    The ``hr`` and ``hr2`` console scripts both point at this SAME app object
    (``hr.cli:app``; hr2 is a compat alias, not a copy). click derives the
    usage-line program name from argv[0], which would print ``hr2`` for the
    alias — this pins it to the canonical name (``hr``) so the two help
    outputs are byte-identical (task-5 acceptance).
    """

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


def _ensure_schema() -> None:
    """Create HR tables if they don't exist yet (v1 legacy path)."""
    from hr.database import init_schema

    init_schema()


def _fail(message: str) -> NoReturn:
    """Print an error message to stderr and exit non-zero.

    Plain ``print(..., file=sys.stderr)`` — exact parity with the old hr2
    ``main()`` error surface (``error: {exc}`` on stderr, exit 1).
    """
    print(message, file=sys.stderr)
    raise typer.Exit(code=1)


def _with_conn(builder) -> None:
    """Connect, print the report produced by builder(conn); clean error surface.

    Mirrors the old hr2 main(): connect -> print -> close; any exception is
    reported as ``error: {exc}`` on stderr with exit 1.
    """
    try:
        conn = connect()
        try:
            console.print(builder(conn))
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — CLI boundary: report the error
        _fail(f"error: {exc}")


def _resolve_sweep_id(conn, sweep_id: Optional[str], latest: bool) -> str:
    """Old argparse default semantics: None or --latest -> most-measured sweep."""
    if not sweep_id or latest:
        return latest_sweep_id(conn)
    return sweep_id


# ---------------------------------------------------------------------------
# commands (inventory order: discover, seed, bench, verdict, health, sweeps,
# calibrate, reference, research, publish, recommend, status, apply)
# ---------------------------------------------------------------------------

@app.command()
def discover(
    all_models: bool = typer.Option(
        False,
        "--all",
        help="list every configured provider/model, including out-of-scope "
             "ones (marked as such)",
    ),
) -> None:
    """Enumerate providers and models from opencode.jsonc into hr2.

    FastDraw-style config derivation: parses the project + global
    opencode.jsonc provider blocks (JSONC-tolerant) and annotates each model
    with scope (every discovered provider minus the OPTIONAL
    ``scope_excludes:`` list in configs/fleet.yaml — new providers
    auto-inherit the default scope) and auth presence (auth-v2.json,
    falling back to auth.json). Upserts into hr2.provider/hr2.model —
    idempotent (ON CONFLICT DO NOTHING); the legacy v1 model table is never
    written from this command.

    Limitation: npm-spec / remote-registry providers that live outside the
    config files (e.g. kimi-for-coding / deepseek, auth keys only) are NOT
    enumerated — hr runs outside opencode, so there is no live
    api.state.provider runtime state to read (static config parse only).
    Stage fleets still reach those models via configs/deployable.yaml
    ``extra_deployable:``.
    """
    from .discover import enumerate_models, scope_providers, upsert_hr2

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
        conn = connect()
        try:
            providers, rows = upsert_hr2(conn, models)
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
        f"upserted {providers} provider(s), {rows} model row(s) into hr2[/green]",
    )


@app.command()
def seed() -> None:
    """Seed the database with research findings and reference scores (v1 legacy path)."""
    try:
        from hr.research import seed_research
        from hr.reference import init_reference_table, seed_reference
    except ImportError as exc:
        _fail(f"seed module not yet built: {exc}")
    _ensure_schema()
    seed_research()
    init_reference_table()
    seed_reference()
    console.print("[green]Research seeded[/green]")
    console.print("[green]Reference scores seeded[/green]")


def _selection_indices(spec: str, n: int) -> list[int]:
    """Parse a comma/range selection (``"1,3,5-7"``) into 1..n indices.

    Comma-separated tokens are single indices or inclusive ``N-M`` ranges;
    duplicate indices collapse to their first occurrence (order preserved).
    Tolerates stray commas (empty tokens skipped). Raises ValueError naming
    the offending token on non-numeric input, a descending range, or an
    index outside 1..n — callers turn that into an error message + re-prompt
    (never a crash).
    """
    indices: list[int] = []
    seen: set[int] = set()
    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            continue
        if "-" in token:
            lo_s, hi_s = token.split("-", 1)
            if not (lo_s.strip().isdigit() and hi_s.strip().isdigit()):
                raise ValueError(f'invalid selection "{token}": expected N or N-M')
            lo, hi = int(lo_s), int(hi_s)
            if lo > hi:
                raise ValueError(f'invalid selection "{token}": range is descending')
            values = range(lo, hi + 1)
        else:
            if not token.isdigit():
                raise ValueError(f'invalid selection "{token}": not a number')
            values = (int(token),)
        for idx in values:
            if idx < 1 or idx > n:
                raise ValueError(
                    f'invalid selection "{idx}": index out of range 1..{n}'
                )
            if idx not in seen:
                seen.add(idx)
                indices.append(idx)
    if not indices:
        raise ValueError("no models selected (empty selection)")
    return indices


def _pick_models_interactive(discovered: list) -> list[str]:
    """Numbered menu over the discover list; comma/range selection from stdin.

    Returns full model ids (``provider/model_id``) in user-selected order.
    Invalid input prints an error to stderr and re-prompts; EOF aborts with
    an error (never an infinite loop). Pure CLI: sys.stdin only, no TUI or
    opencode runtime.
    """
    n = len(discovered)
    console.print(
        "# models available for benchmarking "
        "(discovered from opencode.jsonc configs):",
        markup=False,
    )
    for i, model in enumerate(discovered, start=1):
        # markup off: ids/names are config data — square brackets would be
        # eaten by rich markup, silently dropping the entry
        console.print(
            f"  {i:>2}. {model.provider}/{model.model_id}  ({model.display_name})",
            markup=False,
        )
    while True:
        console.print(
            "Select models to benchmark (comma/range, e.g. 1,3,5-7): ",
            markup=False,
        )
        line = sys.stdin.readline()
        if line == "":
            _fail("error: no selection provided (EOF)")
        try:
            indices = _selection_indices(line, n)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            continue
        return [f"{discovered[i - 1].provider}/{discovered[i - 1].model_id}"
                for i in indices]


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
    """Run the 10 live capability benchmarks and record hr2.measurement rows.

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
    from hr.deployable import load_deployable
    from hr.models import BenchmarkCategory

    batteries: list[BenchmarkCategory] = (
        [battery] if battery is not None else list(LIVEBENCH_BATTERIES)
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
            else sorted(load_deployable())
        )
    if not model_ids:
        _fail("error: no models to benchmark (pass --models or populate the deployable set)")

    if dry_run:
        # selection preview only — everything below needs the engine/DB
        console.print("[green]# dry-run[/green]")
        for model_id in model_ids:
            console.print(f"  • {model_id}", markup=False)
        return

    engine = LivebenchEngine()
    try:
        engine.require_thresholds(batteries)
    except Exception as exc:  # config guard: name the missing battery
        _fail(f"error: {exc}")

    try:
        conn = connect()
    except Exception as exc:
        _fail(f"error: {exc}")

    sweep_id = None
    try:
        engine.ensure_registered(conn)
        sweep_id = make_sweep_id()
        header = (
            f"# livebench run sweep={sweep_id}\n"
            f"models={len(model_ids)} batteries={len(batteries)}\n"
            "| model | battery | score | passed | items | latency_ms | tokens |"
        )
        console.print(header)
        n_measurements = 0
        n_failed = 0
        for model_id in model_ids:
            for b in batteries:
                outcome = engine.run_battery(model_id, b)
                engine.store(conn, sweep_id, model_id, b, outcome)
                ok = len(outcome.items) and all(i.passed for i in outcome.items)
                items_txt = f"{sum(1 for i in outcome.items if i.passed)}/{len(outcome.items)}"
                console.print(
                    f"| {model_id} | {battery_code(b)} | {outcome.score:.1f} | "
                    f"{'PASS' if ok else 'FAIL'} | {items_txt} | "
                    f"{outcome.latency_ms} | {outcome.tokens_in + outcome.tokens_out} |"
                )
                n_measurements += len(outcome.items)
                n_failed += 0 if ok else 1
        console.print(
            f"[green]wrote {n_measurements} measurements to sweep {sweep_id}"
            f" ({n_failed} failed runs)[/green]"
        )
    except Exception as exc:
        _fail(f"error: {exc}")
    finally:
        conn.close()


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
            deployable=load_deployable(),
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
            deployable=load_deployable(),
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


@app.command()
def reference(
    seed: bool = typer.Option(
        False, "--seed", help="seed the hr_reference table from the curated store (DB)",
    ),
    model: Optional[str] = typer.Argument(
        None, help="model_id to show per-category reference scores for",
    ),
) -> None:
    """Reference knowledge: curated published-benchmark scores per model.

    The curated scores are the SINGLE knowledge store (hr/reference.py);
    without arguments this prints the store summary offline. ``--seed``
    upserts them into ``hr_reference`` (needs the DB). 
    """
    from hr.reference import (
        init_reference_table,
        load_reference_scores,
        seed_reference,
    )

    if seed:
        try:
            _ensure_schema()
            init_reference_table()
            counts = seed_reference()
        except Exception as exc:
            _fail(f"error: {exc}")
        console.print(f"[green]seeded reference scores: {counts}[/green]")
        return
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
    seed: bool = typer.Option(
        False, "--seed", help="seed the hr_research table from the curated findings (DB)",
    ),
    model: Optional[str] = typer.Argument(
        None, help="model_id to show curated research findings for",
    ),
) -> None:
    """Research knowledge: qualitative findings per model (offline store summary).

    Benchmark numbers live in the reference store (configs/knowledge.yaml
    ``reference_scores``) — this command carries the qualitative layer it
    does not repeat. ``--seed`` writes findings into ``hr_research`` (needs
    the DB).
    """
    from hr.research import load_findings, seed_research

    if seed:
        try:
            _ensure_schema()
            counts = seed_research()
        except Exception as exc:
            _fail(f"error: {exc}")
        console.print(f"[green]seeded research findings: {counts}[/green]")
        return
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
) -> None:
    """Seat recommendations from configs/seats.yaml + recent measurements.

    Derives the seat list from configs/seats.yaml ONLY (no code tables) and
    ranks models per seat under the blended capability prior (see
    docs/en/capability-prior.md). Read-only: verdict owns assignments.
    """
    from hr.recommend import RecommendationEngine, load_seat_specs

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
            results = engine.recommend_for_task(task)
            top = list(results)[:5]
            table = Table(title=f"Top models for: {task}", show_lines=False)
            table.add_column("Rank", no_wrap=True, justify="right")
            table.add_column("Model")
            table.add_column("Score", justify="right", no_wrap=True)
            for rank, (model, score) in enumerate(top, start=1):
                table.add_row(str(rank), model, f"{float(score):.1f}")
            if top:
                console.print(table)
            else:
                console.print("[yellow](no recommendations returned)[/yellow]")
        else:
            console.print(engine.seat_recommendations(seats))
    except Exception as exc:
        _fail(f"error: {exc}")
    finally:
        engine._conn.close()


@app.command()
def status() -> None:
    """Show DB status: sweeps + latest-sweep capability means (DB-only)."""
    _with_conn(build_status_report)


@app.command(epilog=(
    "RESTART NOTE: --set-state effects require an opencode restart — FastDraw's "
    "config hook applies its state file (.fastdraw.json) only at opencode boot. "
    "Presets take effect at tool-call time (fastdraw_load_preset), no restart. "
    "FastDraw code is never imported or run — this is a plain JSON file contract "
    "on the opencode config dir (OPENCODE_CONFIG_DIR env or ~/.config/opencode)."
))
def apply(
    preset: Optional[str] = typer.Option(
        None, "--preset",
        help="preset name in fastdraw-presets.json (default: verdict-<today>)",
    ),
    set_state: bool = typer.Option(
        False, "--set-state",
        help="also write .fastdraw.json (boot-time state; opencode restart required)",
    ),
) -> None:
    """Bridge the latest verdict seating into a FastDraw preset (file contract only).

    Reads the same per-seat ranking ``hr verdict --latest`` computes for the
    latest sweep (the seating is computed, never stored — there is no
    results table) and writes fastdraw-presets.json under the opencode
    config dir: {presets: {NAME: {description, createdAt, agents: {agent:
    provider/model}}}}. Seat keys are normalized to runtime agent names
    (underscores → hyphens). Every agents value must contain "/" (FastDraw's
    isModelMap rule). Refuses to write when no verdict seating exists —
    presets are never clobbered with empty data.
    """
    from hr.apply import apply as run_apply

    _with_conn(
        lambda conn: run_apply(conn, preset_name=preset, set_state=set_state)
    )