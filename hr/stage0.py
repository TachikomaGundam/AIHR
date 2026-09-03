from __future__ import annotations

import uuid
from pathlib import Path

from hr.config import itemrepo_path
from hr.fleet import fleet_models, provider_display_names
from hr.graders import build_default_registry
from hr.graders.base import GraderRegistry



from hr.stage0_selection import (
    STAGE0_BATTERIES,
    STAGE0_SEAT_CODE,
    STAGE0_SUBSET_SIZES,
    _stage0_token_cap,
    _AdapterFacade,
    select_hallucination_subset,
    select_reasoning_subset,
    select_subsets,
    select_tool_subset,
    select_vision_subset,
)
from hr.stage0_plan import (
    compute_pool_hash,
    CallPlan,
    build_call_plan,
    print_call_plan,
)
from hr.stage0_call import call_and_grade
from hr.stage0_storage import (
    _init_db,
    _connect,
    _upsert_battery,
    _upsert_model,
    _upsert_provider,
    _upsert_seat,
    _upsert_item_pool,
    _upsert_battery_item,
    _upsert_seat_battery,
    _insert_sweep,
    _insert_separation,
    _insert_infra_incident,
    _insert_measurement,
    _insert_run,
)
from hr.stage0_stats import (
    SweepState,
    _key,
    _bootstrap_separation_from_state,
    _print_matrix,
    print_separation_matrix,
)

__all__ = (
    "CallPlan",
    "STAGE0_BATTERIES",
    "STAGE0_SEAT_CODE",
    "STAGE0_SUBSET_SIZES",
    "SweepState",
    "_AdapterFacade",
    "_bootstrap_separation_from_state",
    "_cli_main",
    "_connect",
    "_ensure_provider_model_records",
    "_init_db",
    "_insert_infra_incident",
    "_insert_measurement",
    "_insert_run",
    "_insert_separation",
    "_insert_sweep",
    "_key",
    "_print_matrix",
    "_upsert_battery",
    "_upsert_battery_item",
    "_upsert_item_pool",
    "_upsert_model",
    "_upsert_provider",
    "_upsert_seat",
    "_upsert_seat_battery",
    "build_call_plan",
    "call_and_grade",
    "compute_pool_hash",
    "print_call_plan",
    "print_separation_matrix",
    "run_sweep",
    "select_hallucination_subset",
    "select_reasoning_subset",
    "select_subsets",
    "select_tool_subset",
    "select_vision_subset",
)


def _ensure_provider_model_records(conn, models: tuple[str, ...]) -> dict[str, str]:
    provider_names = provider_display_names()
    model_to_provider: dict[str, str] = {}
    for model_id in models:
        if "/" in model_id:
            provider_id, slug = model_id.split("/", 1)
        else:
            provider_id, slug = "unknown", model_id
        provider_names.setdefault(provider_id, provider_id)
        _upsert_provider(conn, provider_id, provider_names[provider_id])
        _upsert_model(conn, model_id, provider_id, slug)
        model_to_provider[model_id] = provider_id
    return model_to_provider

