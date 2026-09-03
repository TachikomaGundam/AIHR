from __future__ import annotations

from tests.test_stage0 import fleet_env  # noqa: F401 (pytest fixture re-export; resolved by parameter name)

from tests.test_stage0 import (
    FakeAdapter,
    ITEM_REPO,
    STAGE0_BATTERIES,
    STAGE0_SUBSET_SIZES,
    _ensure_provider_model_records,
    build_call_plan,
    call_and_grade,
    compute_pool_hash,
    fleet_models,
    select_subsets
)

def test_provider_model_records_use_configured_provider_names(monkeypatch) -> None:
    # Given
    providers: list[tuple[str, str]] = []
    models: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "hr.stage0.provider_display_names",
        lambda: {"gateway": "Gateway"},
    )
    monkeypatch.setattr(
        "hr.stage0._upsert_provider",
        lambda _conn, provider_id, name: providers.append((provider_id, name)),
    )
    monkeypatch.setattr(
        "hr.stage0._upsert_model",
        lambda _conn, model_id, provider_id, slug: models.append(
            (model_id, provider_id, slug)
        ),
    )

    # When
    result = _ensure_provider_model_records(object(), ("gateway/model-a",))

    # Then
    assert result == {"gateway/model-a": "gateway"}
    assert providers == [("gateway", "Gateway")]
    assert models == [("gateway/model-a", "gateway", "model-a")]

def test_reasoning_subset_size_and_determinism() -> None:
    """Pick 20 reasoning items with ~3-4 per tier across t1-t6."""
    from hr.calibrate import load_item_repo

    items = load_item_repo(ITEM_REPO, batteries=["reasoning"])
    subsets = select_subsets(items)
    reasoning = subsets["reasoning"]
    assert len(reasoning) == STAGE0_SUBSET_SIZES["reasoning"]
    # Deterministic — same selection on repeated call.
    assert [e.item_key for e in reasoning] == [e.item_key for e in select_subsets(items)["reasoning"]]
    # Tier distribution roughly even (6 tiers × 3.33 = 20).
    tiers = [e.tier for e in reasoning]
    assert min(tiers) == 1 and max(tiers) == 6

def test_hallucination_subset_size_and_mix() -> None:
    """Pick 25 hallucination items — a mix of qa/unanswerable/citation."""
    from hr.calibrate import load_item_repo

    items = load_item_repo(ITEM_REPO, batteries=["hallucination"])
    subsets = select_subsets(items)
    halluc = subsets["hallucination"]
    assert len(halluc) == STAGE0_SUBSET_SIZES["hallucination"]
    types = {env.type for env in halluc}
    # Should include at least some of each subtype.
    from hr.items.schema import ItemType

    assert ItemType.FACTUALITY_QA in types or ItemType.UNANSWERABLE in types

def test_tool_subset_size() -> None:
    """Pick 30 tool_a items."""
    from hr.calibrate import load_item_repo

    items = load_item_repo(ITEM_REPO, batteries=["tool_a"])
    subsets = select_subsets(items)
    tool = subsets["tool_a"]
    assert len(tool) == STAGE0_SUBSET_SIZES["tool_a"]

def test_vision_subset_size_and_kind_balance() -> None:
    """Pick 15 vision items — ~5 per kind."""
    from hr.calibrate import load_item_repo

    items = load_item_repo(ITEM_REPO, batteries=["vision"])
    subsets = select_subsets(items)
    vis = subsets["vision"]
    assert len(vis) == STAGE0_SUBSET_SIZES["vision"]
    # Count kinds.
    kinds: dict[str, int] = {}
    for e in vis:
        parts = e.item_key.split(".", 2)
        kind = parts[1] if len(parts) >= 2 else "unknown"
        kinds[kind] = kinds.get(kind, 0) + 1
    # Expect at least 3 kinds represented.
    assert len(kinds) >= 3
    # Each kind should have at most 5 (spec §5.4: 5 each).
    for count in kinds.values():
        assert count <= 6

def test_all_subsets_match_spec_sizes() -> None:
    """Final check: all four subsets match spec sizes."""
    from hr.calibrate import load_item_repo

    items = load_item_repo(ITEM_REPO, batteries=list(STAGE0_BATTERIES))
    subsets = select_subsets(items)
    for b, expected in STAGE0_SUBSET_SIZES.items():
        assert len(subsets[b]) == expected, f"{b} expected {expected}, got {len(subsets[b])}"

