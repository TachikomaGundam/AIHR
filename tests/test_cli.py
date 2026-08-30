"""Tests for hr.cli — typer wiring, summary_table, fake-conn dispatch.

None of these tests touch the live database: a duck-typed fake connection
(cursor with description/fetchall) is injected where a DB path is exercised.
"""

from __future__ import annotations

import pytest  # noqa: F401 (re-export; consumed by sibling test modules)

from typer.testing import CliRunner

from hr.cli import (
    app,  # noqa: F401 (re-export; consumed by sibling test modules)
    build_health_report,  # noqa: F401 (re-export; consumed by sibling test modules)
    build_sweeps_report,  # noqa: F401 (re-export; consumed by sibling test modules)
    build_status_report,  # noqa: F401 (re-export; consumed by sibling test modules)
    latest_sweep_id,  # noqa: F401 (re-export; consumed by sibling test modules)
)

from hr.health import HealthReport, summary_table  # noqa: F401 (re-export; consumed by sibling test modules)

runner = CliRunner()

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
    "apply-preview",
    "apply-rollback",
    "apply-backups",
    "apply-prune",
    "release-build",
    "release-verify",
    "release-activate",
    "release-rollback",
    "release-list",
    "release-prune",
}

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

_SEAT_SQL_KEY = "FROM hr.seat"

_MODEL_SQL_KEY = "FROM hr.model"

_BATTERY_SQL_KEY = "battery_code FROM hr.battery"

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

_BATTERY_KEY = "COUNT(DISTINCT m.item_id)::int"
