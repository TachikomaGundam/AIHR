"""Scratch-DB conftest for bench e2e tests (task 12 / hr-evolution T1).

``scratch_db`` is now the SHARED fixture from the root tests/conftest.py
(pytest auto-loads it) — bench and non-bench DB tests consume the same
object. This module only keeps the bench-local convenience wrapper
``scratch_conn``. Admission/refusal semantics moved to the root fixture:
HR_TEST_PG_DSN (admin, dbname=postgres) only; HR_DSN is rejected; offline
runs skip.
"""

from __future__ import annotations

import psycopg2
import pytest


@pytest.fixture
def scratch_conn(scratch_db):
    """psycopg2 connection to the scratch DB (tears down with the module)."""
    _name, dsn = scratch_db
    conn = psycopg2.connect(dsn)
    yield conn
    conn.close()