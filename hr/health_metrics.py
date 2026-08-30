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
    if not text or not (text_stripped := text.strip()):
        return None

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    line_dup_frac = 0.0
    if lines:
        seen: dict[str, int] = defaultdict(int)
        for line in lines:
            seen[line] += 1
        unique = sum(1 for count in seen.values() if count == 1)
        line_dup_frac = 1.0 - (unique / len(lines))

    span_hit = 0.0
    if len(text_stripped) >= 40:
        seen_spans: dict[str, int] = defaultdict(int)
        for index in range(len(text_stripped) - 39):
            seen_spans[text_stripped[index : index + 40]] += 1
        if any(count >= 3 for count in seen_spans.values()):
            span_hit = 1.0
    return max(line_dup_frac, span_hit)


def _row_cap(row: dict, fallback: int) -> int:
    cap = row.get("requested_max_output")
    if isinstance(cap, int) and cap > 0:
        return cap
    return fallback


def _truncation_rate(
    tokens_outs: Iterable[int | None], cap: int = _NEAR_CAP_PROXY
) -> float | None:
    outs = [token for token in tokens_outs if isinstance(token, int) and token >= 0]
    if not outs:
        return None
    return sum(1 for token in outs if token >= cap) / len(outs)


def _truncation_rate_rows(
    rows: Iterable[dict], fallback_cap: int = _NEAR_CAP_PROXY
) -> float | None:
    count = 0
    flagged = 0
    for row in rows:
        tokens_out = row.get("tokens_out")
        if not isinstance(tokens_out, int) or tokens_out < 0:
            continue
        count += 1
        if tokens_out >= _row_cap(row, fallback_cap):
            flagged += 1
    if count == 0:
        return None
    return flagged / count


def _token_efficiency(
    tokens_outs: Iterable[int | None], scores: Iterable[float]
) -> float | None:
    outputs = list(tokens_outs)
    score_values = list(scores)
    if len(outputs) != len(score_values):
        raise ValueError("tokens_outs and scores must be the same length")
    total_score = sum(score_values)
    if total_score <= 0:
        return None
    return sum(token for token in outputs if isinstance(token, int)) / total_score


def _has_final_answer(text: str | None) -> bool:
    if not text or not (stripped := text.strip()):
        return False
    return bool(_FINAL_ANSWER_RE.search(stripped) or _TRAILING_NUMBER_RE.search(stripped))


def _answer_completion_rate(
    rows: Iterable[dict], cap: int = _NEAR_CAP_PROXY
) -> float | None:
    counted = 0
    completed = 0
    for row in rows:
        text = row.get("response_text")
        tokens_out = row.get("tokens_out")
        if not isinstance(text, str) or not text.strip():
            continue
        counted += 1
        truncated = isinstance(tokens_out, int) and tokens_out >= _row_cap(row, cap)
        if not truncated and _has_final_answer(text):
            completed += 1
    if counted == 0:
        return None
    return completed / counted


def _self_consistency(rows: Iterable[dict]) -> tuple[float | None, float | None]:
    by_item: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        score = row.get("score")
        item = row.get("item_id")
        # NUMERIC columns arrive as Decimal from psycopg2; treat them as
        # real-number scores (scores are small, fixed-precision decimals).
        if item is None or not isinstance(score, (int, float, Decimal)):
            continue
        by_item[item].append(float(score))
    multi = [values for values in by_item.values() if len(values) >= 2]
    if not multi:
        return None, None
    ranges = [max(values) - min(values) for values in multi]
    mean_range = sum(ranges) / len(ranges)
    unanimity = sum(1 for value_range in ranges if value_range <= 0.01) / len(multi)
    return mean_range, unanimity
