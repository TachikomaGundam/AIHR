from __future__ import annotations

from tests.test_cli import (
    _AVG_SQL_KEY,
    _BATTERY_SQL_KEY,
    _COUNT_SQL_KEY,
    _DISTINCT_MODEL_SQL_KEY,
    _KeyedConn,
    _LATEST_SQL_KEY,
    _MEASUREMENT_SQL_KEY,
    _MODEL_SQL_KEY,
    _SEAT_SQL_KEY,
    _SWEEPS_SQL_KEY,
    _health_router,
    app,
    build_health_report,
    build_status_report,
    runner
)

def test_verdict_latest_flag_uses_latest_sweep(monkeypatch):
    router = {
        _LATEST_SQL_KEY: [("s9",)],
        _AVG_SQL_KEY: [],
        _DISTINCT_MODEL_SQL_KEY: [],
        _MEASUREMENT_SQL_KEY: [],
        _COUNT_SQL_KEY: [(0,)],
        _SEAT_SQL_KEY: [],
        _MODEL_SQL_KEY: [],
        _BATTERY_SQL_KEY: [("hallucination",), ("reasoning",), ("tool_a",), ("vision",)],
    }
    monkeypatch.setattr("hr.cli.connect", lambda: _KeyedConn(router))
    monkeypatch.setattr("hr.cli.load_deployable", lambda: set())
    result = runner.invoke(app, ["verdict", "--latest"])
    assert result.exit_code == 0
    assert "# Verdict — sweep s9" in result.output

def test_verdict_ranks_with_fake_conn(monkeypatch):
    router = {
        _AVG_SQL_KEY: [("m_a", "reasoning", 0.9), ("m_b", "reasoning", 0.2)],
        _DISTINCT_MODEL_SQL_KEY: [("m_a",), ("m_b",)],
        _MEASUREMENT_SQL_KEY: [("i1", 0.9, 100, "结论: 42.")],
        _COUNT_SQL_KEY: [(4,)],
        _SEAT_SQL_KEY: [],
        _MODEL_SQL_KEY: [],
        _BATTERY_SQL_KEY: [("hallucination",), ("reasoning",), ("tool_a",), ("vision",)],
    }
    monkeypatch.setattr("hr.cli.connect", lambda: _KeyedConn(router))
    monkeypatch.setattr("hr.cli.load_deployable", lambda: {"m_a", "m_b"})
    result = runner.invoke(app, ["verdict", "--sweep", "s1"])
    assert result.exit_code == 0
    assert "| oracle | strict |" in result.output
    assert "| oracle | strict | m_a |" in result.output

def test_verdict_retired_excluded_from_assignment(monkeypatch):
    # m_dead is in the sweep but not deployable: shown (tagged) in capability
    # and health tables and listed in the retired section, but never assigned.
    router = {
        _AVG_SQL_KEY: [("m_dead", "reasoning", 0.9), ("m_alive", "reasoning", 0.2)],
        _DISTINCT_MODEL_SQL_KEY: [("m_dead",), ("m_alive",)],
        _MEASUREMENT_SQL_KEY: [("i1", 0.9, 100, "结论: 42.")],
        _COUNT_SQL_KEY: [(4,)],
        _SEAT_SQL_KEY: [],
        _MODEL_SQL_KEY: [],
        _BATTERY_SQL_KEY: [("hallucination",), ("reasoning",), ("tool_a",), ("vision",)],
    }
    monkeypatch.setattr("hr.cli.connect", lambda: _KeyedConn(router))
    monkeypatch.setattr("hr.cli.load_deployable", lambda: {"m_alive"})
    result = runner.invoke(app, ["verdict", "--sweep", "s1"])
    assert result.exit_code == 0
    assert "## Retired models (excluded from assignment)" in result.output
    assert "| m_dead ⚠ retired |" in result.output        # tagged in capability table
    assert "m_dead" in result.output                      # present in retired section
    assert "| oracle | strict | m_alive |" in result.output  # survivor is the primary
    assert "m_dead" not in result.output.split("## Recommended seat assignment")[1]

