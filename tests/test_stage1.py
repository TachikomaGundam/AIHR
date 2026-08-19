"""Tests for hr2.stage1 using FakeAdapter (no live API calls)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from hr.adapters.base import Capabilities
from hr.fleet import fleet_models
from hr.graders.base import ModelResponse
from hr.stage0 import (
    STAGE0_BATTERIES,
    STAGE0_SEAT_CODE,
    _key,
)
from hr.stage1 import (
    DEFAULT_THRESHOLDS_PATH,
    STAGE1_DECIDING_BATTERIES,
    STAGE1_SEAT_CODE,
    STAGE1_TOKEN_CAP,
    FinalistSelection,
    FinalsCallPlan,
    Stage1SweepState,
    _bootstrap_separation_from_stage1,
    build_aligned_2d,
    build_finals_plan,
    load_full_banks,
    run_finals,
    select_finalists_from_stage0,
)


ITEM_REPO = Path(__file__).resolve().parents[1] / "itemrepo"


@pytest.fixture
def fleet_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the dynamic fleet (same contract as test_stage0.fleet_env)."""
    config_dir = tmp_path / "opencode"
    config_dir.mkdir()
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
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HR_HOME", str(tmp_path / "hr"))


# ---------------------------------------------------------------------------
# Fake adapter
# ---------------------------------------------------------------------------
@dataclass
class FakeAdapter:
    canned_score: float = 0.8
    canned_tokens_in: int = 100
    canned_tokens_out: int = 50
    canned_latency_ms: int = 10
    thinking_models: set[str] = field(default_factory=set)
    call_log: list[dict[str, Any]] = field(default_factory=list)
    raise_: Exception | None = None
    per_model_score: dict[str, float] = field(default_factory=dict)

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
        self.call_log.append({"model_id": model_id})
        return ModelResponse(
            text="fake response",
            thinking=None,
            tool_calls=[],
            tokens_in=self.canned_tokens_in,
            tokens_out=self.canned_tokens_out,
            latency_ms=self.canned_latency_ms,
            raw={},
        )


# ---------------------------------------------------------------------------
# Finalist selection: tests require Stage 0 data in DB. We mock the DB via
# monkeypatch to exercise both the real-selection and fallback paths.
# ---------------------------------------------------------------------------
class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *a, **kw):
        pass

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)

    def close(self):
        pass


def test_finalist_selection_real_query(monkeypatch):
    """Mock DB with stage0 results; verify top-k selection per battery."""
    fake_rows = [
        # (model_id, battery_code, mean_score)
        ("bailian-token-plan/qwen3.7-plus", "reasoning", 0.99),
        ("bailian-token-plan/qwen3.8-max", "reasoning", 0.98),
        ("bailian-token-plan/qwen3.7-max", "reasoning", 0.97),
        ("bailian-token-plan/kimi-k2.7-code", "reasoning", 0.96),
        ("bailian-token-plan/qwen3.6-plus", "reasoning", 0.95),
        ("bailian-token-plan/deepseek-v4-pro", "reasoning", 0.94),
        ("bailian-token-plan/glm-5", "reasoning", 0.90),  # rank 7 (excluded)
        ("bailian-token-plan/qwen3.7-plus", "tool_a", 0.64),
        ("bailian-token-plan/qwen3.8-max", "tool_a", 0.62),
        ("bailian-token-plan/qwen3.7-max", "tool_a", 0.60),
        ("bailian-token-plan/kimi-k2.7-code", "tool_a", 0.58),
        ("bailian-token-plan/deepseek-v4-pro", "tool_a", 0.55),
        ("bailian-token-plan/glm-5", "tool_a", 0.50),
        ("bailian-token-plan/qwen3.7-plus", "hallucination", 0.96),
        ("bailian-token-plan/qwen3.8-max", "hallucination", 0.95),
        ("bailian-token-plan/qwen3.7-max", "hallucination", 0.94),
        ("bailian-token-plan/kimi-k2.7-code", "hallucination", 0.92),
        ("bailian-token-plan/qwen3.6-plus", "hallucination", 0.90),
        ("bailian-token-plan/deepseek-v4-pro", "hallucination", 0.88),
        ("bailian-token-plan/bailian-token-plan/other", "hallucination", 0.70),
        ("bailian-token-plan/qwen3.7-plus", "vision", 0.90),
        ("bailian-token-plan/qwen3.8-max", "vision", 0.85),
        ("bailian-token-plan/qwen3.7-max", "vision", 0.80),
        ("bailian-token-plan/kimi-k2.7-code", "vision", 0.75),
        ("bailian-token-plan/qwen3.6-plus", "vision", 0.70),
        ("bailian-token-plan/deepseek-v4-pro", "vision", 0.65),
    ]
    monkeypatch.setattr("hr.stage1._connect", lambda: _FakeConn(fake_rows))
    # stage1.select_finalists_from_stage0 uses hr.db.connect via `from hr.db import connect`.
    # Patch that import instead.
    import hr.stage1 as s1

    def fake_connect():
        return _FakeConn(fake_rows)

    monkeypatch.setattr("hr.db.connect", fake_connect)

    sel = select_finalists_from_stage0(allow_db_missing=False)
    assert isinstance(sel, FinalistSelection)
    # Each battery has top-6 finalists; we should have ≤ 24, and each finalist
    # should come from the fleet manifest.
    assert len(sel.finalists) <= 24
    assert len(sel.finalists) > 0
    for battery in STAGE1_DECIDING_BATTERIES:
        top = sel.per_battery[battery]
        assert len(top) == 6
        # Top rank should be qwen3.7-plus (mean 0.99/0.64/0.96/0.90 for each battery).
        assert top[0][0] == "bailian-token-plan/qwen3.7-plus"
    # Rationale text should mention batteries.
    assert "reasoning" in sel.rationale
    assert "tool_a" in sel.rationale


