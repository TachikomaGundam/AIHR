from __future__ import annotations

import re

from hr.bench.scorer_shared import _BenchmarkOutcome

def score_vision(text: str) -> _BenchmarkOutcome:
    """Count (25) + 4 colors (4 × 15) + 4 positions (4 × 3.75); cap 100."""
    t = text.lower()
    points = 0.0
    # count
    if "four" in t or re.search(r"\b4\b", text):
        points += 25
    # colors
    for color in ["red", "blue", "green", "yellow"]:
        if color in t:
            points += 15
    # positions - each color matched to its correct corner = 3.75
    corners = [
        ("red",    ["top-left", "top left", "upper-left", "upper left", "topleft"]),
        ("blue",   ["top-right", "top right", "upper-right", "upper right", "topright"]),
        ("green",  ["bottom-left", "bottom left", "lower-left", "lower left", "bottomleft"]),
        ("yellow", ["bottom-right", "bottom right", "lower-right", "lower right", "bottomright"]),
    ]
    for color, cues in corners:
        color_pos = t.find(color)
        if color_pos < 0:
            continue
        window = t[max(0, color_pos - 100) : color_pos + 200]
        if any(cue in window for cue in cues):
            points += 3.75
    score = min(points, 100.0)
    return _BenchmarkOutcome(
        score=score, passed=(score >= 80.0), raw_output=text,
        item_scores=[("image", score >= 80.0)],
    )


def skip_vision_outcome() -> _BenchmarkOutcome:
    return _BenchmarkOutcome(
        score=0.0, passed=False, raw_output="SKIP: no vision support",
        status="not_applicable",
    )


# ---------------------------------------------------------------------------
# speed — tok/s tiers (same constants as v1), scored from response metadata
# ---------------------------------------------------------------------------


def score_speed(tokens_out: int, latency_ms: int, text: str) -> _BenchmarkOutcome:
    """Tier the tokens-per-second rate exactly like v1's ``_score_speed``.

    v1 computed tps = output_tokens / total_seconds from the SSE response;
    the adapter reports the same values as ``tokens_out`` + ``latency_ms``.
    """
    seconds = latency_ms / 1000.0 if latency_ms > 0 else 0.0
    tps = (tokens_out / seconds) if seconds > 0 else 0.0
    latency = latency_ms
    if tps > 80:
        score = 90.0
    elif tps > 50:
        score = 75.0
    elif tps > 30:
        score = 60.0
    elif tps > 15:
        score = 45.0
    else:
        score = 30.0
    return _BenchmarkOutcome(
        score=score, passed=True, latency_ms=latency,
        tokens_per_sec=round(tps, 2), raw_output=text,
        item_scores=[("speed", True)],
    )

__all__ = ["score_vision", "skip_vision_outcome", "score_speed"]