def test_verdict_include_retired_assigns_with_tag(monkeypatch):
    router = {
        _AVG_SQL_KEY: [("m_dead", "reasoning", 0.9), ("m_alive", "reasoning", 0.2)],
        _DISTINCT_MODEL_SQL_KEY: [("m_dead",), ("m_alive",)],
        _MEASUREMENT_SQL_KEY: [("i1", 0.9, 100, "结论: 42.")],
        _COUNT_SQL_KEY: [(4,)],
        _SEAT_SQL_KEY: [],
        _MODEL_SQL_KEY: [],
        _BATTERY_SQL_KEY: [("hallucination",), ("reasoning",), ("tool_a",), ("vision",)],
    }
    monkeypatch.setattr("hr.cli.connect", lambda: _KeyedConn(router))
    monkeypatch.setattr("hr.cli.load_deployable", lambda: {"m_alive"})
    result = runner.invoke(app, ["verdict", "--sweep", "s1", "--include-retired"])
    assert result.exit_code == 0
    assert "| oracle | strict | m_dead ⚠ |" in result.output

def test_status_dispatch_via_fake_conn(monkeypatch):
    from datetime import datetime

    rows = [
        ("stage1-x", datetime(2026, 1, 2), 156, 13, 8288, 5513),
    ]
    router = {
        _LATEST_SQL_KEY: [("stage1-x",)],
        _SWEEPS_SQL_KEY: rows,
        _AVG_SQL_KEY: [("m_a", "reasoning", 0.9)],
        _DISTINCT_MODEL_SQL_KEY: [("m_a",)],
        _MEASUREMENT_SQL_KEY: [("i1", 0.9, 100, "结论: 42.")],
        _COUNT_SQL_KEY: [(1,)],
        _SEAT_SQL_KEY: [],
        _MODEL_SQL_KEY: [],
        _BATTERY_SQL_KEY: [("reasoning",)],
    }
    monkeypatch.setattr("hr.cli.connect", lambda: _KeyedConn(router))
    monkeypatch.setattr("hr.cli.load_deployable", lambda: {"m_a"})
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "# Status — latest sweep stage1-x" in result.output
    assert "| m_a | 0.900 |" in result.output

def test_status_dispatch_marks_retired_models(monkeypatch):
    from datetime import datetime

    rows = [
        ("stage1-x", datetime(2026, 1, 2), 156, 13, 8288, 5513),
    ]
    router = {
        _LATEST_SQL_KEY: [("stage1-x",)],
        _SWEEPS_SQL_KEY: rows,
        _AVG_SQL_KEY: [("m_dead", "reasoning", 0.9)],
        _DISTINCT_MODEL_SQL_KEY: [("m_dead",)],
        _MEASUREMENT_SQL_KEY: [("i1", 0.9, 100, "结论: 42.")],
        _COUNT_SQL_KEY: [(1,)],
        _SEAT_SQL_KEY: [],
        _MODEL_SQL_KEY: [],
        _BATTERY_SQL_KEY: [("reasoning",)],
    }
    monkeypatch.setattr("hr.cli.connect", lambda: _KeyedConn(router))
    monkeypatch.setattr("hr.cli.load_deployable", lambda: set())
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "| m_dead ⚠ retired |" in result.output

def test_status_empty_db_error_surfaces_cleanly(monkeypatch):
    monkeypatch.setattr("hr.cli.connect", lambda: _KeyedConn({}))
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1
    assert "error: no sweeps found" in result.output

def test_build_health_report_passes_cap_through():
    rows = [
        ("i1", 0.8, 16000, "结论: 42."),
    ]
    conn = _KeyedConn(_health_router(rows))
    out = build_health_report(conn, "s1", cap=16000)
    assert "| m1 | 1 |" in out

def test_build_status_report_function_smoke():
    """build_status_report is the status command's engine — also usable directly."""
    from datetime import datetime

    router = {
        _LATEST_SQL_KEY: [("s0",)],
        _SWEEPS_SQL_KEY: [("s0", datetime(2026, 1, 1), 1, 1, 1, 1)],
        _AVG_SQL_KEY: [],
        _DISTINCT_MODEL_SQL_KEY: [],
        _MEASUREMENT_SQL_KEY: [],
        _COUNT_SQL_KEY: [(0,)],
        _SEAT_SQL_KEY: [],
        _MODEL_SQL_KEY: [],
        _BATTERY_SQL_KEY: [("reasoning",)],
    }
    import hr.cli as cli_mod

    orig = cli_mod.load_deployable
    cli_mod.load_deployable = lambda: set()
    try:
        out = build_status_report(_KeyedConn(router))
    finally:
        cli_mod.load_deployable = orig
    assert "# Status — latest sweep s0" in out
