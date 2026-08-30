#!/usr/bin/env bash
# scripts/test.sh — canonical test entrypoint for the hr engine (plan T5).
#
# Modes:
#   scripts/test.sh             offline: full suite + coverage (fail-under 80)
#                               + universality gate — no DB environment needed.
#   scripts/test.sh --with-db   offline suite PLUS the db-marked tests against a
#                               live postgres server. Requires $HR_TEST_PG_DSN
#                               (admin-level DSN, dbname=postgres): the shared
#                               scratch-db fixture creates/drops its own
#                               hr_test_<uuid> database — $HR_DSN is rejected for
#                               test access. Without it, falls back to the local
#                               wiki compose file at $HOME/wiki/docker-compose.yml,
#                               where a fresh scratch job DB hr_test_<timestamp> is
#                               created and always dropped by an exit trap (for the
#                               env-gated legacy tests). The wiki database is never
#                               a DSN target.
#   scripts/test.sh --ci        exact ci.yml step-order simulation (compileall,
#                               ruff, basedpyright, pytest + coverage, universality
#                               gate) — local only, no postgres service.
#
# Any other argument prints this usage and exits 1.
set -euo pipefail

cd "$(dirname "$0")/.."

usage() {
    cat <<'EOF'
usage: scripts/test.sh [MODE]

Modes:
  (no flag)   offline: full suite + coverage (--cov-fail-under=80)
              + universality gate — no DB environment needed.
  --with-db   offline suite PLUS the db-marked tests against a live postgres
              server. Requires $HR_TEST_PG_DSN (admin-level DSN, dbname=postgres):
              the shared scratch-db fixture creates/drops its own hr_test_<uuid>
              database — $HR_DSN is rejected for test access. Without it, falls
              back to $HOME/wiki/docker-compose.yml, where a fresh scratch job DB
              hr_test_<timestamp> is created and always dropped by an exit trap.
              The wiki database is never a DSN target.
  --ci        exact ci.yml step-order simulation (compileall, ruff,
              basedpyright, pytest + coverage, universality gate).

Any other argument prints this usage and exits 1.
EOF
}

clear_database_environment() {
    # The offline command must never discover an ambient production DSN.
    # Database tests are opt-in through --with-db only.
    unset HR_DSN HR_DB_NAME HR_DB_PASSWORD HR_DB_USER HR_COMPOSE_FILE HR_TEST_DB HR2_DB_PASSWORD
}

run_test_suite() {
    python3 -m pytest --cov=hr --cov-report=term-missing --cov-fail-under=80 -q "$@"
}

run_universality_gate() {
    bash scripts/check_universal.sh
}

drop_job_db() {
    docker exec wiki-db psql -U wikijs -d postgres -Atc \
        "DROP DATABASE IF EXISTS \"${JOB_DB}\" WITH (FORCE)" >/dev/null 2>&1 || true
    echo "== --with-db: dropped scratch job DB ${JOB_DB} =="
}

setup_legacy_job_db() {
    # Legacy env-gated tests (test_db live, stage1 dry-run) resolve db_dsn()
    # through $HOME/wiki/docker-compose.yml; give them a scratch job DB.
    export HR_COMPOSE_FILE="$HOME/wiki/docker-compose.yml"
    JOB_DB="hr_test_$(date +%s)"
    echo "== --with-db: HR_COMPOSE_FILE=$HR_COMPOSE_FILE (legacy job DB $JOB_DB for env-gated tests) =="
    if ! docker exec wiki-db psql -U wikijs -d postgres -Atc \
        "CREATE DATABASE \"${JOB_DB}\" OWNER wikijs" >/dev/null 2>&1; then
        echo "ERROR: could not create scratch job DB ${JOB_DB} via the wiki-db container" >&2
        echo "       is the postgres server running? (docker exec wiki-db psql ...)" >&2
        exit 1
    fi
    export HR_DB_NAME="$JOB_DB"
    trap drop_job_db EXIT
}

run_with_db() {
    JOB_DB=""
    legacy_db=0
    if [[ -n "${HR_TEST_PG_DSN:-}" ]]; then
        dsn_db="${HR_TEST_PG_DSN##*/}"
        dsn_db="${dsn_db%%\?*}"
        if [[ "$dsn_db" != "postgres" ]]; then
            echo "ERROR: HR_TEST_PG_DSN must be an admin-level connection (dbname=postgres, got $dsn_db) — the shared scratch-db fixture creates its own hr_test_* database and never runs tests in a caller-provided database" >&2
            exit 1
        fi
        echo "== --with-db: HR_TEST_PG_DSN=<admin postgres> — shared scratch-db fixture =="
        if [[ -f "$HOME/wiki/docker-compose.yml" ]]; then
            setup_legacy_job_db
            legacy_db=1
        fi
    elif [[ -n "${HR_DSN:-}" ]]; then
        echo "ERROR: HR_DSN is rejected for test-DB access — set HR_TEST_PG_DSN instead (admin-level DSN, dbname=postgres); the fixture creates and drops its own scratch hr_test_* database" >&2
        exit 1
    elif [[ -f "$HOME/wiki/docker-compose.yml" ]]; then
        setup_legacy_job_db
        legacy_db=1
    else
        cat >&2 <<'EOF'
ERROR: --with-db needs a live postgres server to exercise the db-marked
       tests, but found NEITHER:
         - $HR_TEST_PG_DSN (admin-level DSN, dbname=postgres; the fixture
           creates and drops its own hr_test_* scratch database), nor
         - $HOME/wiki/docker-compose.yml (legacy job-DB path)
       Refusing to silently skip the db tests.
EOF
        exit 1
    fi
    # HR_TEST_DB=1 switches on the env-gated legacy live-DB test, which needs
    # a resolvable db_dsn() — only when the legacy job DB is actually set up.
    if [[ "$legacy_db" == 1 ]]; then
        export HR_TEST_DB=1
    fi
    # Belt and braces: keep coverage data out of the repo tree even though
    # the cleanliness guard ignores .coverage artifacts.
    export COVERAGE_FILE="/tmp/${JOB_DB:-hr-with-db}-$$.coverage"
    run_test_suite --cov-report=xml
    run_universality_gate
}

run_ci_sim() {
    echo "== --ci: compileall =="
    python3 -m compileall -q hr scripts itemrepo tests
    echo "== --ci: ruff check =="
    python3 -m ruff check hr scripts itemrepo
    echo "== --ci: basedpyright =="
    python3 -m basedpyright --level error hr scripts
    echo "== --ci: pytest + coverage =="
    clear_database_environment
    run_test_suite --cov-report=xml
    echo "== --ci: universality gate =="
    run_universality_gate
    echo "== --ci: wheel build =="
    wheel_dir="$(mktemp -d)"
    python3 -m pip wheel --no-deps --wheel-dir "$wheel_dir" .
    rm -rf "$wheel_dir"
}

case "${1:-}" in
    "")
        clear_database_environment
        run_test_suite
        run_universality_gate
        ;;
    --with-db)
        run_with_db
        ;;
    --ci)
        run_ci_sim
        ;;
    *)
        usage
        exit 1
        ;;
esac
