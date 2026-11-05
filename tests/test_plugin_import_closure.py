"""Import-closure guard for the shipped OpenCode plugin package.

The plugin ships by explicit file lists: ``package.json`` ``files``,
``hr/release_manifest.py`` ``RELEASE_ASSETS`` and the copy step in
``.github/workflows/ci.yml``. Any relative import added to a shipped source
file without registering its target in those lists ships a package that
crashes at load time on the user's box — the plugin-side twin of the 0.6.10
data-files lesson (missing file invisible in-repo, fatal on the customer).
This guard closes that class for the plugin: every relative import target of
every plugin root source file must (1) exist on disk and (2) be registered in
BOTH file lists, and every root source file must itself be registered.

One documented exception encodes OpenCode's runtime: Bun rewrites a
``./x.js`` specifier to ``./x.ts`` when no real ``.js`` exists (the specifier
server.ts uses). The rewritten ``.ts`` target then faces the same checks.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from hr.release_manifest import RELEASE_ASSETS

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "opencode_plugin"

_RELATIVE_IMPORT_RE = re.compile(
    r"""(?:from\s+|import\s*\(\s*)["'](\.{1,2}/[^"']+)["']"""
)


def _root_sources() -> list[Path]:
    sources = sorted(
        p
        for pattern in ("*.ts", "*.mts", "*.mjs", "*.js", "*.cjs")
        for p in PLUGIN_DIR.glob(pattern)
    )
    assert sources, f"no plugin sources found under {PLUGIN_DIR}"
    return sources


def _package_files() -> set[str]:
    manifest = json.loads((PLUGIN_DIR / "package.json").read_text(encoding="utf-8"))
    return set(manifest["files"])


def _relative_specifiers(source: Path) -> set[str]:
    return set(_RELATIVE_IMPORT_RE.findall(source.read_text(encoding="utf-8")))


def _resolve(source: Path, specifier: str) -> Path:
    """Resolve one relative specifier, allowing the documented Bun .js→.ts rewrite."""
    target = (source.parent / specifier).resolve()
    if not target.exists() and specifier.endswith(".js"):
        rewritten = target.with_suffix(".ts")
        if rewritten.exists():
            return rewritten
    return target


def test_every_plugin_root_source_is_registered_in_both_ship_lists() -> None:
    package_files = _package_files()
    missing: list[str] = []
    for source in _root_sources():
        rel = source.relative_to(REPO_ROOT).as_posix()
        if rel not in RELEASE_ASSETS:
            missing.append(f"{rel} not in RELEASE_ASSETS")
        if source.name not in package_files:
            missing.append(f"{source.name} not in package.json files")
    assert not missing, "plugin file lists drifted:\n" + "\n".join(missing)


def test_every_relative_import_target_exists_and_is_registered() -> None:
    package_files = _package_files()
    failures: list[str] = []
    for source in _root_sources():
        for specifier in sorted(_relative_specifiers(source)):
            target = _resolve(source, specifier)
            if not target.exists():
                failures.append(
                    f"{source.name}: import {specifier!r} resolves to missing {target}"
                )
                continue
            try:
                rel = target.relative_to(REPO_ROOT).as_posix()
            except ValueError:  # outside the repo — never ship via relative import
                failures.append(f"{source.name}: import {specifier!r} escapes the repo")
                continue
            if target.parent == PLUGIN_DIR:
                if rel not in RELEASE_ASSETS:
                    failures.append(
                        f"{source.name}: target {rel} missing from RELEASE_ASSETS"
                    )
                if target.name not in package_files:
                    failures.append(
                        f"{source.name}: target {target.name} missing from "
                        f"package.json files"
                    )
    assert not failures, "plugin import closure broken:\n" + "\n".join(failures)
