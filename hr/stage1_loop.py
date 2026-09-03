
from __future__ import annotations

import uuid
from itertools import combinations
from datetime import datetime, timezone
from pathlib import Path


from hr.items.schema import ItemEnvelope
from hr.stats.sequential import SequentialConfig, SequentialStopper, normalize_bounded_score

# Reuse stage0's helpers and DB plumbing.
from hr.stage0 import (
    _AdapterFacade,
    _insert_infra_incident,
    _insert_measurement,
    _insert_run,
    _key,
    call_and_grade,
)
from hr.stage1_state import (
    STAGE1_STATE_VERSION,
    RecordedMeasurements,
    Stage1SweepState,
    _ensure_sweep_state_stamped,
    _resume_requires_restart,
    _sweep_state_version,
    make_pair_sequence,
)
from hr.stage1_resume import _rebuild_stopper_from_db


def _pair_key(model_a: str, model_b: str, battery: str) -> str:
    return f"{model_a}|{model_b}|{battery}"

#: Stage-1 scores come from GradeResult (hr/graders/base.py), scale [0, 1].
#: Normalization is explicit here so the paired differences entering the
#: anytime-valid sequence are always in [-1, 1] regardless of the grader's
#: raw scale (bench ItemResult scores are 0-100 and use max_score=100.0).
STAGE1_SCORE_SCALE: float = 1.0

