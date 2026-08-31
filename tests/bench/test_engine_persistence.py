from __future__ import annotations

from tests.bench.test_engine import engine
from tests.bench.test_engine import (
    BenchmarkCategory,
    FakeAdapter,
    LIVEBENCH_BATTERIES,
    LivebenchEngine,
    MODEL,
    _run_all_batteries,
    _sql,
    battery_code,
    battery_item_labels,
    engine,
    engine_mod,
    pytest,
    uuid
)

def test_unknown_model_is_recorded_failed_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(model_id: str):
        raise ValueError(f"no provider type configured for {model_id!r}")

    monkeypatch.setattr(engine_mod, "adapter_for", _boom)
    out = LivebenchEngine().run_battery("bogus/model", BenchmarkCategory.speed)
    assert out.score == 0.0
    assert out.passed is False
    assert "ERROR" in out.raw_output

def test_garbage_adapter_scores_zero_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    garbage = FakeAdapter(garbage=True)
    monkeypatch.setattr(engine_mod, "adapter_for", lambda model_id: garbage)
    engine = LivebenchEngine()
    for battery in LIVEBENCH_BATTERIES:
        out = engine.run_battery(MODEL, battery)
        if battery is BenchmarkCategory.speed:
            # speed measures the response's tok/s tier: garbage still ships
            # tokens/latency, so it scores its tier (90 here) — never a 0.
            assert out.score == pytest.approx(90.0)
            assert out.passed is True
        else:
            assert out.score == 0.0
            assert out.passed is False

