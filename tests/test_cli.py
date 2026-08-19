"""Tests for hr.cli — typer wiring, summary_table, fake-conn dispatch.

None of these tests touch the live database: a duck-typed fake connection
(cursor with description/fetchall) is injected where a DB path is exercised.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from hr.cli import (
    app,
    build_health_report,
    build_sweeps_report,
    build_status_report,
    latest_sweep_id,
)
from hr.health import HealthReport, summary_table

runner = CliRunner()

# The 13-command inventory is the ONLY binding inventory (metis m1/m2).
COMMANDS = {
    "discover",
    "seed",
    "bench",
    "verdict",
    "health",
    "sweeps",
    "calibrate",
    "reference",
    "research",
    "publish",
    "recommend",
    "status",
    "apply",
}


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


class _FakeCursor:
    def __init__(self, rows):
        self.description = [("c",)] * len(rows[0]) if rows else [("c",)]
        self._rows = rows

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.queries = []

    def cursor(self):
        return _FakeCursor(self._rows)

    def close(self):
        pass


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


class _KeyedCursor:
    """Cursor that routes fetchall() by a substring key of the SQL text."""

    def __init__(self, router):
        self._router = router

    def execute(self, sql, params=None):
        self._match = None
        for key, rows in self._router.items():
            if key in sql:
                self._match = rows
                if key == _MEASUREMENT_SQL_KEY:
                    self.description = [
                        ("item_id",), ("score",), ("tokens_out",), ("response_text",),
                    ]
                else:
                    self.description = [("c",)]
                return
        self._match = []
        self.description = [("c",)]

    def fetchall(self):
        return self._match

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class _KeyedConn:
    def __init__(self, router):
        self._router = dict(router)
        self._router.setdefault("", [])

    def cursor(self):
        return _KeyedCursor(self._router)

    def close(self):
        pass


_MEASUREMENT_SQL_KEY = "SELECT m.item_id, m.score"
_DISTINCT_MODEL_SQL_KEY = "SELECT DISTINCT r.model_id"
_COUNT_SQL_KEY = "SELECT COUNT(m.measurement_id)::int"
_AVG_SQL_KEY = "AVG(m.score)"
_SEAT_SQL_KEY = "FROM hr2.seat"
_MODEL_SQL_KEY = "FROM hr2.model"
_BATTERY_SQL_KEY = "battery_code FROM hr2.battery"
# latest_sweep_id's SQL has newline right after s.sweep_id; the sweeps-table
# query has "s.sweep_id," (comma) — distinct substrings, order matters.
_LATEST_SQL_KEY = "SELECT s.sweep_id\n"
_SWEEPS_SQL_KEY = "s.created_at"


def _health_router(measurement_rows):
    return {
        _AVG_SQL_KEY: [],
        _DISTINCT_MODEL_SQL_KEY: [("m1",)],
        _MEASUREMENT_SQL_KEY: measurement_rows,
        _COUNT_SQL_KEY: [(len(measurement_rows),)],
        _SEAT_SQL_KEY: [],
        _MODEL_SQL_KEY: [],
        _BATTERY_SQL_KEY: [("b1",)],
    }


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


# The battery-breakdown SQL contains "AVG(m.score)" — this key MUST be
# first in router order so the first-match fake conn routes it correctly.
_BATTERY_KEY = "COUNT(DISTINCT m.item_id)::int"


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