# HR: Agent Seat Matching and Capability Benchmarking

Unified harness that matches autonomous LLM coding agents to task-appropriate seats, runs capability benchmarks across model fleets, and emits deployment verdicts. Single editable install. 13 CLI commands. Zero runtime API keys for the CLI itself.

The English version is canonical. The Chinese version (`README.zh-CN.md`) is a faithful mirror. When the two diverge, the English text governs.

## Install

```bash
pip install -e .
```

Editable installs are the only supported mode. Wheel or binary installs break path-dependent config resolution. If an older `hr-cli` or `hr-bench` package is already installed, remove it first:

```bash
pip uninstall hr-cli hr-bench -y
pip install -e .
```

### Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `HR_DSN` | Override the PostgreSQL connection string (preferred over `HR_DB_PASSWORD` + `db_*` fields) | unset |
| `HR_HOME` | Force an alternate config root (configs/, `hr.toml`, `itemrepo` are resolved from here) | repo root |
| `HR_COMPOSE_FILE` | Override the `docker compose` manifest that DB password resolution probes | unset |
| `HR_ITEMREPO` | Override the benchmark item repo directory | `HR_HOME/itemrepo` |

### Configuration

Copy the example `hr.toml` if you need the DB / Wiki.js knobs:

```bash
cp configs/hr.toml.example hr/hr.toml
```

There is no single "source of truth" file — configuration is split by concern across `configs/`, plus the runtime opencode config:

### Local overlays (`configs/*.local.yaml`)

The tracked configs ship ZERO real deployment values (placeholder examples
where a value is machine-specific). A live machine's real values live in the
gitignored local overlays — `configs/seats.local.yaml`, `configs/fleet.local.yaml`,
`configs/deployable.local.yaml`, `configs/models.local.yaml` — which
`hr.config.load_yaml` deep-merges over the tracked files automatically:
**local wins per key; dicts merge recursively; lists are replaced, never
merged.** A missing overlay is normal (the tracked file is used as-is).

First-install: `cp configs/seats.yaml configs/seats.local.yaml`,
`cp configs/fleet.yaml configs/fleet.local.yaml`,
`cp configs/deployable.yaml configs/deployable.local.yaml` (and
`configs/models.yaml` → `models.local.yaml` if your gateway facts differ),
then fill in your real anchors, wire overrides, gateway URLs and
`extra_deployable` list. Never edit deployment values into the tracked
files — *any* `.local.yaml` is safe for real values, *nothing else* is.

The per-file split:

- `configs/thresholds.yaml` — numeric sweep and gate thresholds (stage0 budgets, half-widths, acceptance bands).
- `configs/models.yaml` — model pricing and the capability overlay (thinking/vision), keyed by bare model slug; unknown models get safe defaults.
- `configs/knowledge.yaml` — curated reference scores and qualitative research findings, keyed by bare model slug (unknown models are skipped; seed with `hr reference --seed` / `hr research --seed`).
- `configs/fleet.yaml` — OPTIONAL overrides for the dynamic fleet: `wire_overrides`, `scope_excludes`, and `gateway_urls` (base URLs for registry-only providers).
- `configs/seats.yaml` — seat definitions, per-seat `primary_capabilities`, and the stage-0 `calibration_anchors`.
- `configs/deployable.yaml` — `extra_deployable`: models served outside the opencode config (the only hand-maintained model list).
- `configs/hr.toml.example` — template for the root `hr.toml` (DB connection + optional Wiki.js publish target). Secrets are NEVER stored here: they come from the environment (`HR_DSN`, `HR_DB_PASSWORD`, provider keys).

The model fleet itself is not declared in this repo: it is derived at runtime from the opencode config (`opencode.jsonc` provider blocks) and merged with the `deployable.yaml` extras — see Universality below.

## CLI Map

Thirteen commands, each targeting a specific concern. Legacy v1 commands (`evaluate`, `report`, `run_all`) were retired. `hr verdict` supersedes the retired evaluation path.

| Command | Purpose |
|---------|---------|
| `hr discover` | Enumerate providers/models from `opencode.jsonc` into hr2 (scope + auth presence) |
| `hr seed` | Seed the database with research findings and reference scores (v1 legacy path) |
| `hr bench` | Run the 10 live capability benchmarks and record `hr2.measurement` rows |
| `hr verdict` | Comprehensive verdict: capability averages + health + gates + assignment |
| `hr health` | Full-pool behavioral-health markdown table (DB-only, zero API calls) |
| `hr sweeps` | List sweeps from the DB with run/model/measurement counts |
| `hr calibrate` | Stage-0 anchor calibration engine (dry-run planning + live API passes) |
| `hr reference` | Curated published-benchmark scores per model (`--seed` upserts into `hr_reference`) |
| `hr research` | Qualitative findings per model (`--seed` writes into `hr_research`) |
| `hr publish` | Publish reports to Wiki.js (optional target; skips with exit 0 when unconfigured) |
| `hr recommend` | Seat recommendations from `configs/seats.yaml` + recent measurements |
| `hr status` | DB status: sweeps + latest-sweep capability means (DB-only) |
| `hr apply` | Bridge the latest verdict seating into a FastDraw preset |

