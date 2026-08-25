"""Shared pytest config + fixtures.

Staging-workspace mechanism (contract, not convention):

* ``hr_sandbox`` — ONE central staging fixture. Every test that touches the
  filesystem must derive ALL paths from it (or from ``tmp_path`` directly):
  it seals ``HOME``/``OPENCODE_CONFIG_DIR``/``HR_HOME``/``HR_ITEMREPO``/
  ``HR_OUTPUT_DIR`` into a per-test tmp dir and chdirs into an empty project
  dir. A test that needs the real machine config (opencode.jsonc, overlays)
  must opt in EXPLICITLY and be marked/documented — none do today.

* ``_sealed_home`` — session-scoped autouse HOME redirection. ``Path.home()``
  resolves into session tmp for the whole run, so no test can ever touch the
  real ``~/.config``/``~/.local`` by accident, even one that forgets to
  monkeypatch.

* ``pytest_sessionfinish`` cleanliness guard — snapshots
  ``git status --porcelain`` at session start and asserts it is IDENTICAL at
  session end, failing the run with the exact paths any test left behind
  inside the repo. Together with the HOME seal this makes "repo dirty after
  tests" a hard failure instead of a cleanup chore.

  Sanctioned volatility (invisible to the guard because gitignored):
  ``__pycache__/``, ``.pytest_cache/``, ``.local.yaml`` overlays, the package
  ``hr.toml`` runtime copy.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg2
import psycopg2.extensions
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_NAMES = (
    "seats.yaml",
    "fleet.yaml",
    "deployable.yaml",
    "models.yaml",
    "thresholds.yaml",
    "knowledge.yaml",
    "hr.toml.example",
)
_DB_ENVS = ("HR_DSN", "HR_DB_PASSWORD", "HR_DB_USER", "HR_COMPOSE_FILE")

# Coverage artifacts are excluded from the guard's comparison set: pytest-cov
# erases `.coverage` BEFORE pytest_sessionstart's snapshot and writes it back
# BEFORE the after-snapshot, so it is always absent from the before set and
# present in the after one (flipping the guard on every --cov run). `.coverage`
# and the CI `coverage.xml` report are test-infra byproducts, not test leaks.
_COVERAGE_ARTIFACTS = {".coverage", "coverage.xml"}


@pytest.fixture(scope="session", autouse=True)
def _sealed_home(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Redirect HOME into session tmp for the whole suite (restored after)."""
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path_factory.mktemp("sealed-home"))
    yield
    if saved is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = saved


