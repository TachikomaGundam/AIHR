from __future__ import annotations

from .decision import measurement_count, seat_assignments
from .health import HealthReport, summary_table, sweep_health
from .seats.health_gates import SEAT_HEALTH_GATE, evaluate_gate

_SWEEPS_SQL = """
    SELECT s.sweep_id,
           s.created_at,
           COUNT(DISTINCT r.run_id)::int                 AS runs,
           COUNT(DISTINCT r.model_id)::int               AS models,
           COUNT(m.measurement_id)::int                  AS measurements,
           COUNT(m.response_text)::int                  AS with_text
      FROM hr.sweep s
      JOIN hr.run r        ON r.sweep_id = s.sweep_id
      LEFT JOIN hr.measurement m ON m.run_id = r.run_id
     GROUP BY s.sweep_id, s.created_at
     ORDER BY COUNT(m.measurement_id) DESC, s.created_at DESC
"""

def _fetch(conn, sql: str, params: tuple | None = None) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return list(cur.fetchall())


# ---------------------------------------------------------------------------
# report builders
# ---------------------------------------------------------------------------

def build_sweeps_report(conn) -> str:
    rows = _fetch(conn, _SWEEPS_SQL)
    if not rows:
        return "no sweeps found in hr.sweep"
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


def _verdict_seats(pool, means, reports, seat_db, caps_db, codes,
                   retired_set, include_retired, separations=None) -> list[list[str]]:
    """Recommended assignment rows: display rendering of ``seat_assignments``.

    ``pool`` is the deployable model set; retired models are excluded unless
    ``include_retired`` (then their ids get a ⚠ tag).
    """
    rows: list[list[str]] = []
    for a in seat_assignments(pool, means, reports, seat_db, caps_db, codes,
                              retired_set, include_retired, separations):
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


