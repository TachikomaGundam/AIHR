"""hr apply — verdict → FastDraw preset/state bridge tests (hr-unification, todo 18).

Offline: duck-typed fake connection (cursor with description/fetchall, test_cli
style) routes each SQL by a substring key; the config dir is redirected with
the OPENCODE_CONFIG_DIR env override so nothing touches ~/.config/opencode.

The full-chain dispatch test (test_apply_dispatch_) exercises the whole
pipeline — latest sweep → capability means → health reports → ranker →
seat_assignments → preset JSON — with only the SQL layer faked. FastDraw
sources are never imported: these tests assert the JSON file contract shapes
the plugin parses (presets store, isModelMap "/" rule, boot-time state file).
"""

from __future__ import annotations

import json  # noqa: F401 (re-export; consumed by sibling test modules)

import re  # noqa: F401 (re-export; consumed by sibling test modules)

from datetime import date

import pytest  # noqa: F401 (re-export; consumed by sibling test modules)

from typer.testing import CliRunner

from hr import apply as apply_mod  # noqa: F401 (re-export; consumed by sibling test modules)

from hr.apply import (
    PRESETS_FILENAME,  # noqa: F401 (re-export; consumed by sibling test modules)
    STATE_FILENAME,  # noqa: F401 (re-export; consumed by sibling test modules)
    agents_from_assignments,  # noqa: F401 (re-export; consumed by sibling test modules)
    apply,  # noqa: F401 (re-export; consumed by sibling test modules)
    validate_agents,  # noqa: F401 (re-export; consumed by sibling test modules)
    write_preset,  # noqa: F401 (re-export; consumed by sibling test modules)
    write_state,  # noqa: F401 (re-export; consumed by sibling test modules)
)

from hr.cli import app  # noqa: F401 (re-export; consumed by sibling test modules)

runner = CliRunner()

_MODEL = "bailian-token-plan/deepseek-v4-flash"

_MEASUREMENT_SQL_KEY = "SELECT m.item_id, m.score"

_DISTINCT_MODEL_SQL_KEY = "SELECT DISTINCT r.model_id"

_COUNT_SQL_KEY = "SELECT COUNT(m.measurement_id)::int"

_AVG_SQL_KEY = "AVG(m.score)"

_BREAKDOWN_SQL_KEY = "AS mean_score"

_SEAT_SQL_KEY = "FROM hr.seat"

_MODEL_SQL_KEY = "FROM hr.model"

_BATTERY_SQL_KEY = "battery_code FROM hr.battery"

_LATEST_SQL_KEY = "SELECT s.sweep_id\n"

_BATTERIES = [
    "reasoning",
    "tool_a",
    "hallucination",
    "livebench_long_context",
    "livebench_speed",
]

class _KeyedCursor:
    """Cursor routing fetchall() by the first substring key match (test_cli pattern)."""

    def __init__(self, router):
        self._router = router

    def execute(self, sql, params=None):
        self._match = []
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
        self.description = [("c",)]

    def fetchall(self):
        return self._match

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

class _KeyedConn:
    """Fake connection; unmatched SQL falls back to an empty result set."""

    def __init__(self, router):
        self._router = dict(router)
        self._router.setdefault("", [])

    def cursor(self):
        return _KeyedCursor(self._router)

    def close(self):
        pass

def _router(means=True) -> dict:
    """Router for one sweep s1 with one healthy model on all five batteries."""
    router = {
        _BREAKDOWN_SQL_KEY: [],
        _AVG_SQL_KEY: [
            (_MODEL, "reasoning", 0.9),
            (_MODEL, "tool_a", 0.8),
            (_MODEL, "hallucination", 0.7),
            (_MODEL, "livebench_long_context", 0.6),
            (_MODEL, "livebench_speed", 0.5),
        ],
        _DISTINCT_MODEL_SQL_KEY: [(_MODEL,)],
        _MEASUREMENT_SQL_KEY: [("i1", 0.9, 500, "The answer is 42.")],
        _COUNT_SQL_KEY: [(1,)],
        _SEAT_SQL_KEY: [("oracle", [], None)],
        _MODEL_SQL_KEY: [],
        _BATTERY_SQL_KEY: [(b,) for b in _BATTERIES],
        _LATEST_SQL_KEY: [("s1",)],
    }
    if not means:
        router[_AVG_SQL_KEY] = []
    return router

def _today_name() -> str:
    return f"verdict-{date.today().isoformat()}"

def _entry(store: dict, name: str) -> dict:
    return store["presets"][name]
