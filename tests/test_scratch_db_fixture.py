"""Contract tests for the shared scratch-postgres fixture (hr-evolution T1).

The unified ``scratch_db`` fixture lives in the ROOT ``tests/conftest.py`` so
bench e2e tests (``tests/bench/``) and non-bench DB tests resolve the SAME
object. These tests pin its contract:

* admission/refusal semantics (offline-runnable, never connect)
* distinct UUID-suffixed database names under concurrency
* ``init_schema()`` initialization of an isolated ``hr_test_*`` database
* a VERIFIED teardown — the drop is confirmed via a ``pg_database``
  follow-up query, and the environment is restored.
"""
from __future__ import annotations

import os
import threading
from collections.abc import Callable
from collections.abc import Generator

import psycopg2
import psycopg2.extensions
import pytest

from tests.conftest import _restore_env
from tests.conftest import _save_env
from tests.conftest import _scratch_db_exists
from tests.conftest import scratch_db
from tests.test_db import EXPECTED_TABLES
import tests.conftest as _conftest_mod


@pytest.fixture(autouse=True)
def _reset_admin_test_params_cache() -> None:
    """Run every test in this module against a pristine admin-resolution cache.

    ``_require_admin_test_dsn`` caches the first valid ambient admin DSN so
    concurrent ``scratch_db`` invocations stay immune to each other's
    HR_TEST_PG_DSN rewrite; the admission tests below pin the refusal
    semantics against a FRESH environment, so the cache must start empty
    for them regardless of which live-DB tests ran earlier in the session.
    """
    _conftest_mod._ADMIN_TEST_PARAMS = None
    yield

# pytest 8 wraps decorated fixture functions and forbids DIRECT calls
# ("fixtures are not meant to be called directly"); ``__wrapped__`` exposes
# the raw generator function, which these contract tests must drive
# explicitly to exercise setup/teardown and the admission/refusal paths.
_raw_scratch_db: Callable[[], Generator[tuple[str, str], None, None]] = getattr(
    scratch_db, "__wrapped__"
)


def _require_live_db() -> None:
    """Skip live-DB tests when no admin test DSN is configured (offline)."""
    if not os.environ.get("HR_TEST_PG_DSN"):
        pytest.skip("live scratch-DB tests need HR_TEST_PG_DSN set")


def _admin_params() -> dict[str, str]:
    return psycopg2.extensions.parse_dsn(os.environ["HR_TEST_PG_DSN"])


# --- admission / refusal (offline-runnable, never connect) ------------------


def test_skips_without_any_db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HR_TEST_PG_DSN", raising=False)
    monkeypatch.delenv("HR_DSN", raising=False)
    with pytest.raises(pytest.skip.Exception, match="HR_TEST_PG_DSN"):
        next(_raw_scratch_db())


def test_refuses_ambient_hr_dsn_when_hr_test_pg_dsn_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HR_TEST_PG_DSN", raising=False)
    monkeypatch.setenv("HR_DSN", "postgresql://wikijs:secret@localhost:5432/wiki")
    with pytest.raises(pytest.fail.Exception, match="HR_DSN is rejected"):
        next(_raw_scratch_db())


def test_refuses_non_postgres_dbname_in_hr_test_pg_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HR_DSN", raising=False)
    monkeypatch.setenv(
        "HR_TEST_PG_DSN", "postgresql://wikijs:secret@localhost:5432/wiki"
    )
    with pytest.raises(pytest.fail.Exception, match="dbname='wiki'"):
        next(_raw_scratch_db())


def test_refuses_hr_test_named_dbname_in_hr_test_pg_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HR_DSN", raising=False)
    monkeypatch.setenv(
        "HR_TEST_PG_DSN",
        "postgresql://wikijs:secret@localhost:5432/hr_test_legacy",
    )
    with pytest.raises(pytest.fail.Exception, match="dbname='hr_test_legacy'"):
        next(_raw_scratch_db())


def test_bench_resolves_shared_scratch_db_from_root() -> None:
    """The bench suite must NOT define its own scratch_db — root's wins.

    ``scratch_conn`` remains bench-local and depends on the shared fixture
    by name, which pytest resolves from the root conftest.
    """
    import tests.bench.conftest as bench_conftest

    assert not hasattr(bench_conftest, "scratch_db")
    assert hasattr(bench_conftest, "scratch_conn")


# --- live contract (skip offline; run when HR_TEST_PG_DSN is set) -----------


@pytest.mark.db
@pytest.mark.integration
def test_live_setup_contract_initializes_isolated_schema() -> None:
    _require_live_db()
    gen = _raw_scratch_db()
    dbname, scratch_dsn = next(gen)
    try:
        assert dbname.startswith("hr_test_")
        assert len(dbname) == len("hr_test_") + 10
        assert psycopg2.extensions.parse_dsn(scratch_dsn)["dbname"] == dbname
        # environment points at the scratch DB during the yield
        assert os.environ["HR_TEST_PG_DSN"] == scratch_dsn
        with psycopg2.connect(scratch_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'hr'"
                )
                names = {row[0] for row in cur.fetchall()}
        # every spec table exists (migrations may legitimately add more)
        assert set(EXPECTED_TABLES) <= names
        # the scratch DB is visible on the admin server
        assert _scratch_db_exists(_admin_params(), dbname)
    finally:
        gen.close()  # runs teardown (drop + verification) early if asserts pass


@pytest.mark.db
@pytest.mark.integration
def test_live_teardown_drops_database_and_restores_environment() -> None:
    _require_live_db()
    saved = _save_env()  # ambient env BEFORE the fixture (incl. any test.sh legacy vars)
    gen = _raw_scratch_db()
    dbname, scratch_dsn = next(gen)
    with pytest.raises(StopIteration):
        next(gen)  # exhausts → teardown ran, including its drop verification
    assert not _scratch_db_exists(_admin_params(), dbname)
    # all fixture-owned env vars restored to their ambient values
    assert _save_env() == saved


@pytest.mark.db
@pytest.mark.integration
def test_live_concurrency_yields_distinct_database_names() -> None:
    _require_live_db()
    saved = _save_env()
    names: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _run() -> None:
        try:
            gen = _raw_scratch_db()
            name, _dsn = next(gen)
            with lock:
                names.append(name)
            with pytest.raises(StopIteration):
                next(gen)
        except BaseException as exc:  # noqa: BLE001 — report thread failures
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert not errors, f"scratch_db failed under concurrency: {errors!r}"
        assert len(names) == 2
        assert len(set(names)) == 2
    finally:
        _restore_env(saved)