def run_sweep(
    adapter: _AdapterFacade,
    item_repo: Path | None = None,
    models: tuple[str, ...] | None = None,
    batteries: tuple[str, ...] = STAGE0_BATTERIES,
    *,
    n_initial: int = 3,
    token_cap: int | None = None,
    dry_run: bool = False,
    init_db: bool = True,
    record_to_db: bool = True,
    registry: GraderRegistry | None = None,
) -> tuple[CallPlan, SweepState | None]:
    """Run the Stage 0 sweep.

    Returns ``(plan, state)`` where ``state`` is ``None`` only on ``dry_run``.
    ``item_repo`` defaults to :func:`hr.config.itemrepo_path` (HR_ITEMREPO
    env or HR_HOME/itemrepo). ``registry`` may be injected (tests supply a
    stub to avoid spawning real grader subprocesses); defaults to
    :func:`build_default_registry`. ``token_cap`` None resolves the config
    value (``stage0.token_cap``).
    """
    if item_repo is None:
        item_repo = itemrepo_path()
    if token_cap is None:
        token_cap = _stage0_token_cap()

    # 1. Load items + select subsets.
    from hr.calibrate import load_item_repo

    if models is None:
        models = fleet_models()

    item_bundles = load_item_repo(item_repo, batteries=list(batteries))
    # load_item_repo returns dict[battery, list[ItemEnvelope]]
    # But the hallucination battery groups FACTUALITY_QA+UNANSWERABLE+CITATION
    # under one key — we need to flatten for subset selection.
    subsets = select_subsets(item_bundles)

    plan = build_call_plan(subsets, models=models, n_initial=n_initial, budget_cap=token_cap)
    if dry_run:
        print_call_plan(plan)
        return plan, None

    pool_hash = compute_pool_hash(subsets)

    # 2. Init DB.
    if init_db or record_to_db:
        _init_db()
        conn = _connect()
    else:
        conn = None

    try:
        # 3. Upsert reference rows.
        if conn is not None:
            _upsert_seat(conn, STAGE0_SEAT_CODE, "Stage 0 full-pool sweep")
            _ensure_provider_model_records(conn, models)
            battery_ids: dict[str, str] = {}
            for bcode in batteries:
                battery_ids[bcode] = _upsert_battery(conn, bcode, f"Stage-0 {bcode} battery")
            for bcode in batteries:
                b_id = battery_ids[bcode]
                _upsert_seat_battery(conn, STAGE0_SEAT_CODE, b_id)
                for pos, env in enumerate(subsets.get(bcode, [])):
                    _upsert_item_pool(conn, env)
                    _upsert_battery_item(conn, b_id, env.item_key, pos)

            # 4. Create sweep.
            sweep_id = f"stage0-{uuid.uuid4()}"
            purpose = (
                f"Stage 0 full-pool sweep\n"
                f"pool_hash: {pool_hash}\n"
                f"models: {len(models)}\n"
                f"n_initial: {n_initial}\n"
                f"token_cap: {token_cap}\n"
                f"subsets: { {b: len(items) for b, items in subsets.items()} }"
            )
            _insert_sweep(conn, sweep_id, STAGE0_SEAT_CODE, purpose)
        else:
            sweep_id = f"stage0-{uuid.uuid4()}"
            battery_ids = {b: f"battery-{b}" for b in batteries}

        state = SweepState(sweep_id=sweep_id)
        registry = registry or build_default_registry()

        # 5. Run sweep.
        try:
            _run_sweep_loop(
                adapter=adapter,
                item_repo=item_repo,
                models=models,
                subsets=subsets,
                batteries=batteries,
                battery_ids=battery_ids,
                n_initial=n_initial,
                token_cap=token_cap,
                state=state,
                registry=registry,
                conn=conn,
                sweep_id=sweep_id,
                record_to_db=record_to_db and conn is not None,
            )
        except KeyboardInterrupt:
            if conn is not None:
                print(f"\nSweep interrupted at {state.total_tokens:,} tokens.")
            raise

        if state.stopped_at_cap:
            print(f"\n⚠ Stage 0 halted at {state.total_tokens:,} / {token_cap:,} tokens.")
            print(f"Reason: {state.stopped_reason}")
        else:
            print(f"\n✓ Stage 0 complete. Total tokens: {state.total_tokens:,} / {token_cap:,}")

        # 6. Compute separation and record.
        sep = _bootstrap_separation_from_state(state)
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
        print(f"Pool hash: {pool_hash}")
        _print_matrix(sep)
        return plan, state
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass



from hr.stage0_loop import _run_sweep_loop

from hr.stage0_cli import _cli_main, main

if __name__ == "__main__":
    raise SystemExit(main())
