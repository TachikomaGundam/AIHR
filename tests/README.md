# Test Suite Developer Guide

Developer reference for the `hr` test suite. Covers installation, run modes, sandbox mechanics, skip semantics, markers, coverage gates, CI behavior, and how to add a DB-gated test.

All commands assume the working directory is the repo root.

## Install

```bash
python3 -m pip install -e ".[test]"
```

On systems with PEP 668 enforcement (externally managed Python), add the override:

```bash
PIP_BREAK_SYSTEM_PACKAGES=1 python3 -m pip install -e ".[test]"
```

The `[test]` extra pulls in `pytest>=8.0`, `pytest-cov>=5.0`, `pytest-timeout>=2.3`, `basedpyright>=1.39`, and `ruff>=0.16`.

## Run Modes

Three modes live in `scripts/test.sh`. Any unrecognised flag prints usage and exits 1.

| Mode | Command | DB needed | Coverage gate | Exit 0 when |
|------|---------|-----------|---------------|-------------|
| Offline (default) | `bash scripts/test.sh` | no | `--cov-fail-under=80` | all tests pass and coverage >= 80 % |
| With DB | `bash scripts/test.sh --with-db` | yes | `--cov-fail-under=80` | all tests pass (incl. DB) and coverage >= 80 % |
| CI simulation | `bash scripts/test.sh --ci` | no | >= 80% | all lints + tests + universality + wheel build pass |

### Offline (default)

```bash
bash scripts/test.sh
```

Runs the full offline suite with `--cov-fail-under=80`, then the universality gate (`scripts/check_universal.sh`). No database or network required. Nine DB-gated tests skip automatically (see Skip Semantics).

You can also invoke pytest directly without the coverage threshold:

```bash
python3 -m pytest -q
```

This runs the same offline tests but without `--cov-fail-under`, so it exits 0 as long as every collected test passes.

### With DB

```bash
bash scripts/test.sh --with-db
```

Runs the offline suite plus the nine `@pytest.mark.db` tests against a live postgres server. Credential resolution:

1. **`$HR_DSN`** set by the caller. Must point at a scratch database. The script refuses if the DSN targets the `wiki` database.
2. **`$HOME/wiki/docker-compose.yml`** present. The script creates a scratch job database named `hr_test_<timestamp>`, sets `HR_TEST_DB=1`, and drops the database in an EXIT trap.

Either path sets `HR_TEST_DB=1` so the `test_db.py` live-schema test activates alongside the `scratch_db` fixture tests.

### CI simulation

```bash
bash scripts/test.sh --ci
```

Mirrors the local-safe CI sequence in `.github/workflows/ci.yml`: `compileall`, `ruff check`, `basedpyright`, `pytest --cov --cov-fail-under=80`, universality gate, and wheel build. It intentionally omits CI's PostgreSQL service, so DB-marked tests remain skipped.

### Unknown flag

```bash
bash scripts/test.sh --bogus
```

Prints usage to stderr and exits 1.

## Sandbox Semantics

The test suite uses a layered isolation model so no test can touch the real filesystem or leak credentials.

### `_sealed_home` (session-scoped, autouse)

Redirects `HOME` (and therefore `Path.home()`) into a session-scoped temporary directory for the entire pytest run. Restored after the session. No test can accidentally read or write `~/.config`, `~/.local`, or any other real home directory path.

### `hr_sandbox` (function-scoped)

The central staging fixture. Every test that touches the filesystem should request it. It:

- Creates isolated directories for `HOME`, `OPENCODE_CONFIG_DIR`, `HR_HOME`, `HR_ITEMREPO`, and `HR_OUTPUT_DIR` inside `tmp_path`.
- Deletes the DB environment variables (`HR_DSN`, `HR_DB_PASSWORD`, `HR_DB_USER`, `HR_COMPOSE_FILE`) so credential-gated code paths behave as offline.
- Changes the working directory to an empty project directory.