@pytest.mark.db
@pytest.mark.integration
@pytest.mark.sandbox
# The whole test is gated on the sandbox probe (see tests/conftest.py): the
# e2e contract asserts exact per-battery means incl. code_gen == 100.0 and
# n_meas == 64, which cannot hold when the sandbox cannot run code_gen at
# all. Skipping the entire sweep on bwrap-incapable environments is the
# minimal-honest option — a conditional assertion around code_gen would
# weaken the persistence contract silently; the other batteries keep their
# own e2e paths (CLI/engine) and locally the test always runs in full.
def test_e2e_all_batteries_write_measurements_with_linkage(
    engine: LivebenchEngine, scratch_conn
) -> None:
    sweep_id = f"qa-bench12-{uuid.uuid4().hex[:8]}"
    engine.ensure_registered(scratch_conn)
    _run_all_batteries(engine, scratch_conn, sweep_id, MODEL)

    # -- battery rows ----------------------------------------------------
    codes = sorted(
        r[0] for r in _sql(scratch_conn, "SELECT battery_code FROM hr.battery")
    )
    expected_codes = sorted(battery_code(b) for b in LIVEBENCH_BATTERIES)
    assert codes == expected_codes

    # -- battery_item counts (13/13/16/1/3/8/4/1/1/4) ----------------------
    rows = _sql(
        scratch_conn,
        """
        SELECT b.battery_code, COUNT(bi.item_id)::int
          FROM hr.battery b
          LEFT JOIN hr.battery_item bi ON bi.battery_id = b.battery_id
         WHERE b.battery_code LIKE 'livebench_%%'
         GROUP BY b.battery_code
        """,
    )
    counts = dict(rows)
    for battery in LIVEBENCH_BATTERIES:
        assert counts[battery_code(battery)] == len(battery_item_labels(battery))

    # -- seat_battery links (honest n_initial/n_max) ----------------------
    links = _sql(
        scratch_conn,
        """
        SELECT b.battery_code, sb.n_initial, sb.n_max
          FROM hr.seat_battery sb
          JOIN hr.battery b ON b.battery_id = sb.battery_id
         WHERE b.battery_code LIKE 'livebench_%%'
         ORDER BY b.battery_code
        """,
    )
    assert len(links) == 10
    for code, n_initial, n_max in links:
        assert n_initial is not None and n_max is not None
        assert 1 <= n_initial <= n_max

    # -- item_pool --------------------------------------------------------
    n_pool = _sql(
        scratch_conn, "SELECT COUNT(*) FROM hr.item_pool WHERE kind = 'livebench'"
    )[0][0]
    assert n_pool == 64

    # -- sweep / runs / measurements --------------------------------------
    assert _sql(
        scratch_conn, "SELECT COUNT(*) FROM hr.sweep WHERE sweep_id = %s", (sweep_id,)
    )[0][0] == 1
    runs = _sql(
        scratch_conn,
        """
        SELECT COUNT(*), COUNT(DISTINCT battery_id)
          FROM hr.run WHERE sweep_id = %s
        """,
        (sweep_id,),
    )[0]
    assert runs == (10, 10)
    n_meas = _sql(
        scratch_conn,
        "SELECT COUNT(*) FROM hr.measurement m JOIN hr.run r ON r.run_id = m.run_id "
        "WHERE r.sweep_id = %s",
        (sweep_id,),
    )[0][0]
    assert n_meas == 13 + 13 + 16 + 1 + 3 + 8 + 4 + 1 + 1 + 4

    # -- per-battery means equal the v1 battery score --------------------
    # (100.0 everywhere except speed: fake responds 2000 tok / 2s -> tier 90)
    expected = {battery_code(b): 100.0 for b in LIVEBENCH_BATTERIES}
    expected[battery_code(BenchmarkCategory.speed)] = 90.0
    means = _sql(
        scratch_conn,
        """
        SELECT b.battery_code, AVG(m.score)::float8
          FROM hr.measurement m
          JOIN hr.run r ON r.run_id = m.run_id
          JOIN hr.battery b ON b.battery_id = r.battery_id
         WHERE r.sweep_id = %s
         GROUP BY b.battery_code
        """,
        (sweep_id,),
    )
    assert dict(means) == expected

    # -- requested_max_output recorded from ChatRequest --------------------
    caps = _sql(
        scratch_conn,
        """
        SELECT DISTINCT b.battery_code, m.requested_max_output
          FROM hr.measurement m
          JOIN hr.run r ON r.run_id = m.run_id
          JOIN hr.battery b ON b.battery_id = r.battery_id
         WHERE r.sweep_id = %s AND m.requested_max_output IS NOT NULL
         ORDER BY b.battery_code
        """,
        (sweep_id,),
    )
    by_code = dict(caps)
    assert by_code[battery_code(BenchmarkCategory.code_gen)] == 32768
    assert by_code[battery_code(BenchmarkCategory.reasoning)] == 32768
    assert by_code[battery_code(BenchmarkCategory.tool_use)] == 4096
    assert by_code[battery_code(BenchmarkCategory.long_context)] == 16384
    assert by_code[battery_code(BenchmarkCategory.attention_probe)] == 16384
    assert by_code[battery_code(BenchmarkCategory.attention_stress)] == 16384
    assert by_code[battery_code(BenchmarkCategory.long_horizon)] == 16384

    # -- response text stored (proof of the mined-output path) -------------
    with_text = _sql(
        scratch_conn,
        """
          SELECT COUNT(*) FROM hr.measurement m
          JOIN hr.run r ON r.run_id = m.run_id
         WHERE r.sweep_id = %s AND m.response_text IS NOT NULL
        """,
        (sweep_id,),
    )[0][0]
    assert with_text == n_meas

@pytest.mark.db
@pytest.mark.integration
def test_e2e_idempotent_registration(scratch_conn) -> None:
    engine = LivebenchEngine()
    engine.ensure_registered(scratch_conn)
    first = _sql(scratch_conn, "SELECT COUNT(*) FROM hr.battery")[0][0]
    engine.ensure_registered(scratch_conn)
    second = _sql(scratch_conn, "SELECT COUNT(*) FROM hr.battery")[0][0]
    assert first == second == 10
