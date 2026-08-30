"""Release surface manifest for the HR agent (hr-evolution todo 8).

This module is the machine-readable source of truth for the release surface
decided at commit ``chore: complete HR release manifest`` (on top of f0c2dae).
Todo 9 (release lifecycle) consumes this manifest when building a release;
todo F4 traces every released artifact back to a plan todo.

Design rules
------------
* ``INCLUDED_HR_MODULES`` — the modules shipped in a release build: the
  runtime import closure of the shipped CLI entry points (``hr/__init__.py``,
  ``hr/__main__.py``, ``hr/cli.py`` — the 23-command unified facade), plus
  modules this commit tracks for other consumers (stage facades, committed
  tests). Computed against committed blobs (HEAD), not the dirty working
  tree. At a fresh checkout of this commit every one of them must exist and
  import cleanly; ``test_shipped_cli_runtime_closure_is_tracked_and_included``
  locks the closure-completeness invariant.
* ``EXCLUDED_HR_MODULES`` — every OTHER ``hr/`` python module that exists in
  the working tree. Each has a documented disposition. Nothing is deleted;
  the exclusions are intentional registrations of in-flight work. The
  registry is EMPTY since the hr-ship W1 commit: the release surface
  coincides with the repository surface (tracked tree, no double reality).
* ``GUARDED_IMPORTS`` — imports of EXCLUDED modules that are performed by
  modules that ARE tracked at HEAD, and are deliberately tolerated because
  the importing code degrades gracefully (try/except) or loads lazily inside
  a function body. These are the ONLY legal references from the tracked tree
  to the excluded surface.
* ``TABLE_DISPOSITIONS`` — classification of every table in the 20-table
  schema contract (``tests/test_db.py::EXPECTED_TABLES``): wired feature /
  future contract / removal proposal. No destructive deletion; a removal
  proposal needs separate approval.
* ``RELEASE_ASSETS`` — non-``hr/`` files that must exist at a fresh checkout
  (configs, plugin, scripts, itemrepo contract dirs).

``tests/test_release_surface.py`` verifies the manifest invariants:
included modules import cleanly at a fresh checkout, the tracked tree's
imports resolve inside the manifest, every untracked module is documented,
and the table contract is fully classified.
"""

from __future__ import annotations

import ast
from typing import Dict, List, Set, Tuple

# ---------------------------------------------------------------------------
# Included: modules shipped in a release build (shipped-CLI runtime closure)
# ---------------------------------------------------------------------------