The CLI has no global `--config` flag: configuration is resolved from the environment (see the table above) and from `configs/` relative to HR_HOME. Run `hr --help` and `hr <command> --help` for the full per-command flag list.

## FastDraw Seam

FastDraw is the model-selection subpackage bundled at `fastdraw/`. It provides TUI-based preset management for agent model assignments and integrates with opencode through `hr apply`.

### Subpackage Layout

```
fastdraw/
  server.ts     # FastDraw HTTP server (preset API)
  tui.ts        # Terminal UI for preset management
  package.json  # npm manifest (standalone install)
  test/         # Test suite
  README.md     # FastDraw-specific documentation
```

### The `hr apply` Contract

`hr apply` is the bridge between FastDraw presets and the opencode runtime. It works in three steps:

1. Reads a FastDraw preset from `~/.config/opencode/fastdraw/presets.json`
2. Writes the model assignments into opencode's registered config files (see below)
3. Prints a restart hint so opencode picks up the new assignments

### Dual-File Registration

Two opencode files must point FastDraw at the correct model catalog paths. The FastDraw server reads from these two locations:

| File | Key | Value |
|------|-----|-------|
| `.opencode/opencode.jsonc` | `models.catalog` | `../configs/models.yaml` |
| `.opencode/tui.json` | `models.catalog` | `../configs/models.yaml` |

Both entries resolve to the same `configs/models.yaml` in the hr tree. This dual registration is intentional and must stay consistent.

## Layout

```
harness/hr/               # repo root (pip install -e .)
  configs/                # YAML config: deployable.yaml, fleet.yaml, hr.toml.example, knowledge.yaml, models.yaml, seats.yaml, thresholds.yaml (+ gitignored *.local.yaml overlays)
  docs/                   # bilingual documentation (en/, zh-CN/)
  exports/                # generated artifacts (gitignored)
  fastdraw/               # npm subpackage: FastDraw server, TUI, preset management
  hr/                     # Python package: the CLI and all business logic
    adapters/             # provider adapters (anthropic-compat, openai-compat) + fleet routing
    bench/                # benchmark batteries + stage0/stage1 sweep engines
    graders/              # grading functions (factuality, reasoning, vision, tools)
    items/                # item loaders for benchmark questions
    scheduler/            # task scheduling (kept per Metis C1)
    seats/                # seat taxonomy and profile helpers
    stats/                # statistical aggregation for sweep results
  itemrepo/               # git-versioned benchmark item repository by category
  scripts/                # operational scripts (check_universal.sh, register_livebench_batteries.py, spread_probe.py, ...)
  tests/                  # pytest test suite
  pyproject.toml          # package manifest with CLI entry point
```

## Tests

```bash
python -m pytest tests/ -q
```

All tests run offline against hermetic fixtures (tmp dirs, no database, no network). The current suite is **567 passed, 9 skipped, 0 failed** (576 collected). Stage0 tests cover the capability-prior pipeline, tier-aware thresholds, and the verdict ranker. Bench tests cover the discovery loader, battery resolver, and the stage0 budget contract.

Live API bench runs need real provider credentials from the opencode config:

```bash
hr bench --model gpt-4o --battery reasoning
```

## Universality

This codebase targets the general class of autonomous LLM coding agents, not a specific product. The seat taxonomy (tier 1 through tier 4), the benchmark item categories (factuality, reasoning, vision, tool_a, tool_b), and the verdict pipeline (discover, bench, assign, verdict) apply to any agent that consumes LLM output and produces code artifacts.

Provider-specific hardcoding was removed during unification. The model fleet is derived at RUNTIME from opencode's live config (`opencode.jsonc` provider blocks: every `provider.*.models` entry becomes a fleet model, and the `npm` field derives the wire type); `configs/fleet.yaml` holds only OPTIONAL overrides (`wire_overrides` for registry-only providers, `scope_excludes`, `gateway_urls`), and `configs/deployable.yaml` `extra_deployable` is the only hand-maintained model list (models served outside the opencode config). Add a model to opencode's config and it flows into the sweep pools, discover and routing with zero file edits here. Knowledge data lives in `configs/models.yaml` (pricing/capabilities) and `configs/knowledge.yaml` (reference scores, findings), both with safe defaults for unknown models.

## License

See `LICENSE`.