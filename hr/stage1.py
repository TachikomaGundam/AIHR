
from __future__ import annotations

import uuid
from pathlib import Path


from hr.config import itemrepo_path
from hr.graders import build_default_registry
from hr.stats.sequential import SequentialConfig

# Reuse stage0's helpers and DB plumbing.
from hr.stage0 import (
    STAGE0_BATTERIES,
    _AdapterFacade,
    _connect,
    _ensure_provider_model_records,
    _init_db,
    _insert_separation,
    _insert_sweep,
    _print_matrix,
    _upsert_battery,
    _upsert_battery_item,
    _upsert_item_pool,
    _upsert_seat,
)

from hr.stage1_selection import (
    STAGE1_DECIDING_BATTERIES,
    STAGE1_N_INITIAL,
    STAGE1_N_MAX,
    STAGE1_TOKEN_CAP,
    STAGE1_SEAT_CODE,
    DEFAULT_THRESHOLDS_PATH,
    FinalistSelection,
    select_finalists_from_stage0,
    load_full_banks,
)
from hr.stage1_state import (
    Stage1SweepState,
    RecordedMeasurements,
    _recorded_measurement_keys,
    _max_round_per_model_battery,
)
from hr.stage1_loop import _run_finals_loop
from hr.stage1_resume import _rebuild_stopper_from_db
from hr.stage1_stats import (
    _bootstrap_separation_from_stage1,
    build_aligned_2d,
)
from hr.stage1_plan import (
    FinalsCallPlan,
    build_finals_plan,
    print_finals_plan,
)

__all__ = (
    "DEFAULT_THRESHOLDS_PATH",
    "FinalistSelection",
    "FinalsCallPlan",
    "STAGE1_DECIDING_BATTERIES",
    "STAGE1_SEAT_CODE",
    "STAGE1_TOKEN_CAP",
    "Stage1SweepState",
    "_bootstrap_separation_from_stage1",
    "_cli_main",
    "_connect",
    "_rebuild_stopper_from_db",
    "_run_finals_loop",
    "build_aligned_2d",
    "build_finals_plan",
    "load_full_banks",
    "run_finals",
    "select_finalists_from_stage0",
)