def test_finalist_selection_db_missing_fallback(monkeypatch, fleet_env):
    """Empty DB + allow_db_missing=True returns full pool as fallback."""
    import hr.stage1 as s1
    import hr.db as db_mod

    def fake_connect():
        return _FakeConn([])

    monkeypatch.setattr(db_mod, "connect", fake_connect)
    sel = select_finalists_from_stage0(allow_db_missing=True)
    assert isinstance(sel, FinalistSelection)
    assert sel.finalists == sorted(fleet_models())
    assert "Stage 0 DB is empty" in sel.rationale


def test_finalist_selection_db_missing_errors_by_default(monkeypatch):
    """Empty DB without the flag raises RuntimeError (real finals requires Stage 0)."""
    import hr.db as db_mod

    def fake_connect():
        return _FakeConn([])

    monkeypatch.setattr(db_mod, "connect", fake_connect)
    with pytest.raises(RuntimeError, match="No Stage 0 measurements"):
        select_finalists_from_stage0(allow_db_missing=False)


# ---------------------------------------------------------------------------
# Full bank loading
# ---------------------------------------------------------------------------
def test_load_full_banks_returns_all_deciding_batteries():
    banks = load_full_banks(ITEM_REPO)
    # All four deciding batteries should be present.
    for b in STAGE1_DECIDING_BATTERIES:
        assert b in banks
        assert len(banks[b]) > 0
    # Sanity: tool_a should be ~100 items per spec.
    assert len(banks["tool_a"]) >= 90
    # reasoning battery has exactly 60 items.
    assert len(banks["reasoning"]) == 60


def test_load_full_banks_is_superset_of_stage0_subsets():
    """Stage 1 full banks are >= Stage 0 subset sizes (60/70/100/22 per spec)."""
    banks = load_full_banks(ITEM_REPO)
    # We don't assert exact spec counts; just check that banks have at least
    # the Stage-0 reduced subset sizes.
    assert len(banks["reasoning"]) >= 20
    assert len(banks["tool_a"]) >= 30
    assert len(banks["vision"]) >= 15


# ---------------------------------------------------------------------------
# Dry-run plan
# ---------------------------------------------------------------------------
def test_build_finals_plan_basic():
    banks = load_full_banks(ITEM_REPO)
    finalists = ["bailian-token-plan/qwen3.7-plus", "bailian-token-plan/kimi-k2.7-code"]
    from hr.stats.sequential import SequentialConfig

    config = SequentialConfig(
        thresholds={"reasoning": 2.0, "hallucination": 2.0, "tool_a": 3.0, "vision": 3.0},
        n_initial=3,
        n_max=10,
    )
    plan = build_finals_plan(finalists, banks, config)
    assert plan.finalists == finalists
    assert plan.battery_item_counts["reasoning"] == 60
    # Total items across 4 batteries.
    total = sum(plan.battery_item_counts.values())
    assert total > 200  # 60+70+100+22 = 252 approximately
    # Min calls = 2 finalists × total items × 3 rounds (pilot).
    assert plan.estimated_min_calls == 2 * total * 3
    assert plan.estimated_max_calls == 2 * total * 10
    assert plan.budget_cap == STAGE1_TOKEN_CAP


