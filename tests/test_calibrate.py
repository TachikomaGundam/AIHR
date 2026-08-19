"""Adapter smoke + FakeAdapter tests for hr2 calibrate.

The FakeAdapter returns canned :class:`ModelResponse` instances so the
calibration pipeline can be exercised without any live API calls. A
separate one-shot cheap-model smoke test is exercised only when the
``HR2_ADAPTER_LIVE_SMOKE`` env var is set.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from hr.adapters.base import AdapterError, Capabilities
from hr.graders.base import ModelResponse


# ---------------------------------------------------------------------------
# Fake adapter
# ---------------------------------------------------------------------------
@dataclass
class FakeAdapter:
    """A canned-response adapter used by the calibration tests."""

    canned_text: str = ""
    canned_thinking: str = ""
    canned_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    canned_tokens_in: int = 100
    canned_tokens_out: int = 50
    canned_latency_ms: int = 10
    thinking_models: set[str] = field(default_factory=set)
    call_log: list[dict[str, Any]] = field(default_factory=list)
    raise_: Exception | None = None

    def probe_capabilities(self, model_id: str) -> Capabilities:
        base = model_id.split("/", 1)[-1] if "/" in model_id else model_id
        provider = model_id.split("/", 1)[0] if "/" in model_id else ""
        return Capabilities(
            model_id=model_id,
            provider=provider,
            supports_thinking=base in self.thinking_models,
            supports_vision=True,
        )

    def chat(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        images: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        thinking_budget: int | None = None,
        max_output: int = 16384,
        timeout_s: int = 600,
    ) -> ModelResponse:
        if self.raise_ is not None:
            raise self.raise_
        self.call_log.append({
            "model_id": model_id,
            "messages": messages,
            "images": images,
            "tools": tools,
            "thinking_budget": thinking_budget,
            "max_output": max_output,
        })
        return ModelResponse(
            text=self.canned_text,
            thinking=self.canned_thinking,
            tool_calls=list(self.canned_tool_calls),
            latency_ms=self.canned_latency_ms,
            tokens_in=self.canned_tokens_in,
            tokens_out=self.canned_tokens_out,
        )


# ---------------------------------------------------------------------------
# Reasoning with simplified check
# ---------------------------------------------------------------------------
ITEM_REPO = Path(__file__).resolve().parents[1] / "itemrepo"


def test_reasoning_item_construction_and_grading() -> None:
    from hr.calibrate import (
        CalibrationRunner,
        build_messages,
        load_item_repo,
    )

    items = load_item_repo(ITEM_REPO, batteries=["reasoning"])
    all_items = items["reasoning"]
    assert len(all_items) == 60, f"expected 60 reasoning items, got {len(all_items)}"

    env = all_items[0]
    msgs = build_messages(env)
    assert msgs[0]["role"] == "user"
    assert env.payload["question"] in msgs[0]["content"]

    fake = FakeAdapter(
        canned_text=str(env.payload["answer_schema"]["expected_value"]),
    )
    runner = CalibrationRunner(
        adapter=fake,
        item_repo=ITEM_REPO,
        anchors={"cheap": "bailian-token-plan/deepseek-v4-flash"},
        batteries=["reasoning"],
    )
    gr = runner._grade(env, fake.chat("m", []))
    assert gr.passed is True, f"expected tier-1 reasoning to pass, got {gr}"


# ---------------------------------------------------------------------------
# Vision item: base64 PNG attached
# ---------------------------------------------------------------------------
def test_vision_image_attachment() -> None:
    from hr.calibrate import (
        CalibrationRunner,
        build_messages,
        load_item_repo,
        maybe_vision_image,
    )

    items = load_item_repo(ITEM_REPO, batteries=["vision"])
    assert items["vision"]
    env = items["vision"][0]
    imgs = maybe_vision_image(env, ITEM_REPO)
    assert imgs is not None
    assert imgs[0]["media_type"] == "image/png"

    msgs = build_messages(env)
    assert msgs[0]["content"] == env.payload["question"]

    # The runner's chat path should attach the image to the last user message.
    from hr.adapters.anthropic_compat import AnthropicCompatAdapter

    built = AnthropicCompatAdapter._attach_images(msgs, imgs)
    assert isinstance(built[-1]["content"], list)
    types = [b["type"] for b in built[-1]["content"]]
    assert "text" in types
    assert "image" in types


# ---------------------------------------------------------------------------
# Tool_a items: schema resolution via correct.arg_constraints + tools[].
# ---------------------------------------------------------------------------
def test_tool_a_schema_from_tools_array() -> None:
    from hr.calibrate import (
        CalibrationRunner,
        build_messages,
        load_item_repo,
    )

    items = load_item_repo(ITEM_REPO, batteries=["tool_a"])
    assert len(items["tool_a"]) == 100

    env = None
    for i in items["tool_a"]:
        if i.item_key == "tool_a.calculator.simple_mult":
            env = i
            break
    assert env is not None

    correct = env.payload["correct"]
    fn_name = correct["name"]  # "calculator"
    constraints = correct.get("arg_constraints", {})
    first_arg = next(iter(constraints))  # "expression"
    # The regex for this item is ^123\s*\*\s*456$; satisfy it literally.
    sample = "123 * 456"

    # Anthropic-style tool call: {name, input}.
    fake = FakeAdapter(
        canned_tool_calls=[{"name": fn_name, "input": {first_arg: sample}}],
    )
    runner = CalibrationRunner(
        adapter=fake,
        item_repo=ITEM_REPO,
        anchors={"c": "bailian-token-plan/deepseek-v4-flash"},
        batteries=["tool_a"],
    )
    msgs = build_messages(env)
    resp = fake.chat("m", msgs)
    gr = runner._grade(env, resp)
    assert gr.passed is True, f"expected tool_a to pass: {gr.detail}"


# ---------------------------------------------------------------------------
# Aggregation math: synthetic per-item scores
# ---------------------------------------------------------------------------
def test_aggregation_pass_rates() -> None:
    from hr.calibrate import CalibrationRunner, Measurement, TierBandVerdict
    from hr.items.schema import ItemEnvelope, ItemType

    # Build a synthetic report for the "reasoning" battery:
    # fake items at tiers 1, 3, 6: 5 each.
    class _StubEnv:
        def __init__(self, tier: int, key: str) -> None:
            self.tier = tier
            self.type = ItemType.REASONING
            self.item_key = key
            self.payload = {"question": "q"}
            self.grading = type("G", (), {"params": {}})()

    items_by_battery = {
        "reasoning": (
            [_StubEnv(1, f"r1_{i}") for i in range(5)]
            + [_StubEnv(3, f"r3_{i}") for i in range(5)]
            + [_StubEnv(6, f"r6_{i}") for i in range(5)]
        ),
    }

    measurements = (
        # tier 1: 5 passed + 0 failed -> 100%
        [
            Measurement(
                anchor="cheap", item_key=f"r1_{i}", battery="reasoning",
                tier=1, item_type="reasoning", score=1.0, passed=True,
                latency_ms=0, tokens_in=0, tokens_out=0,
            )
            for i in range(5)
        ]
        # tier 3: 3 passed + 2 failed -> 60%
        + [
            Measurement(
                anchor="cheap", item_key=f"r3_{i}", battery="reasoning",
                tier=3, item_type="reasoning",
                score=1.0 if i < 3 else 0.0,
                passed=i < 3,
                latency_ms=0, tokens_in=0, tokens_out=0,
            )
            for i in range(5)
        ]
        # tier 6: 2 passed + 3 failed -> 40% (above band max 25%)
        + [
            Measurement(
                anchor="cheap", item_key=f"r6_{i}", battery="reasoning",
                tier=6, item_type="reasoning",
                score=1.0 if i < 2 else 0.0,
                passed=i < 2,
                latency_ms=0, tokens_in=0, tokens_out=0,
            )
            for i in range(5)
        ]
    )

    runner = CalibrationRunner(
        adapter=FakeAdapter(),
        item_repo=ITEM_REPO,
        anchors={"cheap": "bailian-token-plan/deepseek-v4-flash"},
        batteries=["reasoning"],
    )
    verdicts = runner._evaluate(measurements, items_by_battery)
    assert len(verdicts) == 1
    bv = verdicts[0]
    assert bv.battery == "reasoning"

    verdict_by_tier = {tv.tier: tv for tv in bv.tier_verdicts}
    # tier 1: 100%, band [0.90..1.00] -> pass
    assert verdict_by_tier[1].pass_rate == 1.0
    assert verdict_by_tier[1].passed is True
    # tier 3: 60%, band [0.40..0.60] -> pass (boundary)
    assert verdict_by_tier[3].pass_rate == 0.6
    assert verdict_by_tier[3].passed is True
    # tier 6: 40%, band [0.05..0.25] -> FAIL
    assert verdict_by_tier[6].pass_rate == 0.4
    assert verdict_by_tier[6].passed is False
    # Battery verdict
    assert bv.passed is False


# ---------------------------------------------------------------------------
# Budget guard
# ---------------------------------------------------------------------------
def test_budget_guard_stops_at_cap() -> None:
    from hr.calibrate import CalibrationRunner, Measurement

    fake = FakeAdapter(
        canned_text="x",
        canned_tokens_in=1_000_000,
        canned_tokens_out=500_000,
    )
    runner = CalibrationRunner(
        adapter=fake,
        item_repo=ITEM_REPO,
        anchors={"cheap": "bailian-token-plan/deepseek-v4-flash"},
        batteries=["vision"],  # 15 items -> each call = 1.5M tokens
        token_cap=3_500_000,
    )
    report = runner.run()
    # After 2 calls (~3M) we'd approach the cap. After the 3rd call we'd
    # exceed it. The runner should stop before processing all 15 items.
    assert report.stopped_at_cap is True
    assert len(report.measurements) < 15
    # The total tokens in+out should be near (but >=) the cap at the
    # moment it stopped — not wildly more than cap + one call.
    total = report.total_tokens_in + report.total_tokens_out
    assert total >= 3_500_000
    assert total < 3_500_000 + 2_000_000


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------
def test_resume_skips_recorded_pairs() -> None:
    from hr.calibrate import CalibrationRunner

    fake = FakeAdapter(canned_text="x", canned_tokens_in=10, canned_tokens_out=10)
    runner = CalibrationRunner(
        adapter=fake,
        item_repo=ITEM_REPO,
        anchors={"cheap": "bailian-token-plan/deepseek-v4-flash"},
        batteries=["vision"],
        resume=True,
    )
    loaded = __import__("hr.calibrate", fromlist=["load_item_repo"]).load_item_repo(
        ITEM_REPO, batteries=["vision"]
    )["vision"]
    runner._recorded_pairs = {
        ("bailian-token-plan/deepseek-v4-flash", env.item_key)
        for env in loaded[:10]
    }
    report = runner.run()
    assert len(report.measurements) == len(loaded) - 10


# ---------------------------------------------------------------------------
# Thinking budget gating
# ---------------------------------------------------------------------------
def test_thinking_budget_gated_by_capability() -> None:
    fake = FakeAdapter(
        canned_text="42",
        canned_thinking="let me think...",
        thinking_models={"qwen3.7-plus"},
    )
    from hr.calibrate import CalibrationRunner, build_messages, load_item_repo

    items = load_item_repo(ITEM_REPO, batteries=["reasoning"])
    env = items["reasoning"][0]

    runner = CalibrationRunner(
        adapter=fake,
        item_repo=ITEM_REPO,
        anchors={"cheap": "bailian-token-plan/deepseek-v4-flash"},
        batteries=["reasoning"],
    )
    runner._call("bailian-token-plan/deepseek-v4-flash", env)
    assert fake.call_log
    call = fake.call_log[0]
    # cheap model doesn't support thinking -> budget should be None.
    assert call["thinking_budget"] is None

    # Now route through a thinking-capable model.
    fake.call_log.clear()
    runner._call("bailian-token-plan/qwen3.7-plus", env)
    call = fake.call_log[0]
    assert call["thinking_budget"] == 8192
