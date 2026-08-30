"""Behavioral-health analyzer (loop score and related signals).

Computes per-model metrics from measurement rows for a sweep:
  - loop_score (0..1, text-based)
  - truncation_rate       (token-cap proxy)
  - token_efficiency      (tokens_out per unit score)
  - self_consistency      (rep-score range + unanimity %)
  - answer_completion_rate

``report()`` turns the per-model bundle into a markdown table.

Spec ambiguities resolved:
  * truncation cap: judged against the ACTUAL ``requested_max_output``
    recorded on each measurement row when present (the row carries the cap
    that was really sent to the model — e.g. 8192 for the openai-compat
    default). Rows without a recorded cap fall back to the explicit ``cap``
    argument, then to the legacy proxy tokens_out >= 16000
    (≈ 0.98 × 16384 stage battery cap).
  * unanimity: only items with >= 2 repetitions carry consistency
    information; single-rep items are excluded from both the range and the
    unanimity denominator (they used to count as trivially "unanimous",
    inflating the metric to a vacuous 100% that passed the strict gate).
  * final-answer marker set: ``{"结论", "answer:", "final answer", "therefore"}``
    + a trailing bare number; these are cheap, sufficient heuristics — real
    parsing is the grader's job, not health's.
"""

from __future__ import annotations

from typing import Iterable

from hr.health_metrics import (
    HealthReport,
    _answer_completion_rate,
    _has_final_answer,
    _loop_score,
    _NEAR_CAP_PROXY,
    _self_consistency,
    _token_efficiency,
    _truncation_rate,
    _truncation_rate_rows,
)

__all__ = [
    "HealthReport",
    "_answer_completion_rate",
    "_has_final_answer",
    "_loop_score",
    "_self_consistency",
    "_token_efficiency",
    "_truncation_rate",
    "_truncation_rate_rows",
    "compute_health",
    "report",
    "summary_table",
    "sweep_health",
]


def _fetch_rows(conn, sweep_id: str, model_id: str) -> list[dict]:
    sql = """
        SELECT m.item_id, m.score, m.tokens_out, m.response_text,
               m.requested_max_output
          FROM hr.measurement m
          JOIN hr.run        r ON r.run_id = m.run_id
         WHERE r.sweep_id = %s AND r.model_id = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (sweep_id, model_id))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def compute_health(
    model_id: str, sweep_id: str, conn, *, cap: int | None = None
) -> HealthReport:
    rows = _fetch_rows(conn, sweep_id, model_id)
    hr = HealthReport(
        model_id=model_id, sweep_id=sweep_id, n_measurements=len(rows)
    )
    if not rows:
        hr.notes.append("no measurements")
        return hr

    eff_cap = cap if cap is not None else _NEAR_CAP_PROXY

    per_text_scores = [
        _loop_score(r["response_text"]) for r in rows
    ]
    valid_loop = [s for s in per_text_scores if s is not None]
    if valid_loop:
        hr.loop_mean = sum(valid_loop) / len(valid_loop)
        hr.loop_max = max(valid_loop)

    hr.truncation_rate = _truncation_rate_rows(rows, eff_cap)
    hr.token_efficiency = _token_efficiency(
        (r["tokens_out"] for r in rows), (r["score"] for r in rows)
    )
    mr, up = _self_consistency(rows)
    hr.consistency_mean_range = mr
    hr.consistency_unanimity_pct = up
    hr.answer_completion_rate = _answer_completion_rate(rows, cap=eff_cap)

    n_with_text = sum(1 for s in per_text_scores if s is not None)
    if n_with_text == 0:
        hr.notes.append("no response_text stored (pre-migration rows)")
    return hr


def _fetch_battery_breakdown(conn, sweep_id: str) -> list[dict]:
    """Per-battery aggregate (items, measurements, mean score) for a sweep."""
    sql = """
        SELECT b.battery_code,
               COUNT(DISTINCT m.item_id)::int AS n_items,
               COUNT(m.measurement_id)::int   AS n_measurements,
               AVG(m.score)::float8           AS mean_score
          FROM hr.measurement m
          JOIN hr.run        r ON r.run_id = m.run_id
          JOIN hr.battery    b ON b.battery_id = r.battery_id
         WHERE r.sweep_id = %s
         GROUP BY b.battery_code
         ORDER BY b.battery_code
    """
    # Zip against fixed aliases (not cur.description) — the fake conns in
    # CLI tests report a bare ("c",) description for this query.
    cols = ("battery_code", "n_items", "n_measurements", "mean_score")
    with conn.cursor() as cur:
        cur.execute(sql, (sweep_id,))
        out = []
        for row in cur.fetchall():
            if len(row) != len(cols):
                continue
            out.append(dict(zip(cols, row)))
        return out


def sweep_health(
    conn, sweep_id: str, *, cap: int | None = None
) -> dict[str, HealthReport]:
    """Zero-cost full-pool health: every distinct model of a sweep in one call.

    Reuses :func:`compute_health` (and through it ``_fetch_rows``) per model —
    a pure SQL read of existing measurement rows, no API calls. Reports are
    stamped with the sweep-level per-battery breakdown when measurements
    exist (e.g. a ``tool_b`` battery surfaces its own section).
    """
    sql = """
        SELECT DISTINCT r.model_id
          FROM hr.run r
         WHERE r.sweep_id = %s
         ORDER BY r.model_id
    """
    with conn.cursor() as cur:
        cur.execute(sql, (sweep_id,))
        models = [row[0] for row in cur.fetchall()]
    breakdown = _fetch_battery_breakdown(conn, sweep_id)
    reports = {
        model_id: compute_health(model_id, sweep_id, conn, cap=cap)
        for model_id in models
    }
    if breakdown:
        for hr in reports.values():
            hr.battery_breakdown = breakdown
    return reports


def summary_table(reports: dict[str, HealthReport]) -> str:
    """Markdown table of HealthReports (model | n | loop … | completion).

    Rows are sorted by model_id; unmeasured values render as "—".
    """
    hdr = "| model | n | loop_mean | loop_max | truncation_rate | tok/pt | consistency range | unanimity% | completion |"
    sep = "|---|---:|---:|---:|---:|---:|---:|---:|---:|"

    def fmt_f(x: float | None, *, pct: bool = False, digits: int = 3) -> str:
        if x is None:
            return "—"
        v = x * 100 if pct else x
        return f"{v:.{digits}f}{'%' if pct else ''}"

    lines = [hdr, sep]
    for model_id in sorted(reports):
        r = reports[model_id]
        lines.append(
            "| "
            + " | ".join(
                [
                    r.model_id,
                    str(r.n_measurements),
                    fmt_f(r.loop_mean),
                    fmt_f(r.loop_max),
                    fmt_f(r.truncation_rate, pct=True, digits=1),
                    fmt_f(r.token_efficiency, digits=1),
                    fmt_f(r.consistency_mean_range, digits=3),
                    fmt_f(r.consistency_unanimity_pct, pct=True, digits=1),
                    fmt_f(r.answer_completion_rate, pct=True, digits=1),
                ]
            )
            + " |"
        )
    breakdown = next(
        (r.battery_breakdown for r in reports.values() if r.battery_breakdown),
        None,
    )
    if breakdown:
        lines.append("")
        lines.append("## Batteries")
        lines.append("")
        lines.append("| battery | items | measurements | mean_score |")
        lines.append("|---|---:|---:|---:|")
        for row in breakdown:
            mean = row["mean_score"]
            lines.append(
                f"| {row['battery_code']} | {row['n_items']} | "
                f"{row['n_measurements']} | "
                f"{'—' if mean is None else f'{mean:.3f}'} |"
            )
    return "\n".join(lines)


def report(
    models: Iterable[str], sweep_id: str, conn, *, cap: int | None = None
) -> str:
    rows = [compute_health(m, sweep_id, conn, cap=cap) for m in models]
    hdr = "| model | n | loop_mean | loop_max | trunc% | eff (tok/pt) | consistency Δ | unanimity% | answer% |"
    sep = "|---|---:|---:|---:|---:|---:|---:|---:|---:|"

    def fmt_f(x: float | None, *, pct: bool = False, digits: int = 3) -> str:
        if x is None:
            return "—"
        v = x * 100 if pct else x
        return f"{v:.{digits}f}{'%' if pct else ''}"

    lines = [hdr, sep]
    for r in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    r.model_id,
                    str(r.n_measurements),
                    fmt_f(r.loop_mean),
                    fmt_f(r.loop_max),
                    fmt_f(r.truncation_rate, pct=True, digits=1),
                    fmt_f(r.token_efficiency, digits=1),
                    fmt_f(r.consistency_mean_range, digits=3),
                    fmt_f(r.consistency_unanimity_pct, pct=True, digits=1),
                    fmt_f(r.answer_completion_rate, pct=True, digits=1),
                ]
            )
            + " |"
        )
    return "\n".join(lines)
