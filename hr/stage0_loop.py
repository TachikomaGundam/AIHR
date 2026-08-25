from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from hr.items.schema import ItemEnvelope
from hr.stage0_call import call_and_grade
from hr.stage0_selection import _AdapterFacade
from hr.stage0_stats import SweepState, _key
from hr.stage0_storage import (
    _insert_infra_incident,
    _insert_measurement,
    _insert_run,
    resolve_scorer_identity,
)

def _run_sweep_loop(
    *,
    adapter: _AdapterFacade,
    item_repo: Path,
    models: tuple[str, ...],
    subsets: dict[str, list[ItemEnvelope]],
    batteries: tuple[str, ...],
    battery_ids: dict[str, str],
    n_initial: int,
    token_cap: int,
    state: SweepState,
    registry,
    conn,
    sweep_id: str,
    record_to_db: bool,
) -> None:
    """Core nested loop: models × batteries × rounds."""
    active_subsets = {b: subsets[b] for b in batteries if b in subsets}
    for model_id in models:
        if state.stopped_at_cap:
            break
        for battery_code, items in active_subsets.items():
            if state.stopped_at_cap:
                break
            b_id = battery_ids[battery_code]
            for round_num in range(1, n_initial + 1):
                if state.stopped_at_cap:
                    state.stopped_reason = (
                        f"Token cap reached during {model_id}/{battery_code}/{round_num}."
                    )
                    break
                round_id = f"run-{uuid.uuid4()}"
                if record_to_db and conn is not None:
                    _insert_run(
                        conn,
                        run_id=round_id,
                        sweep_id=sweep_id,
                        model_id=model_id,
                        battery_id=b_id,
                        round_num=round_num,
                        total_tokens=0,
                        total_cost_cny=0.0,
                        infra_ok=True,
                    )
                round_total_tokens = 0
                round_infra_ok = True
                for rep, env in enumerate(items, start=1):
                    ok, result = call_and_grade(adapter, model_id, env, item_repo, registry)
                    state.total_calls += 1
                    call_tokens = result.tokens_in + result.tokens_out
                    state.total_tokens += call_tokens
                    round_total_tokens += call_tokens
                    if not ok:
                        round_infra_ok = False
                        if result.infra_failure and conn is not None:
                            _insert_infra_incident(
                                conn,
                                round_id,
                                kind=result.infra_failure or "unknown",
                                details=result.detail or {},
                            )
                    if record_to_db and conn is not None:
                        scorer_name, scorer_version = resolve_scorer_identity(
                            env.type.value
                        )
                        _insert_measurement(
                            conn,
                            measurement_id=f"m-{uuid.uuid4()}",
                            run_id=round_id,
                            item_id=env.item_key,
                            repetition=rep,
                            score=result.score,
                            tokens_in=result.tokens_in,
                            tokens_out=result.tokens_out,
                            latency_ms=result.latency_ms,
                            response_text=result.response_text,
                            thinking_text=result.thinking_text,
                            scorer_name=scorer_name,
                            scorer_version=scorer_version,
                        )
                    key_str = _key(model_id, battery_code)
                    per_item = state.measurements_by_model_battery.setdefault(key_str, {})
                    per_item.setdefault(env.item_key, []).append(result.score)

                    if state.total_tokens >= token_cap:
                        state.stopped_at_cap = True
                        state.stopped_reason = (
                            f"Token cap reached at call {state.total_calls} "
                            f"(tokens: {state.total_tokens:,})."
                        )
                        break
                # Update the run row with final token totals.
                if record_to_db and conn is not None:
                    status = "scored" if round_infra_ok else "inconclusive"
                    failure_reason = None if round_infra_ok else "infra_failure_during_execution"
                    with conn.cursor() as cur:
                        cur.execute(
                    "UPDATE hr.run SET finished_at = %s, total_tokens = %s, "
                            "infra_ok = %s, status = %s, failure_reason = %s WHERE run_id = %s",
                            (datetime.now(timezone.utc), round_total_tokens, round_infra_ok, status, failure_reason, round_id),
                        )
                    conn.commit()
                print(
                    f"  [{state.total_calls}] {model_id} / {battery_code} / round {round_num} "
                    f"tokens_this_round={round_total_tokens:,} total={state.total_tokens:,}"
                )


