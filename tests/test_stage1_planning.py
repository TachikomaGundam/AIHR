from __future__ import annotations

import hr.stage1 as stage1

from tests.test_stage1 import (
    Any,
    FakeAdapter,
    FinalistSelection,
    ITEM_REPO,
    STAGE1_DECIDING_BATTERIES,
    STAGE1_TOKEN_CAP,
    _FakeConn,
    _has_live_db_credentials,
    build_finals_plan,
    fleet_env,  # noqa: F401 (pytest fixture re-export; resolved by parameter name)
    fleet_models,
    load_full_banks,
    pytest,
    select_finalists_from_stage0,
)

def test_cli_no_db_disables_initialization_and_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    received: dict[str, Any] = {}
    monkeypatch.setattr("hr.adapters.RoutedAdapter", FakeAdapter)
    monkeypatch.setattr(
        "hr.stage1.run_finals",
        lambda *_args, **kwargs: received.update(kwargs),
    )

    # When
    result = stage1._cli_main(
        ["--no-db", "--models", "acme-ai/flash", "--item-repo", str(ITEM_REPO)]
    )

    # Then
    assert result == 0
    assert received["init_db"] is False
    assert received["record_to_db"] is False

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

def test_finalist_selection_db_missing_fallback(monkeypatch, fleet_env):  # noqa: F811 (fixture param shadows re-export)
    """Empty DB + allow_db_missing=True returns full pool as fallback."""
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

@pytest.mark.db
@pytest.mark.integration
def test_dry_run_with_override_finalists(capsys):
    """--dry-run with overridden finalists should produce a plan without touching DB.

    Finalist selection reads the live DB; credentials must come from the
    unified config env layer (HR_DSN/HR_DB_PASSWORD or the
    HR_COMPOSE_FILE opt-in) — skip otherwise.
    """
    if not _has_live_db_credentials():
        pytest.skip("requires live-DB credentials via HR_* env")
    from hr.stage1 import _cli_main

    # production entrypoint (hr.cli_app) runs init_schema at startup —
    # replicate it so "empty DB" means "no Stage 0 data" (scratch/job DBs only)
    from hr.db import init_schema

    init_schema()
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
