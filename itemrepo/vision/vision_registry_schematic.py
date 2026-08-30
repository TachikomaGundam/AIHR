from __future__ import annotations

from vision_schematic_flow import (
    SCH03_QUESTION,
    SCH06_QUESTION,
    SCH07_QUESTION,
    sch03_answer,
    sch03_generate,
    sch06_answer,
    sch06_generate,
    sch07_answer,
    sch07_generate
)
from vision_schematic_network import (
    SCH08_QUESTION,
    SCH09_QUESTION,
    SCH10_QUESTION,
    sch08_answer,
    sch08_generate,
    sch09_answer,
    sch09_generate,
    sch10_answer,
    sch10_generate
)
from vision_tier3 import (
    SCH_T3_QUESTION,
    sch_t3_answer,
    sch_t3_generate
)

ITEMS = [
{
        "item_key": "vision.schematic.signal-flow",
        "slug": "sch_03_signal_flow",
        "kind": "schematic",
        "tier": 3,
        "question": SCH03_QUESTION,
        "generate": sch03_generate,
        "answer_fn": sch03_answer,
        "seats": ["multimodal_looker", "circuit_engineer"],
    },
{
        "item_key": "vision.schematic.dense-path",
        "slug": "sch_06_dense_path",
        "kind": "schematic",
        "tier": 5,
        "question": SCH06_QUESTION,
        "generate": sch06_generate,
        "answer_fn": sch06_answer,
        "seats": ["multimodal_looker", "circuit_engineer"],
    },
{
        "item_key": "vision.schematic.bypass-path",
        "slug": "sch_07_bypass_path",
        "kind": "schematic",
        "tier": 5,
        "question": SCH07_QUESTION,
        "generate": sch07_generate,
        "answer_fn": sch07_answer,
        "seats": ["multimodal_looker", "circuit_engineer"],
    },
{
        "item_key": "vision.schematic.node-degree",
        "slug": "sch_08_node_degree",
        "kind": "schematic",
        "tier": 4,
        "question": SCH08_QUESTION,
        "generate": sch08_generate,
        "answer_fn": sch08_answer,
        "seats": ["multimodal_looker", "circuit_engineer"],
    },
{
        "item_key": "vision.schematic.dual-destination",
        "slug": "sch_09_dual_destination",
        "kind": "schematic",
        "tier": 4,
        "question": SCH09_QUESTION,
        "generate": sch09_generate,
        "answer_fn": sch09_answer,
        "seats": ["multimodal_looker", "circuit_engineer"],
    },
{
        "item_key": "vision.schematic.dense-resistor-net",
        "slug": "sch_10_dense_resistor_net",
        "kind": "schematic",
        "tier": 5,
        "question": SCH10_QUESTION,
        "generate": sch10_generate,
        "answer_fn": sch10_answer,
        "seats": ["multimodal_looker", "circuit_engineer"],
    },
{
        "item_key": "vision.schematic.tier3-next-component",
        "slug": "sch_t3_next_component",
        "kind": "schematic",
        "tier": 3,
        "question": SCH_T3_QUESTION,
        "generate": sch_t3_generate,
        "answer_fn": sch_t3_answer,
        "seats": ["multimodal_looker", "circuit_engineer"],
    }
]

CORE_ITEMS = ITEMS[:-1]
TIER3_ITEM = ITEMS[-1]
