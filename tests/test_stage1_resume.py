from __future__ import annotations

from tests.test_stage1 import (
    DEFAULT_THRESHOLDS_PATH,
    FakeAdapter,
    FinalsCallPlan,
    ITEM_REPO,
    Stage1SweepState,
    _ResumeConnection,
    _bootstrap_separation_from_stage1,
    _key,
    _rebuild_stopper_from_db,
    _run_finals_loop,
    build_aligned_2d,
    load_full_banks,
    run_finals
)

def test_run_finals_small_no_db():
    """Smoke: run 1 finalist × 1 battery × full bank with no-DB mode; verify
    that SweepState is populated with measurements and tokens."""
    adapter = FakeAdapter()
    plan, state, selection = run_finals(
        adapter,
        item_repo=ITEM_REPO,
        finalists=["bailian-token-plan/qwen3.7-plus"],
        batteries=("vision",),
        thresholds_path=DEFAULT_THRESHOLDS_PATH,
        n_initial=1,
        n_max=2,
        token_cap=50_000_000,
        init_db=False,
        record_to_db=False,
    )
    assert isinstance(plan, FinalsCallPlan)
    assert state is not None
    assert len(state.finalists) == 1
    # Vision battery has 22 items; 1 finalist × 22 items × 1 round minimum.
    mb_key = _key("bailian-token-plan/qwen3.7-plus", "vision")
    assert mb_key in state.measurements_by_model_battery
    # Should have 22 items measured.
    per_item = state.measurements_by_model_battery[mb_key]
    assert len(per_item) == 22
    # Tokens accounted.
    assert state.total_tokens > 0
    assert state.total_calls > 0
    # Sequential stopper was created for vision battery.
    assert "vision" in state.stoppers
    assert "bailian-token-plan/qwen3.7-plus|vision" in state.model_stoppers

def test_run_finals_budget_cap_triggers():
    """When token_cap is set very low, the runner should halt early and mark stopped_at_cap."""
    adapter = FakeAdapter(canned_tokens_in=10_000, canned_tokens_out=5_000)
    # Cap to force early stop after ~1 call (each call = 15k tokens).
    plan, state, _ = run_finals(
        adapter,
        item_repo=ITEM_REPO,
        finalists=["bailian-token-plan/qwen3.7-plus"],
        batteries=("vision",),
        n_initial=3,
        n_max=5,
        token_cap=20_000,
        init_db=False,
        record_to_db=False,
    )
    assert state is not None
    assert state.stopped_at_cap
    assert "Token cap" in state.stopped_reason

def test_resume_restores_spent_token_and_call_budget() -> None:
    from hr.stats.sequential import SequentialConfig

    # Given: two persisted measurements from an interrupted sweep.
    connection = _ResumeConnection(
        [
            [("model", "vision", 1, 0.8), ("model", "vision", 1, 0.7)],
            [("model", "vision", "i1", 1, 0.8), ("model", "vision", "i2", 1, 0.7)],
            [(250, 2)],
        ]
    )
    state = Stage1SweepState(sweep_id="sweep", finalists=["model"])
    config = SequentialConfig(thresholds={"vision": 3.0}, n_initial=1, n_max=2)

    # When: sequential state is rebuilt for resume.
    _rebuild_stopper_from_db(state, connection, "sweep", ("vision",), config)

    # Then: the remaining budget includes work performed before interruption.
    assert state.total_tokens == 250
    assert state.total_calls == 2

def test_resume_does_not_reuse_measurement_from_an_earlier_round() -> None:
    from hr.graders import build_default_registry
    from hr.stats.sequential import SequentialConfig

    # Given: round one contains the same item/repetition identity as round two.
    item = load_full_banks(ITEM_REPO, batteries=("vision",))["vision"][0]
    adapter = FakeAdapter()
    state = Stage1SweepState(sweep_id="sweep", finalists=["model"])
    config = SequentialConfig(thresholds={"vision": 0.0}, n_initial=2, n_max=2)

    # When: a resumed sweep starts round two.
    _run_finals_loop(
        adapter=adapter,
        item_repo=ITEM_REPO,
        finalists=["model"],
        full_banks={"vision": [item]},
        batteries=("vision",),
        battery_ids={"vision": "battery"},
        seq_config=config,
        token_cap=10_000,
        state=state,
        registry=build_default_registry(),
        conn=None,
        sweep_id="sweep",
        record_to_db=False,
        already_recorded={
            ("model", "vision", 1, item.item_key, 1): 0.8,
        },
        prior_rounds={("model", "vision"): 1},
    )

    # Then: the new round makes a fresh model call.
    assert len(adapter.call_log) == 1

