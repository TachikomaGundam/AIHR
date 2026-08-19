"""Scratch-DB fixture for bench e2e tests (task 12).

Mirrors the task-15 scratch-DB QA pattern: create a throwaway database on
the same postgres server, init the canonical hr2 schema, point HR_DSN at it,
and DROP it in teardown. Credential-gated: without HR_DSN/HR_COMPOSE_FILE the
fixture skips (offline runs stay green).
"""

from __future__ import annotations

import os
import uuid

import pytest
import psycopg2
import psycopg2.extensions


def _creds_available() -> bool:
    return any(
        os.environ.get(n)
        for n in ("HR_DSN", "HR_DB_PASSWORD", "HR2_DB_PASSWORD", "HR_COMPOSE_FILE")
    )


@pytest.fixture(scope="module")
def scratch_db():
    """Yield (dbname, scratch_dsn) on a fresh scratch DB; drop on teardown."""
    if not _creds_available():
        pytest.skip("DB credentials required (HR_DSN/HR_COMPOSE_FILE) for live-DB e2e")
    from hr.config import db_dsn

    dsn = db_dsn()
    params = psycopg2.extensions.parse_dsn(dsn)
    name = "qa_bench12_" + uuid.uuid4().hex[:10]
    host = params.get("host") or "localhost"
    port = params.get("port") or 5432
    admin = psycopg2.connect(
        dbname="postgres", user=params.get("user"), password=params.get("password"),
        host=host, port=int(port),
    )
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        admin.close()
    scratch_dsn = psycopg2.extensions.make_dsn(
        **{**params, "dbname": name, "host": host, "port": port}
    )
    old_dsn = os.environ.get("HR_DSN")
    os.environ["HR_DSN"] = scratch_dsn
    try:
        from hr.db import init_schema

        init_schema()
        yield name, scratch_dsn
    finally:
        if old_dsn is None:
            os.environ.pop("HR_DSN", None)
        else:
            os.environ["HR_DSN"] = old_dsn
        admin = psycopg2.connect(
            dbname="postgres", user=params.get("user"), password=params.get("password"),
            host=host, port=int(port),
        )
        admin.autocommit = True
        try:
            with admin.cursor() as cur:
                cur.execute(
                    f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'
                )
        finally:
            admin.close()


@pytest.fixture
def scratch_conn(scratch_db):
    """psycopg2 connection to the scratch DB (tears down with the module)."""
    _name, dsn = scratch_db
    conn = psycopg2.connect(dsn)
    yield conn
    conn.close()