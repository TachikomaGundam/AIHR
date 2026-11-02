"""preflight executor against the real storage seam (S3 db lane)."""
from __future__ import annotations

import pytest

from hr.bench.engine_results import BenchOutcome
from hr.models import BenchmarkCategory
from hr.preflight import PreflightConfig, marker_age_hours, run_preflight, write_marker

_DB_CFG = PreflightConfig(
    max_requests=50, max_total_tokens=400_000, per_request_max_tokens=4096,
    abort_error_rate=0.4, freshness_hours=12, default_models=(),
    default_batteries=("speed",), bands={},
)


@pytest.mark.db
@pytest.mark.integration
def test_executor_end_to_end_stores_verifies_and_marks(scratch_conn) -> None:
    """Real engine storage seam, real marker rows: one speed cell passes all
    three gates; the freshness marker survives the livebench sweep-row id."""
    from tests.bench.test_engine import MODEL, FakeAdapter, LivebenchEngine

    engine = LivebenchEngine(adapter_factory=lambda model_id: FakeAdapter())
    engine.ensure_registered(scratch_conn)
    calls: list[tuple[str, str]] = []

    class Runner:
        def run_battery(self, model_id: str, battery: BenchmarkCategory) -> BenchOutcome:
            outcome = engine.run_battery(model_id, battery)
            calls.append((model_id, battery.value))
            return outcome

    def store(sweep_id: str, model_id: str, battery_key: str, outcome: BenchOutcome) -> None:
        engine.store(scratch_conn, sweep_id, model_id, BenchmarkCategory(battery_key), outcome)

    def read_back(sweep_id: str) -> int:
        with scratch_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM hr.run WHERE sweep_id = %s", (sweep_id,))
            return int(cur.fetchone()[0])

    markers: list[str] = []
    run = run_preflight(
        Runner(),
        models=[MODEL],
        batteries=["speed"],
        cfg=_DB_CFG,
        store=store,
        read_back=read_back,
        marker_row=lambda sid: (markers.append(sid), write_marker(scratch_conn, sid)),
    )
    assert calls and run.judgment.verdict == "pass"
    assert run.g3_ok and read_back(run.sweep_id) == len(calls)
    assert markers == [run.sweep_id]
    age = marker_age_hours(scratch_conn)
    assert age is not None and age < 1.0


@pytest.mark.db
def test_marker_survives_same_sweep_livebench_row(scratch_conn) -> None:
    from hr.bench.engine_storage import SEAT_CODE
    from hr.stage0_storage import _insert_sweep

    sweep_id = "preflight-conflict-case"
    _insert_sweep(scratch_conn, sweep_id, SEAT_CODE, "livebench")
    write_marker(scratch_conn, sweep_id)
    with scratch_conn.cursor() as cur:
        cur.execute("SELECT purpose FROM hr.sweep WHERE sweep_id = %s", (f"{sweep_id}-pass",))
        assert cur.fetchone()[0] == "preflight-pass"


@pytest.mark.db
def test_latest_sweep_id_skips_marker_rows_on_real_db(scratch_conn) -> None:
    """191 ladder (2026-11-02): the marker is always created after its data
    sweep, so on real Postgres the health/verdict default sweep must never
    land on the empty marker row."""
    from hr.bench.engine_storage import SEAT_CODE
    from hr.decision import latest_sweep_id
    from hr.stage0_storage import _insert_sweep

    _insert_sweep(scratch_conn, "data-before-marker", SEAT_CODE, "livebench")
    write_marker(scratch_conn, "data-before-marker")
    # Determinism over insert-order luck: pin the marker a full hour newer.
    with scratch_conn.cursor() as cur:
        cur.execute(
            "UPDATE hr.sweep SET created_at = created_at + interval '1 hour' "
            "WHERE purpose = 'preflight-pass'"
        )
    assert latest_sweep_id(scratch_conn) == "data-before-marker"
