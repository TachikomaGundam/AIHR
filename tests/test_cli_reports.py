from __future__ import annotations

from tests.test_cli import (
    COMMANDS,
    HealthReport,
    _AVG_SQL_KEY,
    _BATTERY_KEY,
    _BATTERY_SQL_KEY,
    _COUNT_SQL_KEY,
    _DISTINCT_MODEL_SQL_KEY,
    _FakeConn,
    _KeyedConn,
    _MEASUREMENT_SQL_KEY,
    _MODEL_SQL_KEY,
    _SEAT_SQL_KEY,
    _health_router,
    app,
    build_sweeps_report,
    latest_sweep_id,
    pytest,
    runner,
    summary_table
)

class TestTyperWiring:
    def test_app_has_exactly_13_commands(self):
        from typer.main import get_command

        cmd = get_command(app)
        assert set(cmd.commands) == COMMANDS

    def test_retired_v1_commands_absent(self):
        from typer.main import get_command

        cmd = get_command(app)
        assert not (set(cmd.commands) & {"evaluate", "report", "run_all", "benchmark"})

    def test_no_argparse_framework_remains_in_cli_module(self):
        import inspect

        from hr import cli

        # The MUST-NOT-DO is the argparse *framework* (import/parser API) in
        # the CLI entry path — the docstring may mention the word.
        src = inspect.getsource(cli)
        assert "import argparse" not in src
        assert "ArgumentParser" not in src
        assert "parse_args(" not in src

    def test_help_lists_all_13_commands(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        section = result.output.split("╭─ Commands")[1].split("╰─")[0]
        for name in sorted(COMMANDS):
            assert name in section

    def test_help_epilog_notes_retirements(self):
        result = runner.invoke(app, ["--help"])
        assert "evaluate/report/run_all retired" in result.output
        assert "verdict supersedes" in result.output

    def test_no_args_shows_help(self):
        # typer 0.27 no_args_is_help prints the help and exits 2 — matches the
        # real console script (`hr` with no arguments, verified manually).
        result = runner.invoke(app, [])
        assert result.exit_code == 2
        assert "Usage:" in result.output

    def test_unknown_command_rejected(self):
        result = runner.invoke(app, ["frobnicate"])
        assert result.exit_code != 0
        assert "No such command" in result.output

    def test_retired_evaluate_rejected(self):
        result = runner.invoke(app, ["evaluate"])
        assert result.exit_code != 0
        assert "No such command" in result.output

class TestSweepSelection:
    @pytest.mark.parametrize("command", ["health", "verdict"])
    def test_sweep_and_latest_mutually_exclusive(self, command):
        result = runner.invoke(app, [command, "--sweep", "s1", "--latest"])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

class TestSummaryTable:
    def test_rows_sorted_by_model_id(self):
        reports = {
            "zeta": HealthReport(model_id="zeta", sweep_id="s", n_measurements=2),
            "alpha": HealthReport(model_id="alpha", sweep_id="s", n_measurements=1),
        }
        table = summary_table(reports)
        assert table.index("| alpha ") < table.index("| zeta ")

    def test_none_metrics_render_dash(self):
        hr = HealthReport(model_id="m", sweep_id="s", n_measurements=0)
        table = summary_table({"m": hr})
        assert "—" in table
        assert "| m | 0 |" in table

    def test_populated_metrics_render_values(self):
        hr = HealthReport(
            model_id="m", sweep_id="s", n_measurements=4,
            loop_mean=0.08, loop_max=0.10,
            truncation_rate=0.0126, token_efficiency=1154.4,
            consistency_mean_range=0.01, consistency_unanimity_pct=0.90,
            answer_completion_rate=0.75,
        )
        table = summary_table({"m": hr})
        assert "0.080" in table
        assert "1.3%" in table          # truncation_rate 0.0126 → 1.3%
        assert "1154.4" in table
        assert "90.0%" in table
        assert "75.0%" in table

def test_build_sweeps_report_with_fake_conn():
    from datetime import datetime

    rows = [
        ("stage1-x", datetime(2026, 1, 2), 156, 13, 8288, 5513),
        ("stage0-y", datetime(2026, 1, 1), 24, 2, 540, 0),
    ]
    report = build_sweeps_report(_FakeConn(rows))
    assert "stage1-x" in report and "stage0-y" in report
    assert "stage1-x (largest, latest)" in report

def test_latest_sweep_id_picks_most_measurements():
    conn = _FakeConn([("stage1-x",)])
    assert latest_sweep_id(conn) == "stage1-x"

def test_latest_sweep_id_no_rows_raises():
    with pytest.raises(ValueError):
        latest_sweep_id(_FakeConn([]))

def test_sweeps_dispatch_via_fake_conn(monkeypatch):
    from datetime import datetime

    rows = [
        ("stage1-x", datetime(2026, 1, 2), 156, 13, 8288, 5513),
        ("stage0-y", datetime(2026, 1, 1), 24, 2, 540, 0),
    ]
    monkeypatch.setattr("hr.cli.connect", lambda: _FakeConn(rows))
    result = runner.invoke(app, ["sweeps"])
    assert result.exit_code == 0
    assert "stage1-x (largest, latest)" in result.output

def test_health_report_dispatch_via_fake_conn(monkeypatch):
    measurement_rows = [
        ("i1", 0.8, 500, "结论: 42."),
        ("i2", 0.9, 600, "The answer is 42."),
    ]
    monkeypatch.setattr("hr.cli.connect", lambda: _KeyedConn(_health_router(measurement_rows)))
    monkeypatch.setattr("hr.cli.load_deployable", lambda: set())
    result = runner.invoke(app, ["health", "--sweep", "s1"])
    assert result.exit_code == 0
    assert "# Health report — sweep s1" in result.output
    assert "| m1 | 2 |" in result.output
    assert "zero new API calls" in result.output

def test_health_report_notes_retired_models(monkeypatch):
    measurement_rows = [("i1", 0.8, 500, "结论: 42.")]
    monkeypatch.setattr("hr.cli.connect", lambda: _KeyedConn(_health_router(measurement_rows)))
    monkeypatch.setattr("hr.cli.load_deployable", lambda: {"other"})
    result = runner.invoke(app, ["health", "--sweep", "s1"])
    assert result.exit_code == 0
    assert "⚠ retired models in this sweep: m1" in result.output

def test_health_report_shows_battery_sections(monkeypatch):
    """tool_b battery surfaces as its own section in `hr health` output."""
    measurement_rows = [("t1", 0.5, 300, "done."), ("t2", 0.7, 400, "done 2.")]
    router = {
        _BATTERY_KEY: [
            ("tool_b", 10, 30, 0.5),
            ("vision", 15, 15, 0.4),
        ],
        _DISTINCT_MODEL_SQL_KEY: [("m1",)],
        _MEASUREMENT_SQL_KEY: measurement_rows,
        _COUNT_SQL_KEY: [(2,)],
    }
    monkeypatch.setattr("hr.cli.connect", lambda: _KeyedConn(router))
    monkeypatch.setattr("hr.cli.load_deployable", lambda: set())
    result = runner.invoke(app, ["health", "--sweep", "s1"])
    assert result.exit_code == 0
    assert "## Batteries" in result.output
    assert "| tool_b | 10 | 30 | 0.500 |" in result.output
    assert "| vision | 15 | 15 | 0.400 |" in result.output

def test_verdict_empty_conn_reports_no_models(monkeypatch):
    router = {
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
    result = runner.invoke(app, ["verdict", "--sweep", "s1"])
    assert result.exit_code == 0
    assert "# Verdict — sweep s1" in result.output
    assert "zero new API calls" in result.output
    assert "Capability battery averages" in result.output
