from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.test_calibrate import (
    FakeAdapter,
    ITEM_REPO
)

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

def test_vision_image_attachment() -> None:
    from hr.calibrate import (
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

def test_aggregation_pass_rates() -> None:
    from hr.calibrate import CalibrationRunner, Measurement
    from hr.items.schema import ItemType

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


def test_aggregation_is_inconclusive_when_measurements_are_incomplete() -> None:
    from hr.calibrate import CalibrationRunner, Measurement
    from hr.items.schema import ItemType

    class _StubEnv:
        def __init__(self, key: str) -> None:
            self.tier = 1
            self.type = ItemType.REASONING
            self.item_key = key

    items_by_battery = {"reasoning": [_StubEnv("r1_a"), _StubEnv("r1_b")]}
    measurements = [
        Measurement(
            anchor="cheap", item_key="r1_a", battery="reasoning",
            tier=1, item_type="reasoning", score=1.0, passed=True,
            latency_ms=0, tokens_in=0, tokens_out=0,
        )
    ]

    runner = CalibrationRunner(
        adapter=FakeAdapter(), item_repo=ITEM_REPO,
        anchors={"cheap": "bailian-token-plan/deepseek-v4-flash"},
        batteries=["reasoning"],
    )

    verdict = runner._evaluate(measurements, items_by_battery)[0]

    assert verdict.passed is False
    assert verdict.status == "inconclusive"
    assert verdict.tier_verdicts[0].status == "inconclusive"


def test_aggregation_is_inconclusive_when_the_adapter_fails() -> None:
    from hr.calibrate import CalibrationRunner, Measurement
    from hr.items.schema import ItemType

    class _StubEnv:
        def __init__(self, key: str) -> None:
            self.tier = 1
            self.type = ItemType.REASONING
            self.item_key = key

    items_by_battery = {"reasoning": [_StubEnv("r1_a")]}
    measurements = [
        Measurement(
            anchor="cheap", item_key="r1_a", battery="reasoning",
            tier=1, item_type="reasoning", score=0.0, passed=False,
            latency_ms=0, tokens_in=0, tokens_out=0,
            infra_failure="AdapterError: timeout",
        )
    ]

    runner = CalibrationRunner(
        adapter=FakeAdapter(), item_repo=ITEM_REPO,
        anchors={"cheap": "bailian-token-plan/deepseek-v4-flash"},
        batteries=["reasoning"],
    )

    verdict = runner._evaluate(measurements, items_by_battery)[0]

    assert verdict.passed is False
    assert verdict.status == "inconclusive"
    assert verdict.tier_verdicts[0].status == "inconclusive"


def test_report_marks_inconclusive_verdicts_in_json_and_console(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hr.calibrate import (
        BatteryVerdict,
        CalibrationReport,
        TierBandVerdict,
        _report_to_dict,
        print_rendered_report,
    )

    tier = TierBandVerdict(
        battery="reasoning", tier=1, anchor="cheap", pass_rate=0.0,
        band_lo=0.9, band_hi=1.0, passed=False, status="inconclusive",
    )
    report = CalibrationReport(
        pool_hash="pool", measurements=[],
        verdicts=[
            BatteryVerdict(
                battery="reasoning", anchor="cheap", tier_verdicts=[tier],
                passed=False, status="inconclusive",
            )
        ],
    )

    print_rendered_report(report)

    assert "INCONCLUSIVE" in capsys.readouterr().out
    assert _report_to_dict(report)["verdicts"][0]["status"] == "inconclusive"


def test_report_renders_token_cap_warning_and_measurement_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hr.calibrate import (
        CalibrationReport,
        Measurement,
        _report_to_dict,
        print_rendered_report,
    )

    report = CalibrationReport(
        pool_hash="pool",
        measurements=[
            Measurement(
                anchor="cheap", item_key="item", battery="reasoning",
                tier=1, item_type="reasoning", score=0.0, passed=False,
                latency_ms=1, tokens_in=2, tokens_out=3,
                infra_failure="timeout",
            )
        ],
        verdicts=[],
        stopped_at_cap=True,
        total_tokens_in=2,
        total_tokens_out=3,
    )

    print_rendered_report(report)

    assert "stopped at token cap" in capsys.readouterr().out
    assert _report_to_dict(report)["measurements"][0]["infra_failure"] == "timeout"


def test_aggregation_is_invalid_without_an_acceptance_tier() -> None:
    from hr.calibrate import CalibrationRunner
    from hr.items.schema import ItemType

    class _StubEnv:
        tier = 2
        type = ItemType.REASONING
        item_key = "tier2"

    runner = CalibrationRunner(
        adapter=FakeAdapter(), item_repo=ITEM_REPO,
        anchors={"cheap": "bailian-token-plan/deepseek-v4-flash"},
        batteries=["reasoning"],
    )

    verdict = runner._evaluate([], {"reasoning": [_StubEnv()]})[0]

    assert verdict.status == "invalid"


def test_aggregation_passes_a_complete_single_tier_battery() -> None:
    from hr.calibrate import CalibrationRunner, Measurement
    from hr.items.schema import ItemType

    class _StubEnv:
        tier = 1
        type = ItemType.REASONING
        item_key = "tier1"

    measurement = Measurement(
        anchor="cheap", item_key="tier1", battery="reasoning", tier=1,
        item_type="reasoning", score=1.0, passed=True, latency_ms=0,
        tokens_in=0, tokens_out=0,
    )
    runner = CalibrationRunner(
        adapter=FakeAdapter(), item_repo=ITEM_REPO,
        anchors={"cheap": "bailian-token-plan/deepseek-v4-flash"},
        batteries=["reasoning"],
    )

    verdict = runner._evaluate([measurement], {"reasoning": [_StubEnv()]})[0]

    assert verdict.status == "pass"


def test_call_records_adapter_failure_as_inconclusive_input() -> None:
    from hr.calibrate import CalibrationRunner, load_item_repo

    env = load_item_repo(ITEM_REPO, batteries=["reasoning"])["reasoning"][0]
    runner = CalibrationRunner(
        adapter=FakeAdapter(raise_=RuntimeError("gateway unavailable")),
        item_repo=ITEM_REPO,
        anchors={"cheap": "bailian-token-plan/deepseek-v4-flash"},
        batteries=["reasoning"],
    )

    response, meta = runner._call("bailian-token-plan/deepseek-v4-flash", env)

    assert response is None
    assert meta["ok"] is False
    assert "RuntimeError" in str(meta["error"])


def test_grade_rejects_an_unroutable_item_type() -> None:
    from hr.calibrate import CalibrationRunner
    from hr.graders.base import ModelResponse

    runner = CalibrationRunner(
        adapter=FakeAdapter(), item_repo=ITEM_REPO,
        anchors={"cheap": "bailian-token-plan/deepseek-v4-flash"},
        batteries=["reasoning"],
    )
    envelope = SimpleNamespace(type="unsupported")

    result = runner._grade(envelope, ModelResponse())

    assert result.passed is False
    assert result.detail == {"no_routing": True}