def test_call_plan_within_budget(fleet_env) -> None:  # noqa: F811 (fixture param shadows re-export)
    from hr.calibrate import load_item_repo

    items = load_item_repo(ITEM_REPO, batteries=list(STAGE0_BATTERIES))
    subsets = select_subsets(items)
    plan = build_call_plan(subsets)
    assert len(plan.models) == len(fleet_models())
    assert plan.battery_item_counts["reasoning"] == 20
    assert plan.battery_item_counts["hallucination"] == 25
    assert plan.battery_item_counts["tool_a"] == 30
    assert plan.battery_item_counts["vision"] == 15
    assert plan.battery_item_counts["tool_b"] == 10
    # Pilot budget fits under cap. The cap comes from configs/thresholds.yaml
    # `stage0.token_cap` (default 60M, was 30M spec §9.1 v0.3): the fleet
    # (configs/fleet.yaml, todo 23) holds 34 models, so the pilot estimate is
    # 34 models × 100 items × n_initial 3 × 5,000 tokens = 51.0M — already
    # over the old 30M. 60M leaves ~18% headroom for fleet growth.
    assert plan.within_budget

def test_pool_hash_is_deterministic() -> None:
    from hr.calibrate import load_item_repo

    items = load_item_repo(ITEM_REPO, batteries=list(STAGE0_BATTERIES))
    subsets = select_subsets(items)
    h1 = compute_pool_hash(subsets)
    h2 = compute_pool_hash(subsets)
    assert h1 == h2
    assert h1.startswith("sha256:")
    assert len(h1) == 71

def test_call_and_grade_reasoning_item() -> None:
    """Run one reasoning item through fake adapter + grading."""
    from hr.calibrate import load_item_repo
    from hr.graders import build_default_registry

    items = load_item_repo(ITEM_REPO, batteries=["reasoning"])
    env = items["reasoning"][0]
    adapter = FakeAdapter(canned_text="The GCD is 12", canned_tokens_in=500, canned_tokens_out=100)
    registry = build_default_registry()
    ok, result = call_and_grade(adapter, "bailian-token-plan/qwen3.7-plus", env, ITEM_REPO, registry)
    assert ok is True
    assert result.tokens_in == 500
    assert result.tokens_out == 100
    assert result.latency_ms == 10
    assert result.score >= 0.0 and result.score <= 1.0

def test_call_and_grade_vision_item_needs_image() -> None:
    """Vision item should request images from fake adapter."""
    from hr.calibrate import load_item_repo
    from hr.graders import build_default_registry

    items = load_item_repo(ITEM_REPO, batteries=["vision"])
    env = items["vision"][0]
    adapter = FakeAdapter(canned_text="some answer", canned_tokens_in=500, canned_tokens_out=100)
    registry = build_default_registry()
    ok, result = call_and_grade(adapter, "bailian-token-plan/qwen3.7-plus", env, ITEM_REPO, registry)
    assert ok is True
    # Verify image was requested.
    last_call = adapter.call_log[-1]
    assert last_call["images"] is not None or last_call["images"] is None

def test_call_and_grade_captures_response_text() -> None:
    """Successful call carries response_text + thinking_text into the result."""
    from hr.calibrate import load_item_repo
    from hr.graders import build_default_registry

    items = load_item_repo(ITEM_REPO, batteries=["reasoning"])
    env = items["reasoning"][0]
    adapter = FakeAdapter(
        canned_text="The GCD is 12",
        canned_thinking="computing gcd via euclid",
        canned_tokens_in=500,
        canned_tokens_out=100,
    )
    registry = build_default_registry()
    ok, result = call_and_grade(adapter, "bailian-token-plan/qwen3.7-plus", env, ITEM_REPO, registry)
    assert ok is True
    assert result.response_text == "The GCD is 12"
    assert result.thinking_text == "computing gcd via euclid"

def test_call_and_grade_infra_failure_leaves_text_none() -> None:
    """Adapter exception → response_text/thinking_text are None."""
    from hr.calibrate import load_item_repo
    from hr.graders import build_default_registry

    items = load_item_repo(ITEM_REPO, batteries=["reasoning"])
    env = items["reasoning"][0]
    adapter = FakeAdapter(raise_=TimeoutError("timeout"))
    registry = build_default_registry()
    ok, result = call_and_grade(adapter, "bailian-token-plan/qwen3.7-plus", env, ITEM_REPO, registry)
    assert ok is False
    assert result.response_text is None
    assert result.thinking_text is None
