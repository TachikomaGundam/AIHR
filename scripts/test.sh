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
#                               test access. Without it, prefers the AIHR
#                               container: when the aihr-db container is running
#                               (hr db-up), a fresh scratch job DB
#                               hr_test_<timestamp> is created on it via
#                               docker exec, owned by aihr, and always dropped by
#                               an exit trap; only HR_DB_NAME is exported — the
#                               config chain finds password/port via docker/.env.
#                               Last resort: the legacy wiki compose file at
#                               $HOME/wiki/docker-compose.yml (same scratch job
#                               DB, resolved through the HR_COMPOSE_FILE compose
#                               fallback). The wiki/aihr production databases are
#                               never DSN targets.
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
              database — $HR_DSN is rejected for test access. Without it,
              prefers the AIHR container: a running aihr-db container (hr db-up)
              gets a fresh scratch job DB hr_test_<timestamp> created via
              docker exec (owner aihr) and dropped by an exit trap; only
              HR_DB_NAME is exported — password/port resolve via docker/.env.
              Last resort: $HOME/wiki/docker-compose.yml (legacy job-DB path,
              resolved through the extended HR_COMPOSE_FILE compose fallback).
              Production databases (aihr/wiki) are never DSN targets.
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

aihr_db_running() {
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx aihr-db
}

drop_job_db() {
    docker exec "$JOB_CONTAINER" psql -U "$JOB_USER" -d postgres -Atc \
        "DROP DATABASE IF EXISTS \"${JOB_DB}\" WITH (FORCE)" >/dev/null 2>&1 || true
    echo "== --with-db: dropped scratch job DB ${JOB_DB} =="
}

create_job_db() {
    # $1=container $2=user $3=label — scratch job DB on the running postgres container
    JOB_CONTAINER="$1"
    JOB_USER="$2"
    JOB_DB="hr_test_$(date +%s)"
    echo "== --with-db: $3 job DB $JOB_DB for env-gated tests (via container $JOB_CONTAINER) =="
    if ! docker exec "$JOB_CONTAINER" psql -U "$JOB_USER" -d postgres -Atc \
        "CREATE DATABASE \"${JOB_DB}\" OWNER ${JOB_USER}" >/dev/null 2>&1; then
        echo "ERROR: could not create scratch job DB ${JOB_DB} via the $JOB_CONTAINER container" >&2
        echo "       is the postgres server running? (docker exec $JOB_CONTAINER psql ...)" >&2
        exit 1
    fi
    export HR_DB_NAME="$JOB_DB"
    trap drop_job_db EXIT
}

setup_aihr_job_db() {
    # AIHR turnkey stack: hr.config resolves the password/port from the db env
    # file (docker/.env), so ONLY HR_DB_NAME needs exporting for the scratch job DB.
    create_job_db aihr-db aihr "aihr-db"
}

setup_legacy_job_db() {
    # Legacy env-gated tests on pre-aihr machines resolve db_dsn() through
    # $HOME/wiki/docker-compose.yml (the extended compose fallback reads the
    # correct user/db/password from it); give them a scratch job DB.
    export HR_COMPOSE_FILE="$HOME/wiki/docker-compose.yml"
    create_job_db wiki-db wikijs "legacy wiki"
}

run_with_db() {
    JOB_DB=""
    JOB_CONTAINER=""
    JOB_USER=""
    job_db=0
    if [[ -n "${HR_TEST_PG_DSN:-}" ]]; then
        dsn_db="${HR_TEST_PG_DSN##*/}"
        dsn_db="${dsn_db%%\?*}"
        if [[ "$dsn_db" != "postgres" ]]; then
            echo "ERROR: HR_TEST_PG_DSN must be an admin-level connection (dbname=postgres, got $dsn_db) — the shared scratch-db fixture creates its own hr_test_* database and never runs tests in a caller-provided database" >&2
            exit 1
        fi
        echo "== --with-db: HR_TEST_PG_DSN=<admin postgres> — shared scratch-db fixture =="
        if aihr_db_running; then
            setup_aihr_job_db
            job_db=1
        elif [[ -f "$HOME/wiki/docker-compose.yml" ]]; then
            setup_legacy_job_db
            job_db=1
        fi
    elif [[ -n "${HR_DSN:-}" ]]; then
        echo "ERROR: HR_DSN is rejected for test-DB access — set HR_TEST_PG_DSN instead (admin-level DSN, dbname=postgres); the fixture creates and drops its own scratch hr_test_* database" >&2
        exit 1
    elif aihr_db_running; then
        setup_aihr_job_db
        job_db=1
    elif [[ -f "$HOME/wiki/docker-compose.yml" ]]; then
        setup_legacy_job_db
        job_db=1
    else
        cat >&2 <<'EOF'
ERROR: --with-db needs a live postgres server to exercise the db-marked
       tests, but found NONE of:
         - $HR_TEST_PG_DSN (admin-level DSN, dbname=postgres; the fixture
           creates and drops its own hr_test_* scratch database), nor
         - a running aihr-db container (provision one with `hr db-up`), nor
         - $HOME/wiki/docker-compose.yml (legacy job-DB path)
       Refusing to silently skip the db tests.
EOF
        exit 1
    fi
    # HR_TEST_DB=1 switches on the env-gated legacy live-DB test, which needs
    # a resolvable db_dsn() — only when a scratch job DB is actually set up.
    if [[ "$job_db" == 1 ]]; then
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
