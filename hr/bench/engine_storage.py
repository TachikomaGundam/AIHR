from __future__ import annotations

import uuid
import json

from hr.bench.manifest import ExperimentManifest
from hr.bench.engine_results import BenchOutcome
from hr.graders.base import GRADER_VERSION
from hr.bench.livebench import LIVEBENCH_BATTERIES, battery_code, battery_description, battery_item_id, battery_item_labels, seat_battery_bounds
from hr.models import BenchmarkCategory

SEAT_CODE = "_stage0_sweep"

class EngineStorageMixin:
    @staticmethod
    def store_manifest(conn, sweep_id: str, manifest: ExperimentManifest) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO hr.experiment_manifest (sweep_id, manifest_json, digest) "
                "VALUES (%s, %s::jsonb, %s) ON CONFLICT (sweep_id) DO NOTHING",
                (sweep_id, json.dumps(manifest.payload), manifest.digest),
            )
        conn.commit()

    def ensure_registered(self, conn) -> None:
        """Upsert batteries/items/seat links for all 10 livebench batteries.

        Uses the same stage0 upsert helpers as scripts/register_tool_b_battery.py
        and the CLI, so registration is idempotent on any DB (ON CONFLICT DO
        NOTHING) and self-heals FK prerequisites (the seat row is upserted
        first, exactly like the tool_b script learned to do).
        """
        from hr.stage0 import (
            _upsert_battery,
            _upsert_battery_item,
            _upsert_seat,
            _upsert_seat_battery,
        )

        _upsert_seat(conn, SEAT_CODE, "Stage-0 sweep")
        for battery in LIVEBENCH_BATTERIES:
            battery_id = _upsert_battery(
                conn, battery_code(battery), battery_description(battery)
            )
            for pos, label in enumerate(battery_item_labels(battery)):
                item_id = battery_item_id(battery, label)
                self._upsert_livebench_item(conn, item_id, battery_code(battery))
                _upsert_battery_item(conn, battery_id, item_id, pos)
            n_initial, n_max = seat_battery_bounds(battery)
            _upsert_seat_battery(conn, SEAT_CODE, battery_id, n_initial, n_max)

    @staticmethod
    def _upsert_livebench_item(conn, item_id: str, kind: str) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO hr.item_pool (item_id, item_code, version, domain, "
                "kind, json_meta) VALUES (%s, %s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (item_id) DO NOTHING",
                (item_id, item_id, "v1", kind, "livebench",
                 '{"kind": "livebench"}'),
            )
        conn.commit()
    def store(
        self,
        conn,
        sweep_id: str,
        model_id: str,
        battery: BenchmarkCategory,
        outcome: BenchOutcome,
    ) -> None:
        """Write one run + per-item measurements under ``sweep_id``.

        Provider/model rows are upserted first (bench runs must work without
        a prior ``hr discover``), then the sweep, then the run, then one
        measurement row per graded item with the ACTUAL requested_max_output.
        """
        from hr.stage0 import (
            _insert_measurement,
            _insert_run,
            _insert_sweep,
            _upsert_model,
            _upsert_provider,
        )

        provider = model_id.split("/", 1)[0] if "/" in model_id else model_id
        _upsert_provider(conn, provider, provider)
        _upsert_model(conn, model_id, provider, model_id)
        _insert_sweep(conn, sweep_id, SEAT_CODE, "livebench")

        run_id = f"run-{uuid.uuid4().hex}"
        battery_id = f"battery-{battery_code(battery)}"
        failure_reason = None
        if outcome.status == "inconclusive":
            failure_reason = "adapter_setup_failure"
        elif outcome.status == "not_applicable":
            failure_reason = "capability_not_supported"
        _insert_run(
            conn,
            run_id,
            sweep_id,
            model_id,
            battery_id,
            1,
            outcome.tokens_in + outcome.tokens_out,
            0.0,
            outcome.status == "scored",
            status=outcome.status,
            failure_reason=failure_reason,
        )
        # An unavailable capability or failed transport is not a zero-score
        # observation. Keep the run as audit evidence, but do not inject fake
        # failures into the score distribution used for selection.
        if outcome.status != "scored":
            return
        for item in outcome.items:
            _insert_measurement(
                conn,
                f"meas-{uuid.uuid4().hex}",
                run_id,
                item.item_id,
                1,
                item.score,
                outcome.tokens_in,
                outcome.tokens_out,
                outcome.latency_ms,
                response_text=outcome.response_text or None,
                thinking_text=outcome.thinking_text or None,
                requested_max_output=outcome.requested_max_output,
                scorer_name=f"livebench:{battery_code(battery)}",
                scorer_version=GRADER_VERSION,
            )


__all__ = ["EngineStorageMixin", "SEAT_CODE"]
