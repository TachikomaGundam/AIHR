from __future__ import annotations

from tests.test_stage0 import fleet_env  # noqa: F401 (pytest fixture re-export; resolved by parameter name)

from tests.test_stage0 import (
    Any,
    CallPlan,
    FakeAdapter,
    ITEM_REPO,
    ModelResponse,
    STAGE0_BATTERIES,
    STAGE0_SUBSET_SIZES,
    SweepState,
    _bootstrap_separation_from_state,
    _print_matrix,
    call_and_grade,
    fleet_models,
    pytest,
    run_sweep,
    select_subsets,
    stage0
)

def test_call_and_grade_failure_returns_infra() -> None:
    """Adapter exception is handled as infra failure."""
    from hr.calibrate import load_item_repo
    from hr.graders import build_default_registry

    items = load_item_repo(ITEM_REPO, batteries=["reasoning"])
    env = items["reasoning"][0]
    adapter = FakeAdapter(raise_=TimeoutError("connection timed out"))
    registry = build_default_registry()
    ok, result = call_and_grade(adapter, "bailian-token-plan/qwen3.7-plus", env, ITEM_REPO, registry)
    assert ok is False
    assert result.score == 0.0
    assert result.infra_failure is not None

def test_run_sweep_dry_run(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
    """--dry-run should not call adapter."""
    adapter = FakeAdapter()
    plan, state = run_sweep(adapter, item_repo=ITEM_REPO, n_initial=3, dry_run=True)
    assert isinstance(plan, CallPlan)
    assert state is None
    assert len(adapter.call_log) == 0

def test_cli_no_db_disables_initialization_and_recording(
    fleet_env, monkeypatch: pytest.MonkeyPatch  # noqa: F811 (fixture param shadows re-export)
) -> None:
    # Given
    received: dict[str, Any] = {}
    monkeypatch.setattr("hr.adapters.RoutedAdapter", FakeAdapter)
    monkeypatch.setattr(
        "hr.stage0.run_sweep",
        lambda *_args, **kwargs: received.update(kwargs),
    )

    # When
    result = stage0._cli_main(
        ["--no-db", "--models", "acme-ai/flash", "--item-repo", str(ITEM_REPO)]
    )

    # Then
    assert result == 0
    assert received["init_db"] is False
    assert received["record_to_db"] is False

def test_run_sweep_end_to_end_no_db(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
    """Run a tiny sweep (2 models × 2 rounds) with no DB writes."""
    from hr.graders.base import GraderRegistry, GradeResult

    class StubUnitTestGrader:
        """Stands in for UnitTestGrader — no pytest subprocess per call."""

        name = "unit_test"
        version = "1.0"

        def grade(
            self,
            item_payload: dict,
            grading_params: dict,
            response: ModelResponse,
        ) -> GradeResult:
            return GradeResult(1.0, True, detail={"checks": []})

    registry = GraderRegistry()
    registry.register(StubUnitTestGrader(), "unit_test", "1.0")

    adapter = FakeAdapter(canned_text="fake response", canned_tokens_in=100, canned_tokens_out=50)
    small_models = fleet_models()[:2]
    plan, state = run_sweep(
        adapter,
        item_repo=ITEM_REPO,
        models=small_models,
        n_initial=2,
        init_db=False,
        record_to_db=False,
        registry=registry,
    )
    assert state is not None
    assert state.stopped_at_cap is False
    # 2 models × sum(subsets) items × 2 rounds
    expected_calls = len(small_models) * sum(STAGE0_SUBSET_SIZES.values()) * 2
    assert state.total_calls == expected_calls
    # Tokens = calls × (tokens_in + tokens_out)
    assert state.total_tokens == expected_calls * 150
    # Every battery has measurements recorded per model.
    for battery in STAGE0_BATTERIES:
        for model in small_models:
            key = f"{model}|{battery}"
            assert key in state.measurements_by_model_battery

def test_run_sweep_hits_token_cap(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
    """Sweep should stop when token cap is reached."""
    adapter = FakeAdapter(canned_tokens_in=1_000_000, canned_tokens_out=500_000)  # 1.5M per call
    small_models = fleet_models()[:1]
    plan, state = run_sweep(
        adapter,
        item_repo=ITEM_REPO,
        models=small_models,
        n_initial=3,
        token_cap=3_000_000,  # stop after 2 calls
        init_db=False,
        record_to_db=False,
    )
    assert state is not None
    assert state.stopped_at_cap is True
    assert state.total_tokens >= 3_000_000

def test_bootstrap_separation_from_state_basic() -> None:
    """Build a fake sweep state and compute separation."""
    state = SweepState(sweep_id="test")
    # Model A consistently scores higher than B in reasoning.
    state.measurements_by_model_battery["model_a|reasoning"] = {f"item_{i}": [0.9, 0.95] for i in range(10)}
    state.measurements_by_model_battery["model_b|reasoning"] = {f"item_{i}": [0.1, 0.2] for i in range(10)}
    sep = _bootstrap_separation_from_state(state)
    pairs = sep["reasoning"]
    assert len(pairs) == 1  # one pair: A vs B
    p = pairs[0]
    assert p["p_separated"] > 0.9

def test_bootstrap_separation_records_the_better_model_as_model_a() -> None:
    # Given: the alphabetically later model consistently wins.
    state = SweepState(sweep_id="test")
    state.measurements_by_model_battery["a_model|reasoning"] = {
        f"item_{index}": [0.1, 0.2] for index in range(10)
    }
    state.measurements_by_model_battery["z_model|reasoning"] = {
        f"item_{index}": [0.9, 0.95] for index in range(10)
    }

    # When: directional separation is computed.
    pair = _bootstrap_separation_from_state(state)["reasoning"][0]

    # Then: model_a identifies the winner rather than lexical order.
    assert pair["model_a"] == "z_model"
    assert pair["model_b"] == "a_model"
    assert pair["directional"] is True

def test_separation_matrix_printing(capsys) -> None:
    """Smoke-test the matrix printer."""
    sep = {
        "reasoning": [
            {"model_a": "m1", "model_b": "m2", "p_separated": 0.98, "p_weak": 0.0, "p_tie": 0.02},
        ]
    }
    _print_matrix(sep)
    captured = capsys.readouterr()
    assert "reasoning" in captured.out
    assert "Models (2)" in captured.out

def test_subset_selection_is_idempotent() -> None:
    """The same load yields the same subsets on repeated calls."""
    from hr.calibrate import load_item_repo

    items1 = load_item_repo(ITEM_REPO, batteries=list(STAGE0_BATTERIES))
    items2 = load_item_repo(ITEM_REPO, batteries=list(STAGE0_BATTERIES))
    s1 = select_subsets(items1)
    s2 = select_subsets(items2)
    for b in STAGE0_BATTERIES:
        assert [e.item_key for e in s1[b]] == [e.item_key for e in s2[b]]