INCLUDED_HR_MODULES: frozenset[str] = frozenset({
    # Shipped CLI entry points (C2: `python3 -m hr` must work from a release
    # candidate alone — no editable-install leakage).
    "hr/__init__.py",                      # package marker; ships in the built candidate
    "hr/__main__.py",                      # ``python -m hr`` entry: from .cli import app; app()
    "hr/cli.py",                           # 23-command unified CLI facade: imports every cluster + register_release_commands
    # CLI command cluster (self-registering onto hr/cli_app.py's shared app)
    "hr/cli_app.py",                       # the shared typer app (no_args_is_help, retirement epilog); imported by every cluster
    "hr/cli_apply.py",                     # apply/status + apply-preview/rollback/backups/prune family
    "hr/cli_knowledge.py",                 # publish/recommend/reference/research commands
    "hr/cli_report_base.py",               # shared report helpers (_fetch/_tag/_verdict_gates/_verdict_seats + health/sweeps builders)
    "hr/cli_report_commands.py",           # verdict/health/sweeps/calibrate commands
    "hr/cli_report_verdict.py",            # build_status_report/build_verdict_report
    "hr/cli_inventory.py",                 # bench/discover/seed commands
    "hr/cli_selection.py",                 # interactive model picker / selection indices
    # Runtime import closure of the entry points above (top-level imports)
    "hr/db.py",                            # connect/init_schema etc.; imported unguarded by the facade and cli_app
    "hr/decision.py",                      # latest_sweep_id/seat_assignments/battery_codes/capability_means/model_capabilities/seat_rows
    "hr/deployable.py",                    # load_deployable (facade + apply.py)
    "hr/config.py",                        # config resolution (cli_app/deployment_manager/apply.py)
    "hr/fleet.py",                         # imported by hr.deployable at module level
    "hr/health.py",                        # HealthReport + sweep_health (cli_app, apply.py, ranker)
    "hr/models.py",                        # pydantic model/view dataclasses (cli_inventory)
    "hr/opencfg.py",                       # opencode config parsing (deployment_manager)
    "hr/plugin_safety.py",                 # backup/rollback primitives (deployment_manager)
    "hr/assign/ranker.py",                 # imported by hr.decision
    "hr/seats/health_gates.py",            # imported by ranker
    "hr/seats/health_policy.py",           # imported by ranker
    "hr/seats/rolespec.py",                # imported by ranker
    "hr/stats/__init__.py",                # imported by ranker (from ..stats import bootstrap)
    "hr/stats/bootstrap.py",               # statistical engine
    "hr/stats/empirical_bernstein.py",     # imported by stats/bootstrap
    "hr/stats/sequential.py",              # imported by stats/bootstrap
    # Tracked with the manifest/closure for other consumers
    "hr/adapters/openai_protocol.py",      # imported by tracked tests/adapters/test_openai_protocol.py
    "hr/bench/engine_results.py",          # imported by tracked hr/bench/engine_storage.py
    "hr/bench/scorer_shared.py",           # imported by included hr/bench/engine_results.py
    "hr/calibration_items.py",             # imported by tracked hr/calibration_runner.py, hr/stage0_storage.py
    "hr/schema_migration.py",              # imported by tracked tests/test_db.py, tests/test_db_contracts.py
    "hr/stage0_call.py",                   # imported by tracked hr/stage0_loop.py
    "hr/stage0_cli.py",                    # release-surface helper: stage0 CLI facade (tracked with tests)
    "hr/stage0_plan.py",                   # imported by included hr/stage0_cli.py
    "hr/stage0_selection.py",              # imported by tracked hr/stage0_loop.py
    "hr/stage0_stats.py",                  # imported by tracked hr/stage0_loop.py
    "hr/stage1_cli.py",                    # release-surface helper: stage1 CLI facade (tracked with tests)
    "hr/stage1_plan.py",                   # imported by included hr/stage1_cli.py
    "hr/stage1_selection.py",              # imported by tracked tests/test_sequential_validity.py
    "hr/db_schema.py",                     # unified-era 20-table hr-schema DDL; imported unguarded by tracked hr/db.py
    "hr/deployment_manager.py",            # todo 9 release lifecycle core module; ships because the facade imports register_release_commands
    "hr/release_manifest.py",              # the manifest file itself: ships in every release because deployment_manager imports it at runtime
    # Unification-era modules landed with the hr-ship W1 commit: the former
    # worktree-only surface is now tracked, so the release surface coincides
    # with the repository surface (no double reality).
    "hr/adapters/anthropic_messages.py",   # Anthropic wire-format message encoder (stdlib-only)
    "hr/adapters/anthropic_stream.py",     # Anthropic SSE streaming response decoder (httpx + adapters.base + graders.base)
    "hr/adapters/openai_endpoint.py",      # OpenAI-compatible endpoint client (adapters.base + fleet)
    "hr/bench/engine_interactive.py",      # interactive single-item benchmark engine (adapter-backed)
    "hr/bench/engine_runners.py",          # battery runner family (score_* from scorers + graders.base)
    "hr/bench/prompt_image.py",            # PNG -> base64 prompt image payload (stdlib-only)
    "hr/bench/scorer_attention.py",        # attention-probe scorer (scorer_shared + stress_prompts)
    "hr/bench/scorer_code.py",             # hidden-Python-test scorer running code through hr.sandbox
    "hr/bench/scorer_instruction.py",      # instruction-following constraint scorer (scorer_shared)
    "hr/bench/scorer_reasoning.py",        # runtime-truth math/number-theory scorer (scorer_shared)
    "hr/bench/scorer_runtime.py",          # runtime-verified artifact scorer (scorer_shared)
    "hr/calibration_cli.py",               # calibration command cluster (calibration_runner + config)
    "hr/config_resources.py",              # shipped yaml config files resolved as package resources
    "hr/fleet_policy.py",                  # fleet scope policy (yaml + config.load_yaml)
    "hr/graders/constraint_dsl.py",        # constraint-DSL evaluator (graders.base)
    "hr/health_metrics.py",                # behavioral health metrics (stdlib + decimal)
    "hr/items/payloads.py",                # typed benchmark item payloads (pydantic)
    "hr/sandbox.py",                       # subprocess code sandbox backing scorer_code
    # Vision item authoring toolchain (itemrepo/vision). Script-mode modules:
    # they import each other by bare sibling name, so the import closure
    # resolves only with the vision directory on sys.path (their tools run
    # that way: build.py / regenerate_verify.py). Pillow is an OPTIONAL
    # dependency (the ``[vision]`` extra) — drawing primitives load lazily
    # inside the generators, so the import closure works without Pillow
    # installed; only regeneration (an authoring action) needs the extra.
    "itemrepo/vision/vision_chart_core.py",
    "itemrepo/vision/vision_chart_dense.py",
    "itemrepo/vision/vision_draw.py",
    "itemrepo/vision/vision_registry.py",
    "itemrepo/vision/vision_registry_chart.py",
    "itemrepo/vision/vision_registry_schematic.py",
    "itemrepo/vision/vision_registry_ui.py",
    "itemrepo/vision/vision_schematic_flow.py",
    "itemrepo/vision/vision_schematic_network.py",
    "itemrepo/vision/vision_tier3.py",
    "itemrepo/vision/vision_ui_core.py",
    "itemrepo/vision/vision_ui_dense.py",
})

