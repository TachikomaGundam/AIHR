#!/usr/bin/env bash
# scripts/test.sh — canonical test entrypoint for the hr engine (plan T5).
#
# Modes:
#   scripts/test.sh             offline: full suite + coverage (fail-under 80)
#                               + universality gate — no DB environment needed.
#   scripts/test.sh --with-db   offline suite PLUS the db-marked tests against a
#                               live postgres server. Requires either $HR_DSN
#                               (point it at a SCRATCH database) or the local
#                               wiki compose file at $HOME/wiki/docker-compose.yml;
#                               with the compose file a fresh scratch job DB
#                               hr_test_<timestamp> is created and always dropped
#                               by an exit trap. The wiki database is never a
#                               DSN target.
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
              server. Requires $HR_DSN (point it at a SCRATCH database) or
              $HOME/wiki/docker-compose.yml; with the compose file a fresh
              scratch job DB hr_test_<timestamp> is created and always dropped
              by an exit trap. The wiki database is never a DSN target.
  --ci        exact ci.yml step-order simulation (compileall, ruff,
              basedpyright, pytest + coverage, universality gate).

Any other argument prints this usage and exits 1.
EOF
}

run_offline_suite() {
    python3 -m pytest --cov=hr --cov-report=term-missing --cov-fail-under=80 -q
}

run_universality_gate() {
    bash scripts/check_universal.sh
}

drop_job_db() {
    docker exec wiki-db psql -U wikijs -d postgres -Atc \
        "DROP DATABASE IF EXISTS \"${JOB_DB}\" WITH (FORCE)" >/dev/null 2>&1 || true
    echo "== --with-db: dropped scratch job DB ${JOB_DB} =="
}

run_with_db() {
    JOB_DB=""
    if [[ -n "${HR_DSN:-}" ]]; then
        dsn_db="${HR_DSN##*/}"
        dsn_db="${dsn_db%%\?*}"
        if [[ "$dsn_db" == "wiki" ]]; then
            echo "ERROR: HR_DSN points at the wiki database ($dsn_db) — --with-db must target a SCRATCH database" >&2
            exit 1
        fi
        echo "== --with-db: using HR_DSN override (caller-provided SCRATCH db) =="
    elif [[ -f "$HOME/wiki/docker-compose.yml" ]]; then
        export HR_COMPOSE_FILE="$HOME/wiki/docker-compose.yml"
        JOB_DB="hr_test_$(date +%s)"
        echo "== --with-db: HR_COMPOSE_FILE=$HR_COMPOSE_FILE =="
        if ! docker exec wiki-db psql -U wikijs -d postgres -Atc \
            "CREATE DATABASE \"${JOB_DB}\" OWNER wikijs" >/dev/null 2>&1; then
            echo "ERROR: could not create scratch job DB ${JOB_DB} via the wiki-db container" >&2
            echo "       is the postgres server running? (docker exec wiki-db psql ...)" >&2
            exit 1
        fi
        export HR_DB_NAME="$JOB_DB"
        trap drop_job_db EXIT
    else
        cat >&2 <<'EOF'
ERROR: --with-db needs a live postgres server to exercise the db-marked
       tests, but found NEITHER:
         - $HR_DSN (point it at a SCRATCH database), nor
         - $HOME/wiki/docker-compose.yml
       Refusing to silently skip the db tests.
EOF
        exit 1
    fi
    export HR_TEST_DB=1
    # Belt and braces: keep coverage data out of the repo tree even though
    # the cleanliness guard ignores .coverage artifacts.
    export COVERAGE_FILE="/tmp/${JOB_DB:-hr-with-db}-$$.coverage"
    run_offline_suite
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
    python3 -m pytest --cov=hr --cov-report=term-missing --cov-report=xml -q
    echo "== --ci: universality gate =="
    run_universality_gate
}

case "${1:-}" in
    "")
        run_offline_suite
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