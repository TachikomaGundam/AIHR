"""Deterministic statistical fixtures for sequential-valid Stage 1 decisions.

Todo-3 contract (hr-evolution plan): every finalist pair decision goes
through an empirically-Bernstein confidence sequence over complete-round
paired differences — normalized to [0, 1] per item — and returns an effect
estimate, an anytime-valid interval, a resolution status (decided /
unresolvable / indeterminate) and a practical-effect decision (reject /
accept / indeterminate) against the configured ``min_effect`` region.

All fixtures are synthetic and fully seeded with ``np.random.default_rng(42)``
(matching the seeded-RNG convention in ``hr/stats/sequential.py``), so every
outcome is reproducible; the sequence machinery itself is RNG-free.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from hr.stats.sequential import (
    DEFAULT_MIN_EFFECT,
    SequentialConfig,
    bonferroni_pair_alpha,
    normalize_bounded_score,
)
from hr.stats.empirical_bernstein import EmpiricalBernsteinSequence, PairDecision

from hr.stage1_state import (
    STAGE1_STATE_VERSION,
    _purpose_with_state_version,
    _sweep_state_version,
    parse_state_version,
)

RNG = np.random.default_rng(42)

ITEM_REPO = Path(__file__).resolve().parents[1] / "itemrepo"


@dataclass
class FakeAdapter:
    """Minimal in-process adapter (mirrors tests/test_stage1.FakeAdapter)
    so the finals-loop fixtures run with no live API and no cross-test
    module coupling."""

    canned_tokens_in: int = 100
    canned_tokens_out: int = 50
    canned_latency_ms: int = 10
    call_log: list[dict[str, Any]] = field(default_factory=list)

    def probe_capabilities(self, model_id: str) -> object:
        from hr.adapters.base import Capabilities

        return Capabilities(
            model_id=model_id,
            provider=model_id.split("/", 1)[0] if "/" in model_id else "",
            supports_thinking=False,
            supports_vision=True,
        )

    def chat(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        **_: Any,
    ) -> object:
        from hr.graders.base import ModelResponse

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


class _ResumeCursor:
    """Cursor that yields one canned result set per execute (resume-path fake)."""

    def __init__(self, result_sets: list[list[tuple]]) -> None:
        self.result_sets = iter(result_sets)
        self.rows: list[tuple] = []

    def execute(self, *_: object, **__: object) -> None:
        self.rows = next(self.result_sets)

    def fetchall(self) -> list[tuple]:
        return self.rows

    def __enter__(self) -> "_ResumeCursor":
        return self

    def __exit__(self, *_: object) -> None:
        return None


class _ResumeConnection:
    def __init__(self, result_sets: list[list[tuple]]) -> None:
        self.cursor_ = _ResumeCursor(result_sets)

    def cursor(self) -> _ResumeCursor:
        return self.cursor_

    def close(self) -> None:
        return None


def _clipped(rng: np.random.Generator, mu: float, sigma: float, n: int) -> np.ndarray:
    """Normal draws clipped to [0, 1] (bounded-score boundary)."""
    return np.clip(rng.normal(mu, sigma, size=n), 0.0, 1.0)


def _round_diffs(
    rng: np.random.Generator, mu_a: float, mu_b: float, n_items: int, sigma: float = 0.05
) -> list[float]:
    a = _clipped(rng, mu_a, sigma, n_items)
    b = _clipped(rng, mu_b, sigma, n_items)
    return [float(x - y) for x, y in zip(a, b)]


# ---------------------------------------------------------------------------
# Normalization (bounded scores -> [0, 1])
# ---------------------------------------------------------------------------

def test_normalize_bounded_score_both_scales() -> None:
    """Bench scores (0-100) and grader/health scores (0-1) both map to [0,1]."""
    # Bench scale: ItemResult.score is 0-100 (hr/bench/engine_results.py).
    assert normalize_bounded_score(75.0, max_score=100.0) == pytest.approx(0.75)
    assert normalize_bounded_score(0.0, max_score=100.0) == 0.0
    # Grader scale: GradeResult.score is 0-1 (hr/graders/base.py).
    assert normalize_bounded_score(0.75, max_score=1.0) == pytest.approx(0.75)
    # Out-of-range scores are clamped: the EB bound requires bounded data.
    assert normalize_bounded_score(101.0, max_score=100.0) == 1.0
    assert normalize_bounded_score(-3.0, max_score=100.0) == 0.0
    with pytest.raises(ValueError):
        normalize_bounded_score(0.5, max_score=0.0)


def test_paired_diffs_are_normalized_before_sequence() -> None:
    """Normalized (bench-scale) scores fed as diffs land in [-1, 1]."""
    seq = EmpiricalBernsteinSequence(alpha=0.05, min_effect=0.05, max_rounds=6)
    seq.add_round(
        [
            normalize_bounded_score(75.0, max_score=100.0)
            - normalize_bounded_score(50.0, max_score=100.0)
        ]
    )
    assert seq.effect() == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Separated pair -> decided, practical effect rejected
# ---------------------------------------------------------------------------

def test_separated_pair_decides_and_rejects_practical_effect() -> None:
    """A strongly separated pair resolves: status decided, winner a, reject
    practical effect; the anytime-valid interval excludes the min_effect region."""
    seq = EmpiricalBernsteinSequence(alpha=0.05 / 1, min_effect=0.05, max_rounds=6)
    for _round in range(6):
        seq.add_round(_round_diffs(RNG, mu_a=0.75, mu_b=0.35, n_items=40))

    decision = seq.decide(model_a="a", model_b="b")
    assert decision.status == "decided"
    assert decision.winner == "a"
    assert decision.practical_effect == "reject"
    assert decision.ci_lower > decision.min_effect
    assert decision.effect == pytest.approx(0.4, abs=0.02)
    assert decision.n_rounds == 6
    assert decision.n_diffs == 240
    # Per-pair alpha is not consumed when k = 1 pair.
    assert decision.alpha == pytest.approx(0.05)


def test_separated_pair_not_decided_from_single_round() -> None:
    """One 40-item round is not enough: the sequence stays indeterminate
    (anytime-valid CS is wide early, then narrows as rounds accumulate)."""
    seq = EmpiricalBernsteinSequence(alpha=0.05, min_effect=0.05, max_rounds=6)
    seq.add_round(_round_diffs(RNG, mu_a=0.75, mu_b=0.35, n_items=40))
    decision = seq.decide(model_a="a", model_b="b")
    assert decision.status == "indeterminate"
    assert decision.winner is None


def test_decision_after_each_round_reflects_only_rounds_seen() -> None:
    """Running the same fixture a second time reproduces identical intervals
    (the sequence is deterministic — no RNG, matching the seeded convention)."""
    a = EmpiricalBernsteinSequence(alpha=0.05, min_effect=0.05, max_rounds=6)
    b = EmpiricalBernsteinSequence(alpha=0.05, min_effect=0.05, max_rounds=6)
    for _round in range(4):
        diffs = _round_diffs(RNG, mu_a=0.75, mu_b=0.35, n_items=40)
        a.add_round(diffs)
        b.add_round(list(diffs))
    da, db = a.decide(model_a="a", model_b="b"), b.decide(model_a="a", model_b="b")
    assert da.ci_lower == db.ci_lower
    assert da.ci_upper == db.ci_upper
    assert da.effect == db.effect


# ---------------------------------------------------------------------------
# Sub-min_effect / near-tie / underpowered pairs -> never a winner
# ---------------------------------------------------------------------------

def test_sub_min_effect_difference_never_decides_accepts_at_cap() -> None:
    """A real but practically negligible advantage (effect 0.03 < min_effect
    0.05) never produces a winner; with enough data the sequence *accepts*
    practical equivalence once the budget is exhausted."""
    seq = EmpiricalBernsteinSequence(alpha=0.05, min_effect=0.05, max_rounds=8)
    for _round in range(8):
        diffs = _clipped(RNG, 0.03, 0.02, 120) - _clipped(RNG, 0.0, 0.02, 120)
        seq.add_round([float(d) for d in diffs])
    decision = seq.decide(model_a="a", model_b="b")
    assert decision.status == "unresolvable"
    assert decision.winner is None
    assert decision.practical_effect == "accept"
    assert decision.effect == pytest.approx(0.03, abs=0.01)


def test_near_tie_pair_never_wins() -> None:
    """Two identical distributions → unresolvable at budget, never a winner."""
    seq = EmpiricalBernsteinSequence(alpha=0.05, min_effect=0.05, max_rounds=10)
    for _round in range(10):
        diffs = _clipped(RNG, 0.5, 0.1, 60) - _clipped(RNG, 0.5, 0.1, 60)
        seq.add_round([float(d) for d in diffs])
    decision = seq.decide(model_a="a", model_b="b")
    assert decision.status == "unresolvable"
    assert decision.winner is None
    assert decision.practical_effect in {"accept", "indeterminate"}


def test_underpowered_pair_indeterminate_then_unresolvable() -> None:
    """Tiny bank + high noise: interval covers the region and beyond →
    indeterminate while rounds remain, unresolvable once the budget is hit."""
    # Same data, two budgets.
    incoming: list[list[float]] = []
    rng = np.random.default_rng(42)
    for _round in range(8):
        diffs = _clipped(rng, 0.02, 0.35, 3) - _clipped(rng, 0.0, 0.35, 3)
        incoming.append([float(d) for d in diffs])

    still_open = EmpiricalBernsteinSequence(alpha=0.05, min_effect=0.05, max_rounds=10)
    for diffs in incoming:
        still_open.add_round(diffs)
    d_open = still_open.decide(model_a="a", model_b="b")
    assert d_open.status == "indeterminate"
    assert d_open.winner is None

    capped = EmpiricalBernsteinSequence(alpha=0.05, min_effect=0.05, max_rounds=8)
    for diffs in incoming:
        capped.add_round(diffs)
    d_capped = capped.decide(model_a="a", model_b="b")
    assert d_capped.status == "unresolvable"
    assert d_capped.winner is None


# ---------------------------------------------------------------------------
# Incomplete rounds / bounded evidence
# ---------------------------------------------------------------------------

def test_empty_sequence_is_indeterminate() -> None:
    """No complete rounds → no evidence → indeterminate, never a winner."""
    seq = EmpiricalBernsteinSequence(alpha=0.05, min_effect=0.05, max_rounds=3)
    decision = seq.decide(model_a="a", model_b="b")
    assert decision.status == "indeterminate"
    assert decision.practical_effect == "indeterminate"
    assert decision.winner is None
    assert decision.effect is None
    assert decision.ci_lower is None
    assert decision.ci_upper is None
    assert decision.n_diffs == 0


def test_battery_continuation_rule() -> None:
    """is_resolved() is False until a pair is decided or unresolvable."""
    seq = EmpiricalBernsteinSequence(alpha=0.05, min_effect=0.05, max_rounds=2)
    assert seq.is_resolved() is False  # no evidence
    seq.add_round([0.0])
    assert seq.is_resolved() is False  # still indeterminate (rounds remain)
    # Budget exhausted without a decisive interval -> unresolvable -> resolved.
    seq.add_round([0.0])
    assert seq.decide(model_a="a", model_b="b").status == "unresolvable"
    assert seq.is_resolved() is True


def test_pair_decision_report_dict_shape() -> None:
    """The report record carries the full contract for JSON fixtures."""
    seq = EmpiricalBernsteinSequence(alpha=0.025, min_effect=0.05, max_rounds=4)
    seq.add_round(_round_diffs(RNG, mu_a=0.7, mu_b=0.4, n_items=40))
    row = seq.decide(model_a="a", model_b="b").to_dict()
    assert set(row) == {
        "model_a", "model_b", "battery_code",
        "effect", "ci_lower", "ci_upper",
        "alpha", "min_effect", "n_rounds", "n_diffs",
        "status", "practical_effect", "winner", "rationale",
    }


# ---------------------------------------------------------------------------
# Bonferroni family alpha
# ---------------------------------------------------------------------------

def test_bonferroni_pair_alpha_splits_family_alpha() -> None:
    assert bonferroni_pair_alpha(0.05, 1) == pytest.approx(0.05)
    # 4 finalists -> C(4,2) = 6 pairs.
    assert bonferroni_pair_alpha(0.05, 6) == pytest.approx(0.05 / 6)
    assert bonferroni_pair_alpha(0.1, 2) == pytest.approx(0.05)
    with pytest.raises(ValueError):
        bonferroni_pair_alpha(0.05, 0)


# ---------------------------------------------------------------------------
# min_effect configuration (thresholds.yaml contract)
# ---------------------------------------------------------------------------

def test_from_yaml_requires_min_effect_entries(tmp_path) -> None:
    """Mirrors the half_width required-battery validation: a required battery
    without a min_effect entry is a loud config error."""
    p = tmp_path / "thresholds.yaml"
    p.write_text(
        "half_width:\n"
        "  reasoning: 2.0\n"
        "  hallucination: 2.0\n"
        "  tool_a: 3.0\n"
        "  tool_b: 5.0\n"
        "  vision: 3.0\n"
        "min_effect:\n"
        "  reasoning: 0.05\n"
        "  hallucination: 0.05\n"
        "  tool_a: 0.05\n"
        "  tool_b: 0.05\n"
        "  vision: 0.05\n"
        "n_initial: 3\n"
        "n_max: 10\n",
        encoding="utf-8",
    )
    req = ["reasoning", "hallucination", "tool_a", "tool_b", "vision"]
    cfg = SequentialConfig.from_yaml(str(p), required_batteries=req)
    assert cfg.min_effect_for("vision") == 0.05
    # Drop tool_b from min_effect only -> ValueError names it.
    p.write_text(
        p.read_text(encoding="utf-8").replace("  tool_b: 0.05\n", ""),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="tool_b"):
        SequentialConfig.from_yaml(str(p), required_batteries=req)
    # Entirely absent min_effect map -> ValueError listing the batteries.
    p.write_text(
        "half_width:\n"
        "  reasoning: 2.0\n"
        "  hallucination: 2.0\n"
        "  tool_a: 3.0\n"
        "  tool_b: 5.0\n"
        "  vision: 3.0\n"
        "n_initial: 3\n"
        "n_max: 10\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="min_effect"):
        SequentialConfig.from_yaml(str(p), required_batteries=req)


def test_min_effect_default_and_override() -> None:
    cfg = SequentialConfig(thresholds={})
    assert DEFAULT_MIN_EFFECT == 0.05
    assert cfg.min_effect_for("anything") == 0.05  # default when unset
    cfg.min_effect = {"vision": 0.10}  # per-battery override
    assert cfg.min_effect_for("vision") == 0.10
    assert cfg.min_effect_for("reasoning") == 0.05
    with pytest.raises(ValueError):
        SequentialConfig(thresholds={}, min_effect={"vision": 0.0}).min_effect_for("vision")


def test_real_thresholds_yaml_has_min_effect_for_every_battery() -> None:
    """The shipped config covers every half_width battery with a positive
    min_effect and honors n_max as the round budget cap."""
    path = SequentialConfig.__module__.replace("hr.stats.sequential", "configs")  # unused guard
    import hr.stage1_selection as sel

    cfg = SequentialConfig.from_yaml(str(sel.DEFAULT_THRESHOLDS_PATH))
    assert set(cfg.min_effect) == set(cfg.thresholds)
    for battery, value in cfg.min_effect.items():
        assert value > 0.0
    assert cfg.family_alpha == pytest.approx(0.05)
    assert cfg.n_max == 10
    assert cfg.max_rounds == 10  # semantic alias, no second config key
    # Stage-0 required-battery validation passes on the shipped config.
    from hr.stage0 import STAGE0_BATTERIES
    from hr.stage1_selection import DEFAULT_THRESHOLDS_PATH

    cfg2 = SequentialConfig.from_yaml(str(DEFAULT_THRESHOLDS_PATH), required_batteries=list(STAGE0_BATTERIES))
    assert all(cfg2.min_effect_for(b) > 0.0 for b in STAGE0_BATTERIES)


# ---------------------------------------------------------------------------
# Legacy in-progress sweep state: explicit invalidation, never reinterpreted
# ---------------------------------------------------------------------------

def test_state_version_marker_and_parsing() -> None:
    assert STAGE1_STATE_VERSION == 2
    # Legacy sweeps (pre-state-version) carry no marker.
    assert parse_state_version("Stage 1 finalists sweep\nfinalists: [a]\n") is None
    # Version-1 output from the old regime is explicit.
    assert parse_state_version("Stage 1 finalists sweep\nstate_version: 1\n") == 1
    purpose = _purpose_with_state_version("Stage 1 finalists sweep\nfinalists: [a]\n")
    assert parse_state_version(purpose) == STAGE1_STATE_VERSION
    assert "state_version: 2" in purpose
    # Restamping replaces a stale marker instead of stacking a second one.
    stale = _purpose_with_state_version("Stage 1 finalists sweep\nstate_version: 1\n")
    assert stale.count("state_version:") == 1
    assert parse_state_version(stale) == STAGE1_STATE_VERSION


def test_legacy_resume_requires_restart() -> None:
    """Resuming a legacy in-progress sweep must restart, never reinterpret:
    the restart decision is a pure function of the stored version."""
    from hr.stage1_state import _resume_requires_restart

    assert _resume_requires_restart(None) is True      # legacy sweep
    assert _resume_requires_restart(1) is True         # old decision regime
    assert _resume_requires_restart(STAGE1_STATE_VERSION) is False
    # A sweep with no recorded rounds (versionless, nothing to lose) may resume
    # only through a fresh id — conservatively treated as legacy too.
    assert _resume_requires_restart(99) is True        # future/unknown version


# ---------------------------------------------------------------------------
# Stage-1 wiring: the finals loop and resume path
# ---------------------------------------------------------------------------

def test_incomplete_pair_round_excluded_in_finals_loop() -> None:
    """A round interrupted by the token cap leaves model b with no scores:
    the partial round must be excluded from the pair sequence (never raise,
    never aggregate a half round)."""
    from hr.graders import build_default_registry
    from hr.stage1 import load_full_banks
    from hr.stage1_loop import _run_finals_loop
    from hr.stage1_state import Stage1SweepState

    items = load_full_banks(ITEM_REPO, batteries=("vision",))["vision"][:2]
    adapter = FakeAdapter(canned_tokens_in=10_000, canned_tokens_out=5_000)
    state = Stage1SweepState(sweep_id="sweep", finalists=["a", "b"])
    cfg = SequentialConfig(thresholds={"vision": 0.0}, n_initial=1, n_max=1)

    _run_finals_loop(
        adapter=adapter,
        item_repo=ITEM_REPO,
        finalists=["a", "b"],
        full_banks={"vision": items},
        batteries=("vision",),
        battery_ids={"vision": "battery"},
        seq_config=cfg,
        token_cap=20_000,  # one model completes 2 calls (30k), second model never starts
        state=state,
        registry=build_default_registry(),
        conn=None,
        sweep_id="sweep",
        record_to_db=False,
        already_recorded={},
        prior_rounds={},
    )

    pair = state.pair_stoppers["a|b|vision"]
    assert pair.n_rounds == 0          # partial round excluded, not aggregated
    assert pair.n_diffs == 0
    decision = pair.decide(model_a="a", model_b="b")
    assert decision.status == "indeterminate"
    assert decision.winner is None


def test_finals_loop_pair_sequence_gets_bonferroni_alpha() -> None:
    """Two finalists -> one pair -> per-pair alpha = family_alpha / 1;
    the sequence tracks complete-round diffs with an EB contract."""
    from hr.graders import build_default_registry
    from hr.stage1 import load_full_banks
    from hr.stage1_loop import _run_finals_loop
    from hr.stage1_state import Stage1SweepState

    item = load_full_banks(ITEM_REPO, batteries=("vision",))["vision"][0]
    state = Stage1SweepState(sweep_id="sweep", finalists=["a", "b"])
    cfg = SequentialConfig(
        thresholds={"vision": 0.0},
        n_initial=1,
        n_max=1,
        family_alpha=0.05,
        min_effect={"vision": 0.05},
    )
    _run_finals_loop(
        adapter=FakeAdapter(),
        item_repo=ITEM_REPO,
        finalists=["a", "b"],
        full_banks={"vision": [item]},
        batteries=("vision",),
        battery_ids={"vision": "battery"},
        seq_config=cfg,
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
    assert pair.effect() == pytest.approx(0.0)   # identical fake responses
    assert pair.alpha == pytest.approx(0.05)     # 1 pair -> no adjustment
    assert pair.min_effect == 0.05
    assert pair.max_rounds == 1
    assert pair.n_diffs == 1


def test_resume_rebuilds_pair_sequences_with_eb_contract() -> None:
    """The resume path reconstructs EB sequences (not bootstrap stoppers)
    from complete DB rounds, with Bonferroni alpha + min_effect + max_rounds."""
    from hr.stage1_resume import _rebuild_stopper_from_db
    from hr.stage1_state import Stage1SweepState

    connection = _ResumeConnection(
        [
            # Round-level rows: (model, battery, round, score).
            [
                ("a", "vision", 1, 0.8), ("a", "vision", 1, 0.7),
                ("b", "vision", 1, 0.6), ("b", "vision", 1, 0.5),
            ],
            # Per-item rows: (model, battery, item_id, repetition, score).
            [
                ("a", "vision", "i1", 1, 0.8), ("a", "vision", "i2", 1, 0.7),
                ("b", "vision", "i1", 1, 0.6), ("b", "vision", "i2", 1, 0.5),
            ],
            [(250, 4)],
        ]
    )
    state = Stage1SweepState(sweep_id="sweep", finalists=["a", "b"])
    cfg = SequentialConfig(
        thresholds={"vision": 3.0},
        n_initial=1,
        n_max=2,
        family_alpha=0.05,
        min_effect={"vision": 0.05},
    )
    _rebuild_stopper_from_db(
        state, connection, "sweep", ("vision",), cfg, expected_measurements={"vision": 2}
    )
    pair = state.pair_stoppers["a|b|vision"]
    assert pair.n_rounds == 1
    assert pair.n_diffs == 2
    assert pair.alpha == pytest.approx(0.05)
    assert pair.min_effect == 0.05
    assert pair.max_rounds == 2
    assert pair.effect() == pytest.approx(0.2)   # (0.8-0.6 + 0.7-0.5)/2
    assert pair.decide(model_a="a", model_b="b").winner is None


def test_separation_matrix_emits_decision_records() -> None:
    """_bootstrap_separation_from_stage1 reports effect/interval/status/
    practical_effect per pair alongside the legacy p_* columns."""
    from hr.stage1_stats import _bootstrap_separation_from_stage1
    from hr.stage1_state import Stage1SweepState, make_pair_sequence
    from hr.stats.sequential import SequentialConfig

    state = Stage1SweepState(sweep_id="test", finalists=["a", "b"])
    cfg = SequentialConfig(
        thresholds={"vision": 0.0}, n_initial=1, n_max=2, min_effect={"vision": 0.05}
    )
    seq = make_pair_sequence("vision", cfg, n_finalists=2)
    for _ in range(2):
        seq.add_round([float(d) for d in (_clipped(RNG, 0.8, 0.05, 40) - _clipped(RNG, 0.2, 0.05, 40))])
    state.pair_stoppers["a|b|vision"] = seq

    sep = _bootstrap_separation_from_stage1(state)
    assert "vision" in sep
    row = sep["vision"][0]
    assert row["model_a"] == "a"
    assert row["model_b"] == "b"
    assert row["status"] == "decided"
    assert row["winner"] == "a"
    assert row["practical_effect"] == "reject"
    assert row["effect"] == pytest.approx(0.6, abs=0.03)
    assert row["ci_lower"] > row["min_effect"]
    assert row["p_separated"] >= 0.9      # legacy column carries the anytime confidence


@pytest.mark.db
@pytest.mark.integration
def test_legacy_sweep_restarted_not_reinterpreted_live(scratch_db) -> None:
    """Live: resuming a legacy in-progress sweep restarts it — recorded
    legacy rounds never feed the sequence (every item re-called) and the
    purpose row is upgraded to the current state version."""
    import psycopg2

    from hr.graders import build_default_registry
    from hr.stage0_storage import _insert_sweep
    from hr.stage1 import load_full_banks
    from hr.stage1_loop import _run_finals_loop
    from hr.stage1_state import Stage1SweepState

    _dbname, dsn = scratch_db
    conn = psycopg2.connect(dsn)
    try:
        _insert_sweep(conn, "legacy-sweep-restart", "_stage1_finals", "Stage 1 finalists sweep\nfinalists: [a, b]\n")
        item = load_full_banks(ITEM_REPO, batteries=("vision",))["vision"][0]
        state = Stage1SweepState(sweep_id="legacy-sweep-restart", finalists=["a", "b"])
        adapter = FakeAdapter()
        _run_finals_loop(
            adapter=adapter,
            item_repo=ITEM_REPO,
            finalists=["a", "b"],
            full_banks={"vision": [item]},
            batteries=("vision",),
            battery_ids={"vision": "battery"},
            seq_config=SequentialConfig(thresholds={"vision": 0.0}, n_initial=1, n_max=1),
            token_cap=10_000,
            state=state,
            registry=build_default_registry(),
            conn=conn,
            sweep_id="legacy-sweep-restart",
            record_to_db=False,
            already_recorded={
                ("a", "vision", 1, item.item_key, 1): 0.9,
                ("b", "vision", 1, item.item_key, 1): 0.8,
            },
            prior_rounds={("a", "vision"): 1, ("b", "vision"): 1},
        )
        # Restart, not reinterpretation: both models re-called from scratch
        # (a plain resume would have skipped both recorded items).
        assert len(adapter.call_log) == 2
        assert state.pair_stoppers["a|b|vision"].n_rounds == 1
        # The legacy sweep row is upgraded to the current decision regime.
        from hr.stage1_state import _sweep_state_version

        assert _sweep_state_version(conn, "legacy-sweep-restart") == STAGE1_STATE_VERSION
    finally:
        conn.close()
    """Live: a legacy sweep row (no marker) reads back as version-less, a
    current-marker purpose reads back as STAGE1_STATE_VERSION, and
    _ensure_sweep_state_stamped upgrades a legacy purpose in place."""
    import psycopg2

    from hr.stage0_storage import _insert_sweep
    from hr.stage1_state import _ensure_sweep_state_stamped

    _dbname, dsn = scratch_db
    conn = psycopg2.connect(dsn)
    try:
        _insert_sweep(conn, "legacy-sweep-t3", "_stage1_finals", "Stage 1 finalists sweep\nfinalists: [a]\n")
        assert _sweep_state_version(conn, "legacy-sweep-t3") is None
        _insert_sweep(
            conn,
            "current-sweep-t3",
            "_stage1_finals",
            _purpose_with_state_version("Stage 1 finalists sweep\nfinalists: [a]\n"),
        )
        assert _sweep_state_version(conn, "current-sweep-t3") == STAGE1_STATE_VERSION
        # Idempotent: stamping an already-current sweep changes nothing.
        _ensure_sweep_state_stamped(conn, "current-sweep-t3")
        assert _sweep_state_version(conn, "current-sweep-t3") == STAGE1_STATE_VERSION
    finally:
        conn.close()