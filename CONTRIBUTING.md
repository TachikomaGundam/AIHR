# Contributing to AIHR

Thanks for your interest in improving AIHR! This document is the short
version of what a change needs to pass. The repository's CI
(`.github/workflows/ci.yml`) is the authority; the text below mirrors it.

## Development setup

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[test]"
```

Python 3.12+ is required (`requires-python >=3.12`).

## The gates every change must pass

Run them in this order; CI runs the same checks:

```bash
# 1. Lint
.venv/bin/ruff check hr scripts itemrepo

# 2. Types — the CI gate fails on ERRORS only (0 allowed);
#    the recommended-mode warning backlog is tracked separately.
.venv/bin/basedpyright hr scripts

# 3. Tests without a database (fast loop)
.venv/bin/python -m pytest -q

# 4. Full suite including the live-PostgreSQL tests
docker compose -f docker/docker-compose.yml up -d aihr-db
HR_TEST_PG_DSN="postgresql://aihr:<password>@127.0.0.1:5433/postgres" \
  .venv/bin/python -m pytest -q
```

Without `HR_TEST_PG_DSN` the PostgreSQL-gated tests skip silently —
a green offline run is not the full suite.

## Project conventions that carry weight

- **Tracked config ships zero deployment values.** `configs/*.yaml`
  contains policy knobs and commented examples only. Real endpoints,
  keys, and machine-specific values live in gitignored
  `configs/*.local.yaml` overlays (local file wins on merge).
- **The README pair is bilingual with one canonical source.** `README.md`
  (English) is canonical; `README.zh-CN.md` mirrors it section-for-section.
  Keep both in sync in the same commit, and keep the top-of-page language
  switchers symmetric.
- **SQL pinned by guard tests is a contract.** Several tests pin SQL
  strings and CLI command counts verbatim. When you intentionally change
  pinned text, re-pin it with a dated note explaining the change —
  never loosen a pin just to pass.
- **Fail loud, never fake clean.** Unusable inputs exit non-zero with a
  named reason; missing data produces an explicit gap, not a silent zero.
- **Machine state is append-only.** The evolution ledger and measurement
  rows are never rewritten after the fact.

## Pull requests

- One logical change per PR; say which gate output changed.
- New behavior needs a test that fails before the change (name the bug
  it pins).
- CI must be green; the maintainer handles releases and tags.
