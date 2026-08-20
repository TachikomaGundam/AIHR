from __future__ import annotations

from tests.test_stage1 import fleet_env
from tests.test_stage1 import (
    DEFAULT_THRESHOLDS_PATH,
    FakeAdapter,
    ITEM_REPO,
    STAGE0_SEAT_CODE,
    STAGE1_SEAT_CODE,
    _has_live_db_credentials,
    fleet_models,
    pytest,
    run_finals
)

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
