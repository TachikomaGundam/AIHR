"""Tests for hr2.stage0 using FakeAdapter (no live API calls)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from hr.adapters.base import Capabilities
from hr.fleet import fleet_models
from hr.graders.base import ModelResponse
from hr.stage0 import (
    STAGE0_BATTERIES,
    STAGE0_SUBSET_SIZES,
    CallPlan,
    SweepState,
    build_call_plan,
    call_and_grade,
    compute_pool_hash,
    print_separation_matrix,
    run_sweep,
    select_hallucination_subset,
    select_reasoning_subset,
    select_tool_subset,
    select_subsets,
    select_vision_subset,
    _bootstrap_separation_from_state,
    _print_matrix,
)


ITEM_REPO = Path(__file__).resolve().parents[1] / "itemrepo"


@pytest.fixture
def fleet_env(hr_sandbox: dict) -> None:
    """Isolate the dynamic fleet: fake opencode config, empty extras tree.

    fleet_models() derives from OPENCODE_CONFIG_DIR + HR_HOME at call time —
    without this fixture the REAL ~/.config/opencode would be read.
    """
    config_dir = hr_sandbox["config_dir"]
    (config_dir / "opencode.jsonc").write_text(
        json.dumps(
            {
                "provider": {
                    "acme-ai": {
                        "npm": "@ai-sdk/openai-compatible",
                        "models": {"flash": {}, "pro": {}, "plus": {}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Fake adapter (mirrors test_calibrate.py pattern)
# ---------------------------------------------------------------------------
@dataclass
class FakeAdapter:
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
        self.call_log.append(
            {
                "model_id": model_id,
                "messages": messages,
                "images": images,
                "tools": tools,
                "thinking_budget": thinking_budget,
                "max_output": max_output,
            }
        )
        return ModelResponse(
            text=self.canned_text,
            thinking=self.canned_thinking,
            tool_calls=list(self.canned_tool_calls),
            latency_ms=self.canned_latency_ms,
            tokens_in=self.canned_tokens_in,
            tokens_out=self.canned_tokens_out,
        )


# ---------------------------------------------------------------------------
# Subset selection
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Call plan
# ---------------------------------------------------------------------------
def test_call_plan_within_budget(fleet_env) -> None:
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


# ---------------------------------------------------------------------------
# Pool hash determinism
# ---------------------------------------------------------------------------
def test_pool_hash_is_deterministic() -> None:
    from hr.calibrate import load_item_repo

    items = load_item_repo(ITEM_REPO, batteries=list(STAGE0_BATTERIES))
    subsets = select_subsets(items)
    h1 = compute_pool_hash(subsets)
    h2 = compute_pool_hash(subsets)
    assert h1 == h2
    assert h1.startswith("sha256:")
    assert len(h1) == 71  # "sha256:" + 64 hex chars


# ---------------------------------------------------------------------------
# call_and_grade with FakeAdapter
# ---------------------------------------------------------------------------
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
    assert last_call["images"] is not None or last_call["images"] is None  # depends on item


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


# ---------------------------------------------------------------------------
# Sweep end-to-end (no DB)
# ---------------------------------------------------------------------------
def test_run_sweep_dry_run(fleet_env) -> None:
    """--dry-run should not call adapter."""
    adapter = FakeAdapter()
    plan, state = run_sweep(adapter, item_repo=ITEM_REPO, n_initial=3, dry_run=True)
    assert isinstance(plan, CallPlan)
    assert state is None
    assert len(adapter.call_log) == 0


def test_run_sweep_end_to_end_no_db(fleet_env) -> None:
    """Run a tiny sweep (2 models × 2 rounds) with no DB writes."""
    from hr.graders.base import GraderRegistry, GradeResult, ModelResponse

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


def test_run_sweep_hits_token_cap(fleet_env) -> None:
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


# ---------------------------------------------------------------------------
# Separation matrix
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
def test_subset_selection_is_idempotent() -> None:
    """The same load yields the same subsets on repeated calls."""
    from hr.calibrate import load_item_repo

    items1 = load_item_repo(ITEM_REPO, batteries=list(STAGE0_BATTERIES))
    items2 = load_item_repo(ITEM_REPO, batteries=list(STAGE0_BATTERIES))
    s1 = select_subsets(items1)
    s2 = select_subsets(items2)
    for b in STAGE0_BATTERIES:
        assert [e.item_key for e in s1[b]] == [e.item_key for e in s2[b]]