def _run_finals_loop(
    *,
    adapter: _AdapterFacade,
    item_repo: Path,
    finalists: list[str],
    full_banks: dict[str, list[ItemEnvelope]],
    batteries: tuple[str, ...],
    battery_ids: dict[str, str],
    seq_config: SequentialConfig,
    token_cap: int,
    state: Stage1SweepState,
    registry,
    conn,
    sweep_id: str,
    record_to_db: bool,
    already_recorded: RecordedMeasurements,
    prior_rounds: dict[tuple[str, str], int],
) -> None:
    """Sequential-n loop with per-battery independent progression.

    Each battery tracks its own round counter. At each iteration of the outer
    loop we round-robin over batteries: for each active (non-stopped, below
    n_max) battery, we run ONE round for ALL finalists, then update the
    battery's SequentialStopper and check `should_stop`.

    Resume support: if `already_recorded` is non-empty (sweep_id already has
    measurements in DB), we seed `n_rounds_done[battery]` from the max recorded
    round per (model, battery) in the DB and only run subsequent rounds.

    Infra-failed and unscored (no-routing / grader-error) calls are not
    score-bearing observations: they produce no measurement row, no in-memory
    measurement, and no per-round score, and an all-unscored round changes no
    stopper state. Historical contaminated rows remain untouched pending a
    separate cleanup decision (audit HANDOFF-2026-09-02 C7).
    """
    # Initialize stoppers per battery.
    for battery in batteries:
        state.stoppers[battery] = SequentialStopper(
            battery_code=battery, config=seq_config
        )
        for model_id in finalists:
            state.model_stoppers[_key(model_id, battery)] = SequentialStopper(
                battery_code=battery, config=seq_config
            )
        for model_a, model_b in combinations(finalists, 2):
            state.pair_stoppers[_pair_key(model_a, model_b, battery)] = make_pair_sequence(
                battery, seq_config, len(finalists)
            )
        state.n_rounds_done[battery] = 0

    # Resume: seed state from DB. In-progress rounds recorded under an older
    # state version (legacy bootstrap-CI decision regime) are explicitly
    # invalidated and the sweep is restarted — legacy rounds are never
    # silently reinterpreted by the anytime-valid machinery.
    if conn is not None:
        _stored_version = _sweep_state_version(conn, sweep_id)
        if already_recorded and _resume_requires_restart(_stored_version):
            print(
                f"⚠ Sweep {sweep_id} holds in-progress rounds from an older "
                f"decision regime (state_version={_stored_version!r} != "
                f"{STAGE1_STATE_VERSION}); invalidating and restarting — "
                f"legacy rounds are never silently reinterpreted."
            )
            already_recorded = {}
            prior_rounds = {}
        _ensure_sweep_state_stamped(conn, sweep_id)
    if already_recorded and conn is not None:
        _rebuild_stopper_from_db(
            state,
            conn,
            sweep_id,
            batteries,
            seq_config,
            expected_measurements={
                battery: len(full_banks.get(battery, [])) * len(finalists)
                for battery in batteries
            },
        )

    # Determine each battery's starting round (resume-aware).
    next_round: dict[str, int] = {}
    for battery in batteries:
        # Resume: start at the max recorded round (across all finalists) + 1.
        # (We assume all finalists in a battery are at the same round in a resume
        # because we always run all finalists for a battery round together.)
        prior_max = 0
        for model_id in finalists:
            prior_max = max(prior_max, prior_rounds.get((model_id, battery), 0))
        next_round[battery] = prior_max + 1
        items = full_banks.get(battery, [])
        for round_num in range(1, prior_max + 1):
            expected = (
                (model_id, battery, round_num, item.item_key, repetition)
                for model_id in finalists
                for repetition, item in enumerate(items, start=1)
            )
            if any(key not in already_recorded for key in expected):
                next_round[battery] = round_num
                break

    # Main loop: while any battery is still active, run one round of that battery.
    while True:
        any_battery_still_active = False
        for battery in batteries:
            if state.stopped_at_cap:
                break
            round_num = next_round[battery]
            # Stop conditions for this battery:
            if round_num > seq_config.n_max:
                continue
            pair_keys = [
                _pair_key(model_a, model_b, battery)
                for model_a, model_b in combinations(finalists, 2)
            ]
            if pair_keys:
                # Every finalist pair resolved (decided or budget-exhausted
                # unresolvable) -> the battery is done. Any pair still
                # indeterminate keeps the battery running within the budget.
                if all(
                    state.pair_stoppers[k].is_resolved() for k in pair_keys
                ):
                    continue
            elif round_num > seq_config.n_initial and all(
                state.model_stoppers[_key(model_id, battery)].should_stop()
                for model_id in finalists
            ):
                # Single finalist: no pairs to resolve; fall back to the
                # per-model precision rule.
                continue
            any_battery_still_active = True

            items = full_banks.get(battery, [])
            if not items:
                next_round[battery] = round_num + 1
                continue

            # Run round_num for ALL finalists in this battery.
            round_scores_for_stopper: list[float] = []
            complete_scores_by_model: dict[str, list[float]] = {}
            complete_score_keys_by_model: dict[str, list[tuple[str, int]]] = {}
            for model_id in finalists:
                if state.stopped_at_cap:
                    break
                b_id = battery_ids[battery]
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
                round_unscored = False
                mb_key = _key(model_id, battery)
                state.measurements_by_model_battery.setdefault(mb_key, {})
                round_scores_for_model: list[float] = []
                round_score_keys: list[tuple[str, int]] = []
                for rep, env in enumerate(items, start=1):
                    recorded_score = already_recorded.get(
                        (model_id, battery, round_num, env.item_key, rep)
                    )
                    if recorded_score is not None:
                        round_scores_for_model.append(recorded_score)
                        round_score_keys.append((env.item_key, rep))
                        continue
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
                    unscored = (not ok) or (not result.scored)
                    if unscored:
                        round_unscored = True
                    else:
                        if record_to_db and conn is not None:
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
                            )
                        state.measurements_by_model_battery[mb_key].setdefault(
                            env.item_key, []
                        ).append(result.score)
                        round_scores_for_model.append(result.score)
                        round_score_keys.append((env.item_key, rep))

                    # Token cap check.
                    if state.total_tokens >= token_cap:
                        state.stopped_at_cap = True
                        state.stopped_reason = (
                            f"Token cap {state.total_tokens:,} / {token_cap:,} reached "
                            f"during {model_id}/{battery}/{round_num}."
                        )
                        break
                # Update run row with total tokens.
                if record_to_db and conn is not None:
                    status = (
                        "scored"
                        if (round_infra_ok and not round_unscored)
                        else "inconclusive"
                    )
                    if not round_infra_ok:
                        failure_reason = "infra_failure_during_execution"
                    elif round_unscored:
                        failure_reason = "unscored_call_during_execution"
                    else:
                        failure_reason = None
                    with conn.cursor() as cur:
                        cur.execute(
                "UPDATE hr.run SET total_tokens = %s, infra_ok = %s, "
                            "finished_at = %s, status = %s, failure_reason = %s WHERE run_id = %s",
                            (
                                round_total_tokens,
                                round_infra_ok,
                                datetime.now(timezone.utc),
                                status,
                                failure_reason,
                                round_id,
                            ),
                        )
                    conn.commit()
                # Aggregate this model's round scores into battery-wide stopper bucket.
                # An empty round (all calls unscored) is not score-bearing and
                # must not advance any stopper (SequentialStopper.add_round([])
                # bumps n_rounds unconditionally).
                if round_scores_for_model:
                    round_scores_for_stopper.extend(round_scores_for_model)
                    state.model_stoppers[mb_key].add_round(round_scores_for_model)
                    complete_scores_by_model[model_id] = round_scores_for_model
                    complete_score_keys_by_model[model_id] = round_score_keys

                if state.stopped_at_cap:
                    break

            # After this round for all finalists, push the aggregate scores to the stopper.
            if round_scores_for_stopper:
                state.stoppers[battery].add_round(round_scores_for_stopper)
            if not state.stopped_at_cap:
                for model_a, model_b in combinations(finalists, 2):
                    keys_a = complete_score_keys_by_model.get(model_a)
                    keys_b = complete_score_keys_by_model.get(model_b)
                    if not keys_a or not keys_b:
                        # A model produced no scored observation this round:
                        # never feed a partial round into the pair sequence.
                        continue
                    shared = sorted(set(keys_a) & set(keys_b))
                    if not shared:
                        # Disjoint item sets: there is no shared (item_key, rep)
                        # to pair positionally — skip, sequence state unchanged.
                        continue
                    scores_a = dict(zip(complete_score_keys_by_model[model_a], complete_scores_by_model[model_a]))
                    scores_b = dict(zip(complete_score_keys_by_model[model_b], complete_scores_by_model[model_b]))
                    normalized_diffs = [
                        normalize_bounded_score(scores_a[key], max_score=STAGE1_SCORE_SCALE)
                        - normalize_bounded_score(scores_b[key], max_score=STAGE1_SCORE_SCALE)
                        for key in shared
                    ]
                    state.pair_stoppers[_pair_key(model_a, model_b, battery)].add_round(
                        normalized_diffs
                    )
            state.n_rounds_done[battery] = state.stoppers[battery].n_rounds
            next_round[battery] = round_num + 1

            if state.stopped_at_cap:
                break

        if state.stopped_at_cap:
            break
        if not any_battery_still_active:
            break
