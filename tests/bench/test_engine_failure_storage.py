from __future__ import annotations

from tests.bench.test_engine import engine
from tests.bench.test_engine import (
    BenchmarkCategory,
    FakeAdapter,
    LivebenchEngine,
    MODEL,
    PERFECT_INSTRUCTION_JSON,
    _run_all_batteries,
    _sql,
    engine,
    engine_mod,
    pytest,
    uuid
)

@pytest.mark.db
@pytest.mark.integration
def test_e2e_garbage_adapter_records_failed_run_not_crash(
    monkeypatch: pytest.MonkeyPatch, scratch_conn
) -> None:
    monkeypatch.setattr(
        engine_mod, "adapter_for", lambda model_id: FakeAdapter(garbage=True)
    )
    sweep_id = f"qa-bench12-garbage-{uuid.uuid4().hex[:8]}"
    engine = LivebenchEngine()
    engine.ensure_registered(scratch_conn)
    _run_all_batteries(engine, scratch_conn, sweep_id, MODEL)
    rows = _sql(
        scratch_conn,
        """
        SELECT b.battery_code, COUNT(m.measurement_id)::int, AVG(m.score)::float8
          FROM hr.measurement m
          JOIN hr.run r ON r.run_id = m.run_id
          JOIN hr.battery b ON b.battery_id = r.battery_id
         WHERE r.sweep_id = %s
         GROUP BY b.battery_code
        """,
        (sweep_id,),
    )
    assert len(rows) == 10  # all 10 batteries recorded
    for code, count, mean in rows:
        assert count >= 1
        if code == "livebench_speed":
            assert mean == pytest.approx(90.0)  # garbage still has tokens/latency
        else:
            assert mean == 0.0

@pytest.mark.db
@pytest.mark.integration
def test_store_writes_expected_response_text(engine: LivebenchEngine, scratch_conn) -> None:
    sweep_id = f"qa-bench12-text-{uuid.uuid4().hex[:8]}"
    engine.ensure_registered(scratch_conn)
    out = engine.run_battery(MODEL, BenchmarkCategory.instruction_follow)
    engine.store(scratch_conn, sweep_id, MODEL, BenchmarkCategory.instruction_follow, out)
    texts = _sql(
        scratch_conn,
        """
        SELECT m.response_text, m.thinking_text
          FROM hr.measurement m
          JOIN hr.run r ON r.run_id = m.run_id
         WHERE r.sweep_id = %s
        """,
        (sweep_id,),
    )
    assert len(texts) == 16
    assert all(t == PERFECT_INSTRUCTION_JSON for t, _th in texts)
    # thinking_budget None -> no thinking stored (NULL or empty string)
    assert all(th is None or th == "" for _t, th in texts)