# ---------------------------------------------------------------------------
# Excluded: every other hr/*.py present in the working tree, with disposition
# ---------------------------------------------------------------------------

# Every module that used to be registered here has landed in
# INCLUDED_HR_MODULES with the hr-ship W1 commit (the 18 unified-era modules
# and the 12 vision-toolchain modules above). Exclusions are an intentional
# registration of in-flight work; now that the release surface coincides with
# the repository surface there is nothing in flight, so the registry is empty.
# Future untracked work registers here again until it lands.
EXCLUDED_HR_MODULES: Dict[str, str] = {}

# (importer, imported) pairs tolerated by the release surface even though the
# imported module is excluded. Empty since the hr-ship W1 commit: the legacy
# hr/database.py layer is DELETED (its importers were rewired to hr.db in the
# same commit), so no tracked module references an excluded module anymore.
GUARDED_IMPORTS: List[Tuple[str, str]] = []

# ---------------------------------------------------------------------------
# Dead/edge tables in the 20-table schema contract (tests/test_db.py
# EXPECTED_TABLES). Classification per table, with evidence. No destructive
# deletion: a removal_proposal needs separate approval (plan scope: Out).
# ---------------------------------------------------------------------------

TABLE_DISPOSITIONS: Dict[str, Dict[str, str]] = {
    "provider": {"class": "wired", "evidence": "tracked hr/stage0_storage.py, hr/discover.py, hr/cli.py write/read"},
    "model": {"class": "wired", "evidence": "tracked adapters/engine/ranker/deployable/registry write and read"},
    "control_model": {"class": "wired", "evidence": "tracked contract helpers seed; engine control probes read"},
    "seat": {"class": "wired", "evidence": "tracked hr/decision.py seat_rows; stage0_storage upsert"},
    "item_pool": {"class": "wired", "evidence": "tracked hr/stage0_storage.py, hr/benchmark_banks.py, hr/bank_versions.py"},
    "item": {"class": "wired", "evidence": "tracked stage0/benchmark_banks/scorer_calibration read; seeds write"},
    "battery": {"class": "wired", "evidence": "tracked hr/stage0_storage.py, hr/bench/engine.py, hr/bench/livebench.py"},
    "battery_item": {"class": "wired", "evidence": "tracked hr/stage0_storage.py, hr/bench/engine.py, hr/bank_versions.py"},
    "seat_battery": {"class": "wired", "evidence": "tracked hr/stage0_storage.py upsert"},
    "sweep": {"class": "wired", "evidence": "tracked stage0/stage1/decision/recommend/apply read and write"},
    "experiment_manifest": {"class": "wired", "evidence": "tracked hr/bench/engine_storage.py writes experiment envelope"},
    "run": {"class": "wired", "evidence": "tracked engine/benchmark_banks/decision/health/calibration read and write"},
    "measurement": {"class": "wired", "evidence": "tracked engine/stage0/stage1/decision/health read and write"},
    "infra_incident": {"class": "wired", "evidence": "tracked hr/stage0_storage.py writes incident rows"},
    "control_reading": {"class": "removal_proposal", "evidence": "DDL-only: no reader or writer anywhere in the tree (tracked or untracked); the concept appears only as a dataclass in tracked hr/scheduler/taxonomy.py; propose dropping with the schema migration, needs separate approval"},
    "separation": {"class": "wired", "evidence": "tracked hr/decision.py, hr/stage0_storage.py, hr/plugin_safety.py read and write"},
    "assignment": {"class": "wired", "evidence": "tracked hr/cli.py, hr/db.py read; apply writes (worktree)"},
    "policy_override": {"class": "removal_proposal", "evidence": "DDL-only: no reader or writer anywhere in the tree (tracked or untracked); propose dropping with the schema migration, needs separate approval"},
    "calibration_event": {"class": "wired", "evidence": "tracked hr/calibration_persistence.py writes calibration events"},
    "judge_verdict": {"class": "future_contract", "evidence": "no tracked reader/writer at HEAD; uniqueness contract tested in tracked tests/test_db.py; the worktree llm_judge grader sets judge_verdict_fk — wired feature pending the unification commit"},
}

# ---------------------------------------------------------------------------
# Non-hr/ release assets that must exist at a fresh checkout
# ---------------------------------------------------------------------------