@pytest.fixture
def hr_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Central staging workspace: all envs sealed into ``tmp_path``.

    Returns anchors: ``tmp_path``, ``home``, ``config_dir`` (opencode),
    ``hr_home``, ``configs`` (``hr_home/configs``), ``itemrepo``, ``project``
    (the chdir'd cwd). No tracked config is materialized — tests write their
    own fixtures; use :func:`materialize_templates` when production shapes
    are needed.
    """
    home = tmp_path / "home"
    home.mkdir()
    config_dir = tmp_path / "opencode"
    config_dir.mkdir()
    hr_home = tmp_path / "hr"
    configs = hr_home / "configs"
    configs.mkdir(parents=True)
    itemrepo = hr_home / "itemrepo"
    itemrepo.mkdir()
    project = tmp_path / "proj"
    project.mkdir()

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HR_HOME", str(hr_home))
    monkeypatch.setenv("HR_ITEMREPO", str(itemrepo))
    monkeypatch.setenv("HR_OUTPUT_DIR", str(tmp_path / "out"))
    for var in _DB_ENVS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(project)

    return {
        "tmp_path": tmp_path,
        "home": home,
        "config_dir": config_dir,
        "hr_home": hr_home,
        "configs": configs,
        "itemrepo": itemrepo,
        "project": project,
    }


def materialize_templates(sandbox: dict[str, Path]) -> None:
    """Copy the tracked ``configs/*.yaml`` templates into the sandbox.

    Call from a fixture/test when the production YAML shapes (seats,
    thresholds, knowledge, …) are required; tests that assert on malformed or
    custom configs must write their own files instead.
    """
    repo_configs = _REPO_ROOT / "configs"
    for name in _CONFIG_NAMES:
        src = repo_configs / name
        if src.is_file():
            shutil.copy(src, sandbox["configs"] / name)


def _repo_status() -> str:
    proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    lines = []
    for line in proc.stdout.splitlines():
        path = line[3:].strip() if len(line) > 3 else line
        if path in _COVERAGE_ARTIFACTS:
            continue
        lines.append(line)
    return "\n".join(lines)


_cleanliness_before: str | None = None


def pytest_sessionstart(session: pytest.Session) -> None:
    global _cleanliness_before
    _cleanliness_before = _repo_status()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Cleanliness guard: the repo must be EXACTLY as clean as it started.

    Any file a test created/modified inside the repo (a stray write outside
    the staging workspace) fails the run and names the offending paths.
    """
    after = _repo_status()
    if after == _cleanliness_before:
        return
    lines = [l for l in after.splitlines() if l.strip()]
    print("\n" + "=" * 72, flush=True)
    print("CLEANLINESS GUARD: repository is dirty after the test session", flush=True)
    print("(a test wrote outside the staging workspace — see paths below):", flush=True)
    for line in lines:
        print(f"  {line}", flush=True)
    print("=" * 72, flush=True)
    session.exitstatus = max(int(exitstatus), 1)


# --- shared scratch-postgres fixture (hr-evolution T1) ----------------------
#
# One unified live-DB fixture: bench e2e tests (tests/bench/) and non-bench
# DB tests consume the SAME ``scratch_db``. Sole entry point: HR_TEST_PG_DSN,
# an ADMIN-level DSN (dbname=postgres) used only to CREATE and DROP a
# throwaway ``hr_test_<uuid-hex>`` database. HR_DSN is rejected for test
# access; offline runs skip instead of failing.

_ENV_NAMES = (
    "HR_TEST_PG_DSN",
    "HR_COMPOSE_FILE",
    "HR_DB_NAME",
    "HR_DB_USER",
    "HR_DB_HOST",
    "HR_DB_PORT",
)


def _save_env() -> dict[str, str | None]:
    return {var: os.environ.get(var) for var in _ENV_NAMES}


def _restore_env(saved: dict[str, str | None]) -> None:
    for var, value in saved.items():
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value


def _require_admin_test_dsn() -> dict[str, str]:
    """Resolve and validate HR_TEST_PG_DSN; return the parsed admin params.

    Refusals (each has a dedicated contract test):
    * HR_DSN set while HR_TEST_PG_DSN is unset -> FAIL (ambient DSN rejected)
    * HR_TEST_PG_DSN dbname != "postgres" (incl. any hr_test_* name) -> FAIL
    * neither env set -> SKIP (offline runs stay green)
    """
    dsn = os.environ.get("HR_TEST_PG_DSN")
    if not dsn:
        if os.environ.get("HR_DSN"):
            pytest.fail(
                "HR_DSN is rejected for test-DB access: set HR_TEST_PG_DSN "
                "(admin-level DSN, dbname=postgres) for live-DB tests"
            )
        pytest.skip("DB credentials required (HR_TEST_PG_DSN) for live-DB tests")
    params = psycopg2.extensions.parse_dsn(dsn)
    dbname = params.get("dbname") or "postgres"
    if dbname != "postgres":
        pytest.fail(
            f"HR_TEST_PG_DSN must be an admin-level connection (dbname=postgres), "
            f"got dbname={dbname!r}; the fixture creates and drops its own "
            f"hr_test_* scratch database — never run tests in a real database"
        )
    return params


def _admin_connect(
    params: dict[str, str], dbname: str = "postgres"
) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(
        dbname=dbname,
        host=params.get("host") or "localhost",
        port=int(params.get("port") or 5432),
        user=params.get("user") or "wikijs",
        password=params.get("password"),
    )
    conn.autocommit = True
    return conn


def _scratch_db_exists(params: dict[str, str], dbname: str) -> bool:
    with _admin_connect(params) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
            return cur.fetchone() is not None


def _compose_shim(params: dict[str, str], directory: str) -> str:
    """Temporary docker-compose feeding ``hr.config``'s HR_COMPOSE_FILE path.

    Lets ``db_dsn()`` resolve to the scratch DB (HR_DSN untouched) with the
    admin DSN's credentials; host/port come from the admin params so a
    non-local endpoint still resolves.
    """
    compose_path = Path(directory) / "docker-compose.yml"
    payload = {
        "services": {
            "wiki": {
                "environment": {
                    "DB_PASS": params.get("password") or "",
                    "DB_HOST": params.get("host") or "localhost",
                    "DB_PORT": str(params.get("port") or 5432),
                    "DB_NAME": params.get("dbname") or "wiki",
                    "DB_USER": params.get("user") or "wikijs",
                }
            }
        }
    }
    with compose_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh)
    return str(compose_path)


@pytest.fixture(scope="module")
def scratch_db() -> Iterator[tuple[str, str]]:
    """Yield (dbname, scratch_dsn) on a fresh isolated ``hr_test_*`` database.

    Admin connection via HR_TEST_PG_DSN only (see ``_require_admin_test_dsn``
    for the refusal semantics). The scratch DB name is unique per invocation,
    initialized with :func:`hr.db.init_schema`, and DROPPED — with verified
    teardown (pg_database follow-up) — afterwards. During the yield the
    environment points at the scratch DB: HR_TEST_PG_DSN is rewritten to the
    scratch DSN and ``db_dsn()`` resolution runs through a temporary compose
    shim (HR_COMPOSE_FILE + HR_DB_*), never through HR_DSN. Everything is
    restored before the drop.
    """
    params = _require_admin_test_dsn()
    dbname = "hr_test_" + uuid.uuid4().hex[:10]
    admin = _admin_connect(params)
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        admin.close()
    scratch_dsn = psycopg2.extensions.make_dsn(**{**params, "dbname": dbname})
    shim_dir = tempfile.mkdtemp(prefix="hr-test-pg-")
    saved = _save_env()
    try:
        os.environ["HR_TEST_PG_DSN"] = scratch_dsn
        os.environ["HR_COMPOSE_FILE"] = _compose_shim(params, shim_dir)
        os.environ["HR_DB_NAME"] = dbname
        os.environ["HR_DB_USER"] = params.get("user") or "wikijs"
        os.environ["HR_DB_HOST"] = params.get("host") or "localhost"
        os.environ["HR_DB_PORT"] = str(params.get("port") or 5432)
        from hr.db import init_schema  # imported fresh, matches the bench pattern

        init_schema()
        yield dbname, scratch_dsn
    finally:
        _restore_env(saved)
        shutil.rmtree(shim_dir, ignore_errors=True)
        admin = _admin_connect(params)
        try:
            with admin.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        finally:
            admin.close()
        assert not _scratch_db_exists(params, dbname), (
            f"scratch DB teardown verification failed: {dbname!r} still exists"
        )