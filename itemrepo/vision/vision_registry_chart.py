from __future__ import annotations

from vision_chart_core import (
    CRT01_QUESTION,
    CRT04_QUESTION,
    CRT06_QUESTION,
    CRT07_QUESTION,
    crt01_answer,
    crt01_generate,
    crt04_answer,
    crt04_generate,
    crt06_answer,
    crt06_generate,
    crt07_answer,
    crt07_generate
)
from vision_chart_dense import (
    CRT08_QUESTION,
    CRT09_QUESTION,
    crt08_answer,
    crt08_generate,
    crt09_answer,
    crt09_generate
)
from vision_tier3 import (
    CRT_T3_QUESTION,
    crt_t3_answer,
    crt_t3_generate
)

ITEMS = [
{
        "item_key": "vision.chart_extract.bar-max",
        "slug": "crt_01_bar_max",
        "kind": "chart_extract",
        "tier": 2,
        "question": CRT01_QUESTION,
        "generate": crt01_generate,
        "answer_fn": crt01_answer,
        "seats": ["multimodal_looker", "artistry"],
    },
{
        "item_key": "vision.chart_extract.trend-direction",
        "slug": "crt_04_trend_direction",
        "kind": "chart_extract",
        "tier": 3,
        "question": CRT04_QUESTION,
        "generate": crt04_generate,
        "answer_fn": crt04_answer,
        "seats": ["multimodal_looker", "artistry"],
    },
{
        "item_key": "vision.chart_extract.eight-near-bars",
        "slug": "crt_06_eight_near_bars",
        "kind": "chart_extract",
        "tier": 4,
        "question": CRT06_QUESTION,
        "generate": crt06_generate,
        "answer_fn": crt06_answer,
        "seats": ["multimodal_looker", "artistry"],
    },
{
        "item_key": "vision.chart_extract.smallest-gap",
        "slug": "crt_07_smallest_gap",
        "kind": "chart_extract",
        "tier": 5,
        "question": CRT07_QUESTION,
        "generate": crt07_generate,
        "answer_fn": crt07_answer,
        "seats": ["multimodal_looker", "artistry"],
    },
{
        "item_key": "vision.chart_extract.three-lines-middle",
        "slug": "crt_08_three_lines_middle",
        "kind": "chart_extract",
        "tier": 5,
        "question": CRT08_QUESTION,
        "generate": crt08_generate,
        "answer_fn": crt08_answer,
        "seats": ["multimodal_looker", "artistry"],
    },
{
        "item_key": "vision.chart_extract.exact-double",
        "slug": "crt_09_exact_double",
        "kind": "chart_extract",
        "tier": 4,
        "question": CRT09_QUESTION,
        "generate": crt09_generate,
        "answer_fn": crt09_answer,
        "seats": ["multimodal_looker", "artistry"],
    },
{
        "item_key": "vision.chart_extract.tier3-bar-value",
        "slug": "crt_t3_bar_value",
        "kind": "chart_extract",
        "tier": 3,
        "question": CRT_T3_QUESTION,
        "generate": crt_t3_generate,
        "answer_fn": crt_t3_answer,
        "seats": ["multimodal_looker", "artistry"],
    }
]

CORE_ITEMS = ITEMS[:-1]
TIER3_ITEM = ITEMS[-1]