RELEASE_ASSETS: List[str] = [
    "pyproject.toml",
    "README.md",
    "README.zh-CN.md",
    "configs/deployable.yaml",
    "configs/fleet.yaml",
    "configs/hr.toml.example",
    "configs/knowledge.yaml",
    "configs/models.yaml",
    "configs/seats.yaml",
    "configs/thresholds.yaml",
    "opencode_plugin/package.json",
    "opencode_plugin/server.ts",
    "fastdraw/package.json",
    "fastdraw/server.ts",
    "fastdraw/install.sh",
    "fastdraw/origins.ts",
    "fastdraw/roles.ts",
    "fastdraw/tui.ts",
    "scripts/register_livebench_batteries.py",
    "scripts/register_tool_b_battery.py",
    "scripts/seed_seats.py",
    "scripts/spread_probe.py",
    "scripts/test.sh",
    "scripts/check_universal.sh",
]

# Reasoning item contract dirs that the one-to-one registry-vs-disk test
# (tests/items/test_items.py, locked at T5) protects. Each glob must match
# at least one tracked file at a fresh checkout.
ITEMREPO_CONTRACT_GLOBS: Dict[str, str] = {
    "reasoning_registry": "itemrepo/reasoning/reasoning_registry.py",
    "reasoning_t3_items": "itemrepo/reasoning/t3/reason.t3.*.json",
    "reasoning_t4_items": "itemrepo/reasoning/t4/reason.t4.*.json",
    "vision_items": "itemrepo/vision/vision.*.json",
}


# ---------------------------------------------------------------------------
# Import-graph helpers shared with tests/test_release_surface.py
# ---------------------------------------------------------------------------

def imports_in_src(src: str) -> List[Tuple[str, int, int, bool, Tuple[str, ...]]]:
    """Return ``(module, level, line_no, guarded, names)`` for every import.

    ``level`` is 0 for absolute imports and >0 for relative imports
    (1 = current package). ``guarded`` is True when the import sits inside
    the body of a try statement (graceful-degradation pattern). ``names``
    are the imported names of an ImportFrom (empty for ``import X`` and
    ``from X import *``) and let namespace-package imports such as
    ``from scripts import register_tool_b_battery`` resolve to the real
    submodule file.
    """
    out: List[Tuple[str, int, int, bool, Tuple[str, ...]]] = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out

    guarded_ids: Set[int] = set()
    for try_node in ast.walk(tree):
        if isinstance(try_node, ast.Try):
            for stmt in try_node.body:
                for stmt_node in ast.walk(stmt):
                    guarded_ids.add(id(stmt_node))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((alias.name, 0, node.lineno, id(node) in guarded_ids, ()))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names = tuple(a.name for a in node.names if a.name != "*")
                out.append((node.module, node.level, node.lineno, id(node) in guarded_ids, names))
    return out


def resolve_import(
    module: str, level: int, importer_path: str, names: Tuple[str, ...] = ()
) -> List[str]:
    """Resolve an import statement to candidate repo-relative file paths.

    ``importer_path`` is the repo-relative path of the importing file and is
    used for relative imports. Every dotted name can denote a module file
    ``<name>.py`` OR a package ``<name>/__init__.py`` — both candidates are
    returned and a caller accepts the import if any candidate is part of the
    release surface. Absolute imports are resolved only for the
    hr./itemrepo./scripts. trees; everything else (stdlib, third-party, flat
    sibling imports) resolves to an empty list and is out of scope. Names
    from an ImportFrom add ``<pkg>/<name>.py`` candidates so namespace
    packages (no ``__init__.py``, e.g. scripts/) resolve to their member.
    """
    candidates: List[str] = []
    if level > 0:
        base = importer_path.rsplit("/", 1)[0] if "/" in importer_path else ""
        for _ in range(level - 1):
            base = base.rsplit("/", 1)[0] if "/" in base else ""
        rel = f"{base}/{module}".replace("//", "/") if base else module
        candidates = _candidate_paths(rel)
    elif module.split(".", 1)[0] in ("hr", "itemrepo", "scripts", "tests"):
        candidates = _candidate_paths(module)
    # Names from an ImportFrom add member-file candidates only for the one
    # namespace package (scripts/ has no __init__.py; every other root is a
    # real package whose module candidates already resolve).
    if names and module.split(".", 1)[0] == "scripts":
        candidates.extend(f"{module.replace('.', '/')}/{name}.py" for name in names)
    return candidates


def _candidate_paths(dotted: str) -> List[str]:
    rel = dotted.replace(".", "/")
    out = []
    for candidate in (f"{rel}.py", f"{rel}/__init__.py"):
        if candidate.startswith(("hr/", "itemrepo/", "scripts/")):
            out.append(candidate)
    return out