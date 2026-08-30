"""hr.health — behavioral-health analyzer (loop_score + friends).

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

import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

_NEAR_CAP_PROXY = 16000
_FINAL_ANSWER_RE = re.compile(
    r"(结论|answer\s*:|final\s*answer|therefore|\b所以\b)",
    re.IGNORECASE,
)
_TRAILING_NUMBER_RE = re.compile(r"\s*(-?\d+(?:\.\d+)?)\s*[.。?!？]*\s*$")


@dataclass
class HealthReport:
    model_id: str
    sweep_id: str
    n_measurements: int
    loop_mean: float | None = None
    loop_max: float | None = None
    truncation_rate: float | None = None
    token_efficiency: float | None = None
    consistency_mean_range: float | None = None
    consistency_unanimity_pct: float | None = None
    answer_completion_rate: float | None = None
    battery_breakdown: list[dict] | None = None
    notes: list[str] = field(default_factory=list)


def _loop_score(text: str | None) -> float | None:
    """Per-response repetition metric in [0, 1]. None if text is empty."""
    if not text:
        return None
    text_stripped = text.strip()
    if not text_stripped:
        return None

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    line_dup_frac = 0.0
    if lines:
        seen: dict[str, int] = defaultdict(int)
        for ln in lines:
            seen[ln] += 1
        unique = sum(1 for v in seen.values() if v == 1)
        line_dup_frac = 1.0 - (unique / len(lines))

    span_hit = 0.0
    if len(text_stripped) >= 40:
        seen_spans: dict[str, int] = defaultdict(int)
        for i in range(len(text_stripped) - 39):
            seen_spans[text_stripped[i : i + 40]] += 1
        if any(c >= 3 for c in seen_spans.values()):
            span_hit = 1.0

    return max(line_dup_frac, span_hit)


def _row_cap(row: dict, fallback: int) -> int:
    """Per-measurement output cap: the ``requested_max_output`` recorded on
    the row (the cap actually sent with that call), else ``fallback``."""
    cap = row.get("requested_max_output")
    if isinstance(cap, int) and cap > 0:
        return cap
    return fallback


def _truncation_rate(
    tokens_outs: Iterable[int | None], cap: int = _NEAR_CAP_PROXY
) -> float | None:
    outs = [t for t in tokens_outs if isinstance(t, int) and t >= 0]
    if not outs:
        return None
    return sum(1 for t in outs if t >= cap) / len(outs)


def _truncation_rate_rows(
    rows: Iterable[dict], fallback_cap: int = _NEAR_CAP_PROXY
) -> float | None:
    """Row-level truncation rate: each measurement is compared against the
    output cap ACTUALLY requested for that call (``requested_max_output`` on
    the row), falling back to ``fallback_cap`` when the row doesn't record
    one. A response that ends exactly at its requested cap counts as
    truncated (>= semantics)."""
    n = 0
    flagged = 0
    for r in rows:
        tokens_out = r.get("tokens_out")
        if not isinstance(tokens_out, int) or tokens_out < 0:
            continue
        n += 1
        if tokens_out >= _row_cap(r, fallback_cap):
            flagged += 1
    if n == 0:
        return None
    return flagged / n


def _token_efficiency(
    tokens_outs: Iterable[int | None], scores: Iterable[float]
) -> float | None:
    to = list(tokens_outs)
    sc = list(scores)
    if len(to) != len(sc):
        raise ValueError("tokens_outs and scores must be the same length")
    total_out = sum(t for t in to if isinstance(t, int))
    total_score = sum(sc)
    if total_score <= 0:
        return None
    return total_out / total_score


def _has_final_answer(text: str | None) -> bool:
    if not text:
        return False
    t = text.strip()
    if not t:
        return False
    if _FINAL_ANSWER_RE.search(t):
        return True
    if _TRAILING_NUMBER_RE.search(t):
        return True
    return False


def _answer_completion_rate(
    rows: Iterable[dict], cap: int = _NEAR_CAP_PROXY
) -> float | None:
    counted = 0
    ok = 0
    for r in rows:
        text = r.get("response_text")
        tokens_out = r.get("tokens_out")
        if not isinstance(text, str) or not text.strip():
            continue
        counted += 1
        truncated = isinstance(tokens_out, int) and tokens_out >= _row_cap(r, cap)
        if not truncated and _has_final_answer(text):
            ok += 1
    if counted == 0:
        return None
    return ok / counted


def _self_consistency(rows: Iterable[dict]) -> tuple[float | None, float | None]:
    """Per-(item) rep-score range → (mean_range, unanimity_pct).

    Only items with >= 2 repetitions count: a single-rep item carries no
    consistency information and must not count as "unanimous" (it used to
    inflate unanimity toward a vacuous 100% that let the strict gate pass
    on items that were never actually re-run).
    """
    by_item: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        score = r.get("score")
        item = r.get("item_id")
        if item is None or not isinstance(score, (int, float, Decimal)):
            continue
        by_item[item].append(float(score))
    if not by_item:
        return None, None
    multi = [v for v in by_item.values() if len(v) >= 2]
    ranges = [max(v) - min(v) for v in multi]
    mean_range = sum(ranges) / len(ranges) if ranges else None
    unanimous = sum(1 for v in multi if max(v) - min(v) <= 0.01)
    unanimity_pct = unanimous / len(multi) if multi else None
    return mean_range, unanimity_pct


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
