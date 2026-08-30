from __future__ import annotations

from vision_ui_core import (
    UI01_QUESTION,
    UI04_QUESTION,
    UI06_QUESTION,
    UI07_QUESTION,
    ui01_answer,
    ui01_generate,
    ui04_answer,
    ui04_generate,
    ui06_answer,
    ui06_generate,
    ui07_answer,
    ui07_generate
)
from vision_ui_dense import (
    UI08_QUESTION,
    UI09_QUESTION,
    UI10_QUESTION,
    ui08_answer,
    ui08_generate,
    ui09_answer,
    ui09_generate,
    ui10_answer,
    ui10_generate
)
from vision_tier3 import (
    UI_T3_QUESTION,
    ui_t3_answer,
    ui_t3_generate
)

ITEMS = [
{
        "item_key": "vision.ui_read.sidebar-count",
        "slug": "ui_01_sidebar_count",
        "kind": "ui_read",
        "tier": 2,
        "question": UI01_QUESTION,
        "generate": ui01_generate,
        "answer_fn": ui01_answer,
        "seats": ["multimodal_looker", "visual_engineering"],
    },
{
        "item_key": "vision.ui_read.window-title-cta",
        "slug": "ui_04_window_title_cta",
        "kind": "ui_read",
        "tier": 2,
        "question": UI04_QUESTION,
        "generate": ui04_generate,
        "answer_fn": ui04_answer,
        "seats": ["multimodal_looker", "visual_engineering"],
    },
{
        "item_key": "vision.ui_read.dense-sidebar",
        "slug": "ui_06_dense_sidebar",
        "kind": "ui_read",
        "tier": 4,
        "question": UI06_QUESTION,
        "generate": ui06_generate,
        "answer_fn": ui06_answer,
        "seats": ["multimodal_looker", "visual_engineering"],
    },
{
        "item_key": "vision.ui_read.multi-state-form",
        "slug": "ui_07_multi_state_form",
        "kind": "ui_read",
        "tier": 5,
        "question": UI07_QUESTION,
        "generate": ui07_generate,
        "answer_fn": ui07_answer,
        "seats": ["multimodal_looker", "visual_engineering"],
    },
{
        "item_key": "vision.ui_read.near-label-buttons",
        "slug": "ui_08_near_label_buttons",
        "kind": "ui_read",
        "tier": 4,
        "question": UI08_QUESTION,
        "generate": ui08_generate,
        "answer_fn": ui08_answer,
        "seats": ["multimodal_looker", "visual_engineering"],
    },
{
        "item_key": "vision.ui_read.dashboard-cross-region",
        "slug": "ui_09_dashboard_cross_region",
        "kind": "ui_read",
        "tier": 5,
        "question": UI09_QUESTION,
        "generate": ui09_generate,
        "answer_fn": ui09_answer,
        "seats": ["multimodal_looker", "visual_engineering"],
    },
{
        "item_key": "vision.ui_read.cjk-dense-table",
        "slug": "ui_10_cjk_dense_table",
        "kind": "ui_read",
        "tier": 4,
        "question": UI10_QUESTION,
        "generate": ui10_generate,
        "answer_fn": ui10_answer,
        "seats": ["multimodal_looker", "visual_engineering"],
    },
{
        "item_key": "vision.ui_read.tier3-toggles",
        "slug": "ui_t3_toggles",
        "kind": "ui_read",
        "tier": 3,
        "question": UI_T3_QUESTION,
        "generate": ui_t3_generate,
        "answer_fn": ui_t3_answer,
        "seats": ["multimodal_looker", "visual_engineering"],
    }
]

CORE_ITEMS = ITEMS[:-1]
TIER3_ITEM = ITEMS[-1]