Returns a dict of path anchors: `tmp_path`, `home`, `config_dir`, `hr_home`, `configs`, `itemrepo`, `project`.

Call `materialize_templates(sandbox)` from `tests/conftest.py` when the production YAML shapes (seats, fleet, thresholds, etc.) are needed in the sandbox.

### Cleanliness guard

`pytest_sessionstart` snapshots `git status --porcelain`. `pytest_sessionfinish` compares the snapshot and fails the run if the repo is dirty, printing the offending paths. Coverage artifacts (`.coverage`, `coverage.xml`) are excluded from the comparison because pytest-cov writes them between the two snapshots.

Sanctioned volatility (gitignored, invisible to the guard): `__pycache__/`, `.pytest_cache/`, `.local.yaml` overlays, the package `hr.toml` runtime copy.

## Skip Semantics

Nine tests carry `@pytest.mark.db` and skip in offline runs. They fall into two gate families:

### scratch_db fixture gate (8 tests)

These tests request the `scratch_db` fixture (defined in `tests/bench/conftest.py`). The fixture checks for any of `HR_DSN`, `HR_DB_PASSWORD`, or `HR_COMPOSE_FILE` in the environment. When none are present, it calls `pytest.skip(...)`.

| Test file | Test function | Line |
|-----------|---------------|------|
| `tests/bench/test_engine_persistence.py` | `test_e2e_all_batteries_write_measurements_with_linkage` | 49 |
| `tests/bench/test_engine_persistence.py` | `test_e2e_idempotent_registration` | 175 |
| `tests/bench/test_engine_failure_storage.py` | `test_store_writes_expected_response_text` | 18 |
| `tests/bench/test_engine_failure_storage.py` | `test_e2e_garbage_adapter_records_failed_run_not_crash` | 50 |
| `tests/bench/test_cli_bench.py` | `test_bench_mocked_e2e_writes_measurements` | 56 |
| `tests/bench/test_cli_pick.py` | `test_pick_feeds_engine_run_set` | 234 |
| `tests/test_stage1_config.py` | `test_dry_run_no_api_calls_with_db_empty` | 26 |
| `tests/test_stage1_planning.py` | `test_dry_run_with_override_finalists` | 164 |

### Environment gate (1 test)

`tests/test_db.py` line 93 uses `@pytest.mark.skipif(os.environ.get("HR_TEST_DB") != "1", ...)` as an explicit opt-in. Even with DB credentials present, this test only runs when `HR_TEST_DB=1` is set.

| Test file | Test function | Line | Gate |
|-----------|---------------|------|------|
| `tests/test_db.py` | `test_init_schema_against_live_db` | 93 | `HR_TEST_DB=1` |

### Gate summary

| Environment variable | Who checks it | Effect when absent |
|----------------------|---------------|-------------------|
| `HR_DSN` | `scratch_db` fixture, `_has_live_db_credentials()` | 8 tests skip |
| `HR_DB_PASSWORD` | `scratch_db` fixture, `_has_live_db_credentials()` | 8 tests skip |
| `HR_COMPOSE_FILE` | `scratch_db` fixture, `_has_live_db_credentials()` | 8 tests skip |
| `HR_TEST_DB` | `test_db.py:95` skipif, `test_db.py:100` body guard | 1 test skips |

## Markers

Two markers are declared in `pyproject.toml` under `[tool.pytest.ini_options]`:

| Marker | Meaning |
|--------|---------|
| `db` | Requires a live postgres server (env/service-gated) |
| `integration` | Exercises real external resources (postgres) |

Pytest runs with `--strict-markers` (set in `addopts`). Any test decorated with an undeclared marker fails collection immediately. When adding a new marker, register it in `pyproject.toml` first.

Run only offline tests (exclude DB-marked):

```bash
python3 -m pytest -m "not db" -q
```

List all DB-marked tests without running them:

```bash
python3 -m pytest -m db --co -q
```

## Coverage Gates

