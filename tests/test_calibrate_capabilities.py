from __future__ import annotations

from tests.test_calibrate import (
    FakeAdapter,
    ITEM_REPO
)

def test_thinking_budget_gated_by_capability() -> None:
    fake = FakeAdapter(
        canned_text="42",
        canned_thinking="let me think...",
        thinking_models={"qwen3.7-plus"},
    )
    from hr.calibrate import CalibrationRunner, load_item_repo

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