def test_resume_completes_an_interrupted_round() -> None:
    from hr.graders import build_default_registry
    from hr.stats.sequential import SequentialConfig

    # Given: only the first item from round one was persisted.
    items = load_full_banks(ITEM_REPO, batteries=("vision",))["vision"][:2]
    adapter = FakeAdapter()
    state = Stage1SweepState(sweep_id="sweep", finalists=["model"])
    config = SequentialConfig(thresholds={"vision": 0.0}, n_initial=1, n_max=1)

    # When: the interrupted sweep resumes.
    _run_finals_loop(
        adapter=adapter,
        item_repo=ITEM_REPO,
        finalists=["model"],
        full_banks={"vision": items},
        batteries=("vision",),
        battery_ids={"vision": "battery"},
        seq_config=config,
        token_cap=10_000,
        state=state,
        registry=build_default_registry(),
        conn=None,
        sweep_id="sweep",
        record_to_db=False,
        already_recorded={
            ("model", "vision", 1, items[0].item_key, 1): 0.8,
        },
        prior_rounds={("model", "vision"): 1},
    )

    # Then: only the missing item is called and the round completes once.
    assert len(adapter.call_log) == 1
    assert state.n_rounds_done["vision"] == 1


def test_pairwise_stopper_records_complete_round_difference() -> None:
    from hr.graders import build_default_registry
    from hr.stats.sequential import SequentialConfig

    item = load_full_banks(ITEM_REPO, batteries=("vision",))["vision"][0]
    state = Stage1SweepState(sweep_id="sweep", finalists=["a", "b"])

    _run_finals_loop(
        adapter=FakeAdapter(),
        item_repo=ITEM_REPO,
        finalists=["a", "b"],
        full_banks={"vision": [item]},
        batteries=("vision",),
        battery_ids={"vision": "battery"},
        seq_config=SequentialConfig(thresholds={"vision": 0.0}, n_initial=1, n_max=1),
        token_cap=10_000,
        state=state,
        registry=build_default_registry(),
        conn=None,
        sweep_id="sweep",
        record_to_db=False,
        already_recorded={},
        prior_rounds={},
    )

    pair = state.pair_stoppers["a|b|vision"]
    assert pair.n_rounds == 1
    assert pair.diffs == [0.0]
    # Budget (n_max=1) exhausted with an unresolved interval: unbudgeted,
    # never a winner.
    decision = pair.decide(model_a="a", model_b="b")
    assert decision.winner is None
    assert decision.status in {"unresolvable", "indeterminate"}

def test_separation_matrix_from_state_requires_aligned_2d():
    """Build two models' states and verify the pair decision row."""
    state = Stage1SweepState(sweep_id="test", finalists=["a", "b"])
    # Model a: 3 items × 2 reps each.
    state.measurements_by_model_battery[_key("a", "tool_a")] = {
        "i1": [0.8, 0.9],
        "i2": [0.7, 0.8],
        "i3": [0.9, 1.0],
    }
    # Model b: lower scores.
    state.measurements_by_model_battery[_key("b", "tool_a")] = {
        "i1": [0.3, 0.4],
        "i2": [0.2, 0.3],
        "i3": [0.4, 0.5],
    }
    # Alignment helper.
    arr_a, keys_a = build_aligned_2d(state.measurements_by_model_battery[_key("a", "tool_a")])
    arr_b, keys_b = build_aligned_2d(state.measurements_by_model_battery[_key("b", "tool_a")])
    assert arr_a.shape == arr_b.shape == (3, 2)
    assert keys_a == keys_b
    # The separation report is driven by the pair's anytime-valid sequence,
    # seeded by the finals loop with complete-round paired differences.
    from hr.stage1_state import make_pair_sequence
    from hr.stats.sequential import SequentialConfig

    seq = make_pair_sequence(
        "tool_a",
        SequentialConfig(thresholds={"tool_a": 3.0}, n_initial=1, n_max=3),
        2,
    )
    for _ in range(40):
        seq.add_round([float(x - y) for x, y in zip(arr_a[:, 0], arr_b[:, 0])])
    state.pair_stoppers["a|b|tool_a"] = seq
    # Separation helper should produce the decision row.
    sep = _bootstrap_separation_from_stage1(state)
    assert "tool_a" in sep
    pairs = sep["tool_a"]
    assert len(pairs) == 1
    p = pairs[0]
    assert p["model_a"] == "a"
    assert p["model_b"] == "b"
    # a has much higher scores than b -> decided with a as the winner.
    assert p["status"] == "decided"
    assert p["winner"] == "a"
    assert p["p_separated"] >= 0.9

def test_build_aligned_2d_pads_to_max_reps():
    """If one item has 3 reps and another has 1, pad with NaN to 3."""
    import numpy as np

    per_item = {"a": [0.5, 0.6, 0.7], "b": [0.4]}
    arr, keys = build_aligned_2d(per_item)
    assert arr.shape == (2, 3)
    assert keys == ["a", "b"]
    # 'b' should be padded with NaNs.
    assert np.isnan(arr[1, 1])
    assert np.isnan(arr[1, 2])
    assert arr[1, 0] == 0.4
