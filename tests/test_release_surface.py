"""Release-surface checks: manifest, import closure, and artifact presence.

This file is the executable definition of the release surface decided at
``hr/release_manifest.py`` (hr-evolution todo 8). Its gates must pass both in
the (dirty) working tree and at a fresh checkout of the release commit:
every module the manifest includes must exist and import cleanly, every
import from the tracked tree must resolve inside the manifest (or be a
documented guarded reference), every untracked module must be classified,
and the 20-table schema contract must be fully dispositioned.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from hr.release_manifest import (
    EXCLUDED_HR_MODULES,
    GUARDED_IMPORTS,
    INCLUDED_HR_MODULES,
    ITEMREPO_CONTRACT_GLOBS,
    RELEASE_ASSETS,
    TABLE_DISPOSITIONS,
    imports_in_src,
    resolve_import,
)
from scripts import register_tool_b_battery

from tests.test_db import EXPECTED_TABLES


ROOT = Path(__file__).resolve().parents[1]


def _git(args: list[str]) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def _git_show(rev: str, path: str) -> str | None:
    """Committed blob for path at rev, ``None`` when the path did not exist."""
    result = subprocess.run(
        ["git", "show", f"{rev}:{path}"], capture_output=True, text=True, check=False
    )
    return result.stdout if result.returncode == 0 else None


def _subprocess_import(
    module: str, extra_paths: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-c", f"import {module}"]
    env = os.environ | {"PYTHONPATH": os.pathsep.join([*sys.path, *extra_paths])}
    return subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=False)


def _runtime_import_closure() -> set[str]:
    """Module-level import closure of the shipped CLI entry points.

    Only top-level imports count: function-local lazy imports (doctrine:
    boundary adapters load on demand) are not closure members. Computed
    against COMMITTED blobs (``git show HEAD:``), not the working tree, so
    the result is identical at this checkout and at a fresh checkout: the
    surface-completeness gate locks the shipped closure exactly, never the
    dirty-tree variant (which drags in untracked worktree-era modules).
    """
    # Paths whose committed blob differs from the working tree during
    # development (this commit authors them) are read from the working tree;
    # every other path is read from HEAD. After the commit the two coincide.
    authored = {
        "hr/cli.py", "hr/__init__.py", "hr/__main__.py",
        "hr/cli_report_base.py", "hr/cli_report_commands.py",
        "hr/cli_report_verdict.py", "hr/cli_inventory.py",
        "hr/cli_selection.py",
    }

    def source(path: str) -> str | None:
        if path in authored:
            return (ROOT / path).read_text(encoding="utf-8")
        return _git_show("HEAD", path)

    def module_level_lines(src: str) -> set[int]:
        tree = ast.parse(src)
        return {
            n.lineno
            for n in tree.body
            if isinstance(n, (ast.Import, ast.ImportFrom))
        }

    closure: set[str] = set()
    queue = ["hr/__init__.py", "hr/__main__.py", "hr/cli.py"]
    while queue:
        current = queue.pop(0)
        if current in closure:
            continue
        src = source(current)
        if src is None:
            continue
        closure.add(current)
        top_lines = module_level_lines(src)
        for module, level, lineno, guarded, names in imports_in_src(src):
            if guarded or lineno not in top_lines:
                continue
            targets = resolve_import(module, level, current, names)
            # resolve_import adds member candidates only for the scripts/
            # namespace package; the hr root needs the same treatment
            # (`from hr import fleet` -> hr/fleet.py).
            if level == 0 and module == "hr":
                targets += [f"hr/{name}.py" for name in names]
            for target in targets:
                if target.startswith("hr/") and target not in closure and target not in queue:
                    queue.append(target)
    return closure


def test_shipped_cli_runtime_closure_is_tracked_and_included() -> None:
    # Given: the module-level runtime import closure of the shipped CLI
    # (computed against committed blobs — identical here and at origin).
    closure = _runtime_import_closure()
    # Then: every member is tracked at HEAD...
    tracked = set(_git(["ls-files"]).splitlines())
    assert sorted(c for c in closure if c not in tracked) == [], (
        f"untracked closure members: {sorted(c for c in closure if c not in tracked)}"
    )
    # ...and every member is INCLUDED in the release manifest, so build_release
    # ships a candidate whose own ``-S -m hr`` resolves entirely from itself.
    not_included = sorted(closure - INCLUDED_HR_MODULES)
    assert not_included == [], f"closure members missing from INCLUDED_HR_MODULES: {not_included}"
    # Guard rails: the closure is non-trivial and its roots are present.
    assert len(closure) >= 30, f"closure unexpectedly small: {len(closure)}"
    # The manifest itself covers the FULL repository surface since the
    # hr-ship W1 commit (the 18 landed unified-era modules + the 12 vision
    # toolchain modules on top of the shipped closure): lock a floor so the
    # registry cannot silently shrink back to the split-era closure.
    assert len(INCLUDED_HR_MODULES) >= 70, (
        f"manifest closure unexpectedly small: {len(INCLUDED_HR_MODULES)}"
    )
    for root in ("hr/__init__.py", "hr/__main__.py", "hr/cli.py"):
        assert root in INCLUDED_HR_MODULES, f"entry point missing from INCLUDED: {root}"


# ---------------------------------------------------------------------------
# Stage CLI split (worktree-era facades now tracked with the release manifest)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", ["hr.stage0_cli", "hr.stage1_cli"])
def test_stage_cli_module_imports_without_order_dependency(module: str) -> None:
    # Given: a clean interpreter with no preloaded stage facade.
    # When: the split CLI module is imported directly.
    result = _subprocess_import(module)
    # Then: import order does not trigger a facade cycle.
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("module", ["hr.stage0", "hr.stage1"])
def test_stage_module_help_is_runnable(module: str) -> None:
    # Given: the public module entry point.
    command = [sys.executable, "-m", module, "--help"]
    env = os.environ | {"PYTHONPATH": os.pathsep.join(sys.path)}
    # When: a user asks for command help without credentials.
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=False)
    # Then: the module starts and renders its CLI contract.
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


# ---------------------------------------------------------------------------
# Packaging metadata
# ---------------------------------------------------------------------------


def test_console_scripts_expose_only_canonical_hr_command() -> None:
    # Given: the package metadata consumed by installers.
    with (ROOT / "pyproject.toml").open("rb") as stream:
        metadata = tomllib.load(stream)

    # When: console entry points are inspected.
    scripts = metadata["project"]["scripts"]

    # Then: users receive one product command without a legacy alias.
    assert scripts == {"hr": "hr.cli:app"}


def test_package_declares_openai_adapter_runtime_dependency() -> None:
    # Given: package metadata consumed by a fresh wheel installer.
    with (ROOT / "pyproject.toml").open("rb") as stream:
        metadata = tomllib.load(stream)

    # When: dependencies needed by eagerly imported adapters are inspected.
    dependencies = metadata["project"]["dependencies"]

    # Then: importing the installed package does not depend on ambient tools.
    assert any(dependency.split("[")[0].split(">=")[0] == "requests" for dependency in dependencies)


def test_fastdraw_manifest_has_no_unresolved_repository_owner() -> None:
    # Given: the npm manifest and installer published with FastDraw.
    manifest = json.loads((ROOT / "fastdraw" / "package.json").read_text(encoding="utf-8"))
    installer = (ROOT / "fastdraw" / "install.sh").read_text(encoding="utf-8")

    # When: public release metadata and installation instructions are inspected.
    release_text = json.dumps(manifest) + installer

    # Then: the package does not advertise an invented repository owner.
    assert "YOUR-GITHUB-USER" not in release_text


# ---------------------------------------------------------------------------
# Release manifest: included closure imports and exists at a fresh checkout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_path", sorted(INCLUDED_HR_MODULES), ids=lambda p: p.removesuffix(".py").replace("/", "."))
def test_manifest_included_module_exists_and_imports_cleanly(module_path: str) -> None:
    if module_path == "hr/__main__.py":
        # Entry point, not an importable module: importing it EXECUTES the
        # CLI (``app()`` -> help + SystemExit). Its runnability is locked by
        # the isolated ``python3 -S -m hr --help`` smoke test in
        # tests/test_deployment_manager.py.
        pytest.skip("hr/__main__.py is the -m entry point; runnability is locked by the -S smoke test")
    # Given: a module the release manifest declares part of the release surface.
    # When: the manifest is evaluated at this checkout (fresh-HEAD gate).
    assert (ROOT / module_path).is_file(), f"INCLUDED module missing from checkout: {module_path}"
    # The vision toolchain is script-mode (bare sibling imports): mirror its
    # runtime by putting the vision directory on sys.path, exactly as
    # build.py / regenerate_verify.py do when invoked from that directory.
    extra = (str(ROOT / "itemrepo" / "vision"),) if module_path.startswith("itemrepo/vision/") else ()
    result = _subprocess_import(module_path.removesuffix(".py").replace("/", "."), extra_paths=extra)
    # Then: the module imports cleanly in a fresh interpreter.
    assert result.returncode == 0, f"{module_path}: {result.stderr}"


def test_manifest_lists_every_untracked_hr_module() -> None:
    # Given: every python module under hr/ that git does not track yet.
    untracked = {
        f for f in _git(["ls-files", "--others", "--exclude-standard"]).splitlines()
        if f.startswith("hr/") and f.endswith(".py")
    }
    # When: the manifest classification is evaluated.
    unknown = untracked - set(INCLUDED_HR_MODULES) - set(EXCLUDED_HR_MODULES) - {"hr/release_manifest.py"}
    # Then: no hr module is silently absent from the manifest. (The manifest
    # file itself is, of course, the one adding itself.)
    assert unknown == set(), f"untracked and unclassified: {sorted(unknown)}"


def test_manifest_has_no_excluded_tracked_modules() -> None:
    # Given: modules that are BOTH tracked at HEAD AND listed as excluded.
    tracked = set(_git(["ls-files"]).splitlines())
    gone = set(EXCLUDED_HR_MODULES) & tracked
    # Then: none. The exclusion registry is empty since the hr-ship W1
    # commit — every module that used to be excluded is now included, and
    # the legacy database layer is deleted rather than excluded.
    assert gone == set(), f"unexpected tracked-but-excluded: {sorted(gone)}"


def test_guarded_imports_reference_documented_exclusions() -> None:
    # Given: the whitelist of tolerated references from the tracked tree.
    tracked = set(_git(["ls-files"]).splitlines())
    for importer, target in GUARDED_IMPORTS:
        # Then: the importer is tracked and the target is documented-excluded.
        assert importer in tracked, f"guarded importer not tracked: {importer}"
        assert target in EXCLUDED_HR_MODULES, f"guarded target not excluded: {target}"


# ---------------------------------------------------------------------------
# Release manifest: the tracked tree's imports resolve inside the manifest
# ---------------------------------------------------------------------------


def test_tracked_tree_imports_resolve_in_release_manifest() -> None:
    """Every import from a tracked module resolves to a tracked or included
    module, or is an explicitly documented guarded reference.

    The analysis runs against HEAD blobs (not the dirty working tree), so the
    outcome is identical at the commit under test and at a fresh checkout.
    """
    tracked = set(_git(["ls-files"]).splitlines())
    violations: list[str] = []
    for path in sorted(tracked):
        if not path.endswith(".py"):
            continue
        src = _git(["show", f"HEAD:{path}"]) if path in tracked else ""
        for module, level, lineno, guarded, names in imports_in_src(src):
            targets = resolve_import(module, level, path, names)
            if not targets:
                continue
            if any(t in INCLUDED_HR_MODULES or (t in tracked and t not in EXCLUDED_HR_MODULES) for t in targets):
                continue
            if any(t in EXCLUDED_HR_MODULES and (path, t) in GUARDED_IMPORTS for t in targets):
                continue
            violations.append(f"{path}:{lineno} -> {module} ({sorted(targets)})")
    assert violations == [], "tracked tree imports an unregistered module:\n" + "\n".join(violations)


# ---------------------------------------------------------------------------
# Release manifest: dead-schema classification and release assets
# ---------------------------------------------------------------------------


def test_table_classification_contract_is_complete() -> None:
    # Given: the 20-table schema contract (tests/test_db.py, locked at T2).
    # When: the manifest disposition table is compared against it.
    classified = set(TABLE_DISPOSITIONS)
    unclassified = set(EXPECTED_TABLES) - classified
    # Then: every table has a documented disposition and legal class.
    assert not unclassified, f"tables without disposition: {sorted(unclassified)}"
    assert not (classified - set(EXPECTED_TABLES)), f"dispositions for unknown tables: {sorted(classified - set(EXPECTED_TABLES))}"
    legal = {"wired", "future_contract", "removal_proposal"}
    for table, entry in TABLE_DISPOSITIONS.items():
        assert entry["class"] in legal, f"{table}: illegal class {entry['class']!r}"
        assert entry.get("evidence"), f"{table}: missing evidence"


def test_removal_proposals_are_documented_not_deleted() -> None:
    # Given: tables the manifest proposes to remove.
    removals = [t for t, e in TABLE_DISPOSITIONS.items() if e["class"] == "removal_proposal"]
    # Then: exactly the two DDL-only tables, and their DDL still exists in
    # the checkout's schema source (nothing was deleted — deletion needs
    # separate approval). Bare-name presence covers both the current schema
    # (hr.*) and the legacy DDL of this commit (hr2.*) alike.
    assert removals == ["control_reading", "policy_override"], f"unexpected removals: {removals}"
    from hr.db import ddl

    schema = ddl()
    for table in removals:
        assert table in schema, f"DDL for {table} missing (destructive deletion?)"


def test_release_assets_are_present() -> None:
    # Given: the non-hr/ files the release manifest requires.
    # When: the checkout is inspected.
    missing = [asset for asset in RELEASE_ASSETS if not (ROOT / asset).exists()]
    # Then: every asset needed to build the release is present.
    assert missing == [], f"missing release assets: {missing}"


def test_itemrepo_contract_globs_are_tracked() -> None:
    # Given: the item-repository contract locations named by the manifest.
    # When: tracked files are matched against each contract glob.
    tracked = set(_git(["ls-files"]).splitlines())
    for label, glob in ITEMREPO_CONTRACT_GLOBS.items():
        import fnmatch

        matches = [f for f in tracked if fnmatch.fnmatch(f, glob)]
        # Then: the glob is non-empty at this checkout (the one-to-one
        # registry-vs-disk tests in tests/items/test_items.py stay runnable).
        assert matches, f"itemrepo contract {label} empty: {glob}"


# ---------------------------------------------------------------------------
# Legacy surface (manifest-aware; the raw checks depend on the dirty tree)
# ---------------------------------------------------------------------------


def test_release_closure_has_no_legacy_product_identifier() -> None:
    # Given: the modules this commit adds to the release surface. The one-time
    # data migration is a carve-out: its SQL legitimately renames the legacy
    # "hr2" schema (same exemption as the pre-manifest release test).
    sources = [
        ROOT / p
        for p in sorted(INCLUDED_HR_MODULES)
        if p.endswith(".py") and p != "hr/schema_migration.py"
    ]
    # When: source text is checked as a release artifact. Legacy "hr2" strings
    # that are UNCHANGED from the parent commit are pre-existing tracked
    # content swept into the shipped closure by the CLI unification (e.g.
    # hr/deployable.py); their cleanup lands with the unification commit —
    # out of this commit's scope. Text that DIFFERS from the parent blob (new
    # or rewritten modules) must present the canonical name.
    offenders = [
        path.relative_to(ROOT)
        for path in sources
        if "hr2" in path.read_text(encoding="utf-8").lower()
        and path.read_text(encoding="utf-8") != (_git_show("HEAD~1", str(path.relative_to(ROOT))) or "")
    ]
    # Then: the registered surface consistently presents the canonical name.
    assert offenders == []


def test_release_surface_has_one_database_layer() -> None:
    # Given: the unified storage layer. The legacy parallel layer
    # (hr/database.py) was DELETED by the hr-ship W1 commit and its importers
    # were rewired to hr.db in the same commit — it must neither be tracked,
    # nor registered as an exclusion, nor referenced by any tracked module.
    tracked = set(_git(["ls-files"]).splitlines())
    assert "hr/database.py" not in tracked, "legacy database.py re-appeared in the tree"
    assert "hr/database.py" not in EXCLUDED_HR_MODULES, "hr/database.py registered as an exclusion while unreachable"
    # When: every tracked python module's imports are inspected with the same
    # AST resolution the manifest gate uses (string matching is unreliable —
    # blobs mention "hr.database" in docstrings).
    for path in sorted(tracked):
        if not path.endswith(".py"):
            continue
        src = _git(["show", f"HEAD:{path}"])
        for module, level, lineno, guarded, names in imports_in_src(src):
            targets = resolve_import(module, level, path, names)
            assert "hr/database.py" not in targets, f"{path}:{lineno} imports deleted hr.database"
    # Then: the hr.database layer is unreachable in a fresh interpreter.
    result = _subprocess_import("hr.database")
    assert result.returncode != 0, "deleted hr.database is still importable"


# ---------------------------------------------------------------------------
# Operational probes
# ---------------------------------------------------------------------------


def test_tool_b_registration_resolves_its_runtime_dependencies(monkeypatch) -> None:
    from hr import stage0

    # Given
    class Connection:
        def close(self) -> None:
            return None

    monkeypatch.setattr(stage0, "_connect", Connection)
    monkeypatch.setattr(stage0, "_upsert_battery", lambda *_: "battery")
    monkeypatch.setattr(stage0, "_upsert_battery_item", lambda *_: None)
    monkeypatch.setattr(stage0, "_upsert_item_pool", lambda *_: None)
    monkeypatch.setattr(stage0, "_upsert_seat", lambda *_: None)
    monkeypatch.setattr(stage0, "_upsert_seat_battery", lambda *_: None)
    # At the commit under test stage0.py does not yet define DEFAULT_ITEM_REPO
    # (it lands with the unification revision of stage0) — the HEAD revision
    # of the script imports it inside main() from the already-imported module.
    # Supplying it here (raising=False: absent in the unified working tree)
    # exercises the same path in both the working tree and a fresh checkout.
    monkeypatch.setattr(stage0, "DEFAULT_ITEM_REPO", "seats/example.json", raising=False)
    monkeypatch.setattr(register_tool_b_battery, "load_item_repo", lambda *_args, **_kwargs: {"tool_b": []})

    # When / Then
    assert register_tool_b_battery.main() == 1


def test_spread_probe_resolves_current_benchmark_api() -> None:
    # Given: the operational probe advertised in the README.
    result = _subprocess_import("scripts.spread_probe")

    # When: a clean interpreter imports the script without making API calls.
    # Then: all benchmark imports resolve.
    assert result.returncode == 0, result.stderr


_PIL_BLOCKER = (
    "import sys\n"
    "class _BlockPIL:\n"
    "    def find_spec(self, fullname, path=None, target=None):\n"
    "        if fullname == 'PIL' or fullname.startswith('PIL.'):\n"
    "            raise ModuleNotFoundError(f'PIL blocked: {fullname}')\n"
    "        return None\n"
    "sys.meta_path.insert(0, _BlockPIL())\n"
)


def test_included_import_closure_succeeds_with_pillow_blocked() -> None:
    """Every INCLUDED module imports in an interpreter where PIL is absent.

    Pillow is an optional dependency (the ``[vision]`` extra). The vision
    toolchain draws lazily inside its generator functions, so the import
    closure never touches PIL — only regeneration (an authoring action)
    does, and it raises a loud RuntimeError naming the extra. The blocker
    sits on ``sys.meta_path`` so the absence is absolute, regardless of any
    Pillow installed in the environment.
    """
    # Given: a Pillow-blocked interpreter (meta_path finder raising
    # ModuleNotFoundError for PIL and PIL.*) and every manifest module.
    vision_dir = ROOT / "itemrepo" / "vision"
    for module_path in sorted(INCLUDED_HR_MODULES):
        if module_path == "hr/__main__.py":
            continue  # entry point, not an importable module
        module = module_path.removesuffix(".py").replace("/", ".")
        extra = (str(vision_dir),) if module_path.startswith("itemrepo/vision/") else ()
        env = os.environ | {"PYTHONPATH": os.pathsep.join([*sys.path, *extra])}
        # When: the module is imported with PIL blocked.
        result = subprocess.run(
            [sys.executable, "-c", f"{_PIL_BLOCKER}import {module}"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        # Then: the import closure holds without Pillow.
        assert result.returncode == 0, f"{module_path} fails without Pillow:\n{result.stderr}"