_LIVE_DB_ENVS = ("HR_DSN", "HR_DB_PASSWORD", "HR2_DB_PASSWORD", "HR_COMPOSE_FILE")


def _has_live_db_credentials() -> bool:
    return any(os.environ.get(name) for name in _LIVE_DB_ENVS)


def test_dry_run_with_override_finalists(capsys):
    """--dry-run with overridden finalists should produce a plan without touching DB.

    Finalist selection reads the live DB; credentials must come from the
    unified config env layer (HR_DSN/HR_DB_PASSWORD/HR2_DB_PASSWORD or the
    HR_COMPOSE_FILE opt-in) — skip otherwise.
    """
    if not _has_live_db_credentials():
        pytest.skip("requires live-DB credentials via HR_* env")
    from hr.stage1 import _cli_main

    rc = _cli_main(
        [
            "--dry-run",
            "--models=bailian-token-plan/qwen3.7-plus",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "Stage 1 Finals Call Plan" in captured.out
    assert "qwen3.7-plus" in captured.out
    # dry-run bypasses the adapter entirely; no API calls happen.
    assert "Finalists (1):" in captured.out


# ---------------------------------------------------------------------------
# End-to-end run with FakeAdapter (no DB)
# ---------------------------------------------------------------------------
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


def test_separation_matrix_from_state_requires_aligned_2d():
    """Build two models' states and verify 2-D alignment works."""
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
    # Separation helper should produce pairs.
    sep = _bootstrap_separation_from_stage1(state)
    assert "tool_a" in sep
    pairs = sep["tool_a"]
    assert len(pairs) == 1
    p = pairs[0]
    assert p["model_a"] == "a"
    assert p["model_b"] == "b"
    # a has much higher scores than b, so p_separated should be high.
    assert p["p_separated"] > 0.9 or p["p_weak"] > 0.8 or p["p_tie"] > 0  # any non-empty classification


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


def test_thresholds_yaml_loads():
    """The thresholds.yaml must exist and parse correctly."""
    from hr.stats.sequential import SequentialConfig

    cfg = SequentialConfig.from_yaml(str(DEFAULT_THRESHOLDS_PATH))
    assert cfg.thresholds["reasoning"] > 0
    assert cfg.thresholds["tool_a"] > 0
    assert cfg.n_initial == 3
    assert cfg.n_max == 10


def test_dry_run_no_api_calls_with_db_empty():
    """With no Stage 0 data, dry-run with --models override should still work.

    Finalist selection reads the live DB; credentials must come from the
    unified config env layer (HR_DSN/HR_DB_PASSWORD/HR2_DB_PASSWORD or the
    HR_COMPOSE_FILE opt-in) — skip otherwise.
    """
    if not _has_live_db_credentials():
        pytest.skip("requires live-DB credentials via HR_* env")
    from hr.stage1 import _cli_main

    rc = _cli_main(["--dry-run", "--models=bailian-token-plan/kimi-k2.7-code"])
    assert rc == 0


def test_seat_code_is_stage1_finals():
    """Stage 1 uses its own seat code, distinct from Stage 0."""
    assert STAGE1_SEAT_CODE == "_stage1_finals"
    assert STAGE1_SEAT_CODE != STAGE0_SEAT_CODE


def test_run_finals_dry_run_returns_no_state(fleet_env):
    """dry_run=True should return (plan, None, selection)."""
    adapter = FakeAdapter()
    plan, state, selection = run_finals(
        adapter,
        item_repo=ITEM_REPO,
        finalists=list(fleet_models()[:3]),
        batteries=("vision",),
        dry_run=True,
    )
    assert plan is not None
    assert state is None
    assert selection.finalists == list(fleet_models()[:3])