def run_finals(
    adapter: _AdapterFacade,
    *,
    item_repo: Path | None = None,
    finalists: list[str] | None = None,
    batteries: tuple[str, ...] = STAGE1_DECIDING_BATTERIES,
    thresholds_path: Path = DEFAULT_THRESHOLDS_PATH,
    n_initial: int = STAGE1_N_INITIAL,
    n_max: int = STAGE1_N_MAX,
    token_cap: int = STAGE1_TOKEN_CAP,
    sweep_id: str | None = None,
    dry_run: bool = False,
    init_db: bool = True,
    record_to_db: bool = True,
    allow_db_missing_for_finalists: bool = False,
) -> tuple[FinalsCallPlan, Stage1SweepState | None, FinalistSelection]:
    """Run the Stage 1 finals sweep.

    Returns (plan, state, selection). ``state`` is None on dry_run.
    ``item_repo`` defaults to :func:`hr.config.itemrepo_path`.
    """
    if item_repo is None:
        item_repo = itemrepo_path()
    # 1. Finalist selection.
    if finalists is None:
        selection = select_finalists_from_stage0(
            deciding_batteries=batteries,
            allow_db_missing=allow_db_missing_for_finalists,
        )
        finalists = selection.finalists
    else:
        # User-overridden finalist list.
        selection = FinalistSelection(
            per_battery={},
            finalists=list(finalists),
            rationale=f"User-provided finalist list: {finalists}",
        )

    # 2. Load full banks.
    full_banks = load_full_banks(item_repo, batteries=batteries)

    # 3. Configure sequential stopper.
    if thresholds_path.exists():
        seq_config = SequentialConfig.from_yaml(
            str(thresholds_path), required_batteries=list(STAGE0_BATTERIES)
        )
    else:
        # Use defaults from thresholds.yaml spec.
        seq_config = SequentialConfig(
            thresholds={
                "reasoning": 2.0,
                "hallucination": 2.0,
                "tool_a": 3.0,
                "vision": 3.0,
                "tool_b": 5.0,
            },
            n_initial=n_initial,
            n_max=n_max,
        )
    # Override from CLI args.
    seq_config.n_initial = n_initial
    seq_config.n_max = n_max

    # 4. Build plan (for reporting / dry-run).
    plan = build_finals_plan(finalists, full_banks, seq_config, budget_cap=token_cap)
    plan.finalist_selection = selection
    if dry_run:
        print_finals_plan(plan, selection)
        return plan, None, selection

    # 5. Init DB + records.
    if init_db or record_to_db:
        _init_db()
        conn = _connect()
    else:
        conn = None

    try:
        # 6. Upsert reference data.
        if conn is not None:
            _upsert_seat(conn, STAGE1_SEAT_CODE, "Stage 1 finalists sweep")
            _ensure_provider_model_records(conn, tuple(finalists))
            battery_ids: dict[str, str] = {}
            for bcode in batteries:
                battery_ids[bcode] = _upsert_battery(conn, bcode, f"Stage-1 {bcode} battery (full bank)")
            for bcode in batteries:
                b_id = battery_ids[bcode]
                for pos, env in enumerate(full_banks.get(bcode, [])):
                    _upsert_item_pool(conn, env)
                    _upsert_battery_item(conn, b_id, env.item_key, pos)

            # Create or resume sweep.
            if sweep_id is None:
                sweep_id = f"stage1-{uuid.uuid4()}"
            purpose = (
                f"Stage 1 finalists sweep\n"
                f"finalists: {finalists}\n"
                f"selection_rationale:\n{selection.rationale}\n"
                f"full_bank_sizes: { {b: len(items) for b, items in full_banks.items()} }\n"
                f"n_initial: {n_initial}, n_max: {n_max}, token_cap: {token_cap}\n"
                f"thresholds (battery -> half_width): {dict(seq_config.thresholds)}"
            )
            _insert_sweep(conn, sweep_id, STAGE1_SEAT_CODE, purpose)
        else:
            if sweep_id is None:
                sweep_id = f"stage1-{uuid.uuid4()}"
            battery_ids = {b: f"battery-{b}" for b in batteries}

        state = Stage1SweepState(sweep_id=sweep_id, finalists=list(finalists))
        registry = build_default_registry()

        # 7. Resume check.
        already_recorded: RecordedMeasurements = {}
        prior_rounds: dict[tuple[str, str], int] = {}
        if conn is not None:
            already_recorded = _recorded_measurement_keys(conn, sweep_id)
            prior_rounds = _max_round_per_model_battery(conn, sweep_id)
        if already_recorded:
            print(f"Resuming sweep {sweep_id}: skipping {len(already_recorded)} already-recorded measurements.")

        # 8. Run the finals loop.
        try:
            _run_finals_loop(
                adapter=adapter,
                item_repo=item_repo,
                finalists=finalists,
                full_banks=full_banks,
                batteries=batteries,
                battery_ids=battery_ids,
                seq_config=seq_config,
                token_cap=token_cap,
                state=state,
                registry=registry,
                conn=conn,
                sweep_id=sweep_id,
                record_to_db=record_to_db and conn is not None,
                already_recorded=already_recorded,
                prior_rounds=prior_rounds,
            )
        except KeyboardInterrupt:
            if conn is not None:
                print(f"\nSweep interrupted at {state.total_tokens:,} tokens.")
            raise

        if state.stopped_at_cap:
            print(f"\n⚠ Stage 1 halted at {state.total_tokens:,} / {token_cap:,} tokens.")
            print(f"   Reason: {state.stopped_reason}")
        else:
            print(f"\n✓ Stage 1 complete. Total tokens: {state.total_tokens:,} / {token_cap:,}")
            print(f"   Rounds per battery: {state.n_rounds_done}")

        # 9. Compute separation and record.
        sep = _bootstrap_separation_from_stage1(state)
        if conn is not None:
            for battery_code, pairs in sep.items():
                if battery_code not in battery_ids:
                    continue
                b_id = battery_ids[battery_code]
                for p in pairs:
                    _insert_separation(
                        conn,
                        separation_id=f"sep-{uuid.uuid4()}",
                        sweep_id=sweep_id,
                        battery_id=b_id,
                        model_a=p["model_a"],
                        model_b=p["model_b"],
                        p_separated=p["p_separated"],
                        p_weak=p["p_weak"],
                        p_tie=p["p_tie"],
                    )

        print(f"Sweep ID: {sweep_id}")
        _print_matrix(sep)
        return plan, state, selection
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Separation report (--report)
# ---------------------------------------------------------------------------
from hr.stage1_cli import _cli_main, main

if __name__ == "__main__":
    raise SystemExit(main())