| Gate | Threshold | Where enforced |
|------|-----------|----------------|
| Overall | >= 80 % | `scripts/test.sh` default and `--with-db` (`--cov-fail-under=80`) |
| Per-module (bottom 3) | reporting target | Review the three lowest-coverage modules before release; the current runner does not enforce a per-module threshold. |

The overall gate is live today. `scripts/test.sh` (default, `--with-db`, and `--ci`) exits 1 when total coverage falls below 80 %.

Coverage is measured with `--cov=hr --cov-report=term-missing`. Branch coverage is enabled (`branch = true` in `pyproject.toml`). The source set is `hr/` only.

## CI Behavior

`.github/workflows/ci.yml` runs on push and pull request. The job matrix covers Python 3.12 and 3.14.

### Postgres service

A `postgres:16-alpine` container starts with a health check:

| Setting | Value |
|---------|-------|
| `POSTGRES_USER` | `hr` |
| `POSTGRES_PASSWORD` | `hr` |
| `POSTGRES_DB` | `hr_test` |

### Step order

1. Install `bubblewrap` (sandbox for code execution tests).
2. `pip install ".[test]"`.
3. `compileall -q hr scripts itemrepo tests`.
4. `ruff check hr scripts itemrepo`.
5. `basedpyright --level error hr scripts`.
6. **Tests**: `pytest --cov=hr --cov-report=term-missing --cov-report=xml -q` with `HR_DSN=postgresql://hr:hr@localhost:5432/hr_test` and `HR_TEST_DB=1`. All nine DB-gated tests run.
7. Upload `coverage.xml` as an artifact.
8. **Universality gate**: `bash scripts/check_universal.sh` (model-name literals, absolute paths, import smoke, secret scan).
9. `pip wheel` (build verification).

## How to Add a DB-Gated Test

### Using the scratch_db fixture (preferred)

For tests that need a full HR schema on a real postgres server:

```python
import pytest

@pytest.mark.db
@pytest.mark.integration
def test_my_feature_writes_to_db(scratch_conn):
    """scratch_conn is a psycopg2 connection to a fresh scratch DB
    with the HR schema already initialized. The DB is dropped
    automatically when the module tears down."""
    cur = scratch_conn.cursor()
    cur.execute("SELECT count(*) FROM hr_models")
    assert cur.fetchone()[0] == 0
```

The `scratch_db` fixture (module-scoped, `tests/bench/conftest.py`):
1. Checks for `HR_DSN`, `HR_DB_PASSWORD`, or `HR_COMPOSE_FILE`. Skips when none are present.
2. Creates a database named `qa_bench12_<uuid10>` on the same postgres server.
3. Calls `init_schema()` to build the HR tables.
4. Yields `(dbname, scratch_dsn)`.
5. Drops the database with `DROP DATABASE ... WITH (FORCE)` in teardown.

The `scratch_conn` fixture depends on `scratch_db` and yields a ready psycopg2 connection.

### Using an env gate

For tests that connect to the DB directly (not through the fixture):

```python
import os
import pytest

@pytest.mark.db
@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("HR_TEST_DB") != "1",
    reason="requires a live DB; set HR_TEST_DB=1 + HR_DSN pointing at a SCRATCH DB",
)
def test_schema_migration():
    # Connect using the DSN from the environment
    ...
```

### Rules

- **Never target the `wiki` database.** All DB tests use scratch databases that are created and destroyed per run.
- **Always mark with both `@pytest.mark.db` and `@pytest.mark.integration`.** Undeclared markers fail collection under `--strict-markers`.
- **The scratch_db fixture handles skip logic.** Do not add your own credential check when using `scratch_conn` or `scratch_db`.
- **Register new markers** in `pyproject.toml` `[tool.pytest.ini_options].markers` before using them.

## Related Documentation

- [Seat profile summary](../docs/en/seat-profile-summary.md) for role definitions.
- [Capability priors](../docs/en/capability-prior.md) for scoring model details.
