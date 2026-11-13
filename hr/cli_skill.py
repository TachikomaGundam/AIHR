"""``hr skill`` — the skill factory: validate / list / new / install / doctor.

Authority contract (HANDOFF-AIHR-SKILL-FACTORY invariant C): the single
authoritative skills surface is ``<HR_HOME>/opencode-config/skills/`` in the
V2 directory layout (``<id>/SKILL.md``, the layout both OpenCode and Codex
load unchanged). Legacy flat ``<id>.md`` files are still *read* so the
migration's before/after can be proven, but nothing writes them. Every other
location on disk is a COPY; ``hr skill doctor`` measures their drift against
the authority — a second authority is never produced or honoured.

Validation is fail-closed per the OpenCode V2 skills contract
(https://opencode.ai/docs/skills): the engine only advertises skills whose
frontmatter carries a non-empty ``description``, so a missing description is
a FAILURE here, never a passed-with-warning (design requirement 1 — defect D1
shipped precisely because no gate checked it). The frontmatter ``name`` must
satisfy the portable ID rule (lowercase kebab-case, 1-64 chars) AND equal the
path-derived skill ID.

Test seams (never real ``~/.config`` in tests): ``HR_SKILL_SOURCE`` overrides
the authoritative root, ``HR_SKILL_COPIES`` (os.pathsep-separated) registers
extra copy roots for ``doctor``, and ``install --dest`` names the destination
outright. The machine surface (``--json``) writes strict bytes to stdout —
rich markup would swallow brackets (defect #17, same rule as cli_knowledge).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import typer
import yaml

from .cli_app import _fail, app
from .config_resources import hr_home, opencode_config_dir

# Portable skill ID rule from https://opencode.ai/docs/skills ("IDs"):
# lowercase kebab-case, recommended 1-64 characters, kept aligned with the
# directory name so the file copies between Codex and OpenCode unchanged.
SKILL_ID_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SKILL_ID_MAX_LEN = 64

SOURCE_ENV = "HR_SKILL_SOURCE"
COPIES_ENV = "HR_SKILL_COPIES"

# Engine query (measured on opencode v2.2.7, HANDOFF §3): the outer response
# key is ``data`` (not ``skills``), the query parameter must stay URL-encoded,
# and the FIRST query against a never-opened directory legitimately returns an
# empty table — hence the warm-up retries (counting a cold miss as "no skills"
# misreports discovery as absence).
_ENGINE_QUERY_RETRIES = 3
_ENGINE_RETRY_DELAY_S = 1.0
_ENGINE_TIMEOUT_S = 30

skill_app = typer.Typer(
    name="skill",
    help=(
        "Skill factory: validate, list, create, install, and audit the "
        "opencode skills whose single authority lives in this repository "
        "under opencode-config/skills/ (fail-closed frontmatter gate)."
    ),
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# Location resolution
# ---------------------------------------------------------------------------


def authoritative_root() -> Path:
    """The ONLY authoritative skills surface (invariant C).

    ``HR_SKILL_SOURCE`` overrides (test seam and explicit redirection); else
    ``<HR_HOME>/opencode-config/skills`` resolved through the standard
    :func:`hr.config_resources.hr_home` chain.
    """
    env = os.environ.get(SOURCE_ENV)
    if env:
        return Path(env).expanduser().resolve()
    return hr_home() / "opencode-config" / "skills"


def default_dest() -> Path:
    """Default install destination: the opencode config dir's ``skills/`` —
    the global discovery location documented at
    https://opencode.ai/docs/skills ("Discovery")."""
    return opencode_config_dir() / "skills"


def _copy_roots(extra: Optional[list[Path]]) -> list[Path]:
    """Roots believed to hold COPIES of authoritative skills: the default
    install dir, every ``HR_SKILL_COPIES`` entry, every ``--copy`` option."""
    roots: list[Path] = [default_dest()]
    env = os.environ.get(COPIES_ENV)
    if env:
        roots.extend(Path(part).expanduser() for part in env.split(os.pathsep) if part)
    if extra:
        roots.extend(p.expanduser() for p in extra)
    seen: set[Path] = set()
    out: list[Path] = []
    for root in roots:
        resolved = root.resolve() if root.exists() else root
        if resolved not in seen:
            seen.add(resolved)
            out.append(resolved)
    return out


# ---------------------------------------------------------------------------
# Skill discovery (both layouts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillFile:
    """One skill document located under a skills root, with its path-derived ID."""

    path: Path
    skill_id: str  # OpenCode derives the ID from the path, not the frontmatter
    directory_form: bool


def discover_skills(root: Path) -> list[SkillFile]:
    """Enumerate skills under ``root``: ``<id>/SKILL.md`` (V2 form) and
    legacy flat ``<id>.md`` at the root only — mirroring the engine's own
    discovery rule (nested files count only when named exactly SKILL.md)."""
    found: list[SkillFile] = []
    if not root.is_dir():
        return found
    for flat in sorted(root.glob("*.md")):
        found.append(SkillFile(path=flat, skill_id=flat.stem, directory_form=False))
    for sub in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        skill_md = sub / "SKILL.md"
        if skill_md.is_file():
            found.append(SkillFile(path=skill_md, skill_id=sub.name, directory_form=True))
    return found


def _skill_file_for(path: Path) -> SkillFile:
    """Classify a user-supplied path into a SkillFile (accepts the SKILL.md,
    its directory, or a flat ``<id>.md``). The ID is path-derived: a file
    named SKILL.md takes its ID from the parent directory, exactly like the
    engine's own rule (``<source>/<id>/SKILL.md`` -> ``<id>``)."""
    if path.is_dir():
        skill_md = path / "SKILL.md"
        if skill_md.is_file():
            return SkillFile(path=skill_md, skill_id=path.name, directory_form=True)
        _fail(f"error: {path} contains no SKILL.md")
    if path.suffix.lower() != ".md":
        _fail(f"error: {path} is not a Markdown skill file (*.md / SKILL.md)")
    if path.name == "SKILL.md":
        return SkillFile(path=path, skill_id=path.parent.name, directory_form=True)
    return SkillFile(path=path, skill_id=path.stem, directory_form=False)


# ---------------------------------------------------------------------------
# Frontmatter validation (fail-closed)
# ---------------------------------------------------------------------------


def parse_frontmatter(path: Path) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Return ``(meta, hard_defects)``.

    ``(None, [])`` means no frontmatter block at all — the caller reports the
    required fields as missing. ``(None, defects)`` means a block exists but
    is broken (invalid YAML / not a mapping / unterminated).
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, []
    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            raw = "\n".join(lines[1:idx])
            try:
                meta = yaml.safe_load(raw)
            except yaml.YAMLError as exc:
                return None, [f"frontmatter is not valid YAML: {exc}"]
            if not isinstance(meta, dict):
                return None, ["frontmatter block is not a YAML mapping"]
            return meta, []
    return None, ["frontmatter opens with '---' but is never terminated"]


def check_skill(skill: SkillFile) -> list[str]:
    """Defect strings for one skill document; empty list means valid.

    Required (fail-closed): a frontmatter mapping; ``name`` matching the
    portable ID rule and equal to the path-derived ID; non-empty
    ``description``. Optional: ``disable-model-invocation``, which must be a
    boolean when present.
    """
    if not skill.path.is_file():
        return [f"not a readable file: {skill.path}"]
    meta, hard = parse_frontmatter(skill.path)
    if hard:
        return hard
    if meta is None:
        return [
            "no YAML frontmatter block",
            "missing frontmatter field: name (must equal the skill ID "
            f"{skill.skill_id!r} and match ^[a-z0-9]+(-[a-z0-9]+)*$)",
            "missing frontmatter field: description (the engine does not "
            "advertise skills without it — defect D1)",
        ]
    defects: list[str] = []
    name = meta.get("name")
    if name is None:
        defects.append("missing frontmatter field: name")
    elif not isinstance(name, str):
        defects.append(f"frontmatter name must be a string, got {type(name).__name__}")
    else:
        if len(name) > SKILL_ID_MAX_LEN or not SKILL_ID_PATTERN.match(name):
            defects.append(
                f"frontmatter name {name!r} violates the portable skill ID rule "
                "(lowercase kebab-case ^[a-z0-9]+(-[a-z0-9]+)*$, 1-64 chars)"
            )
        if name != skill.skill_id:
            defects.append(
                f"frontmatter name {name!r} does not match the path-derived skill ID "
                f"{skill.skill_id!r}"
            )
    description = meta.get("description")
    if description is None:
        defects.append("missing frontmatter field: description")
    elif not isinstance(description, str) or not description.strip():
        defects.append("frontmatter description must be a non-empty string "
                       "(skills without one are never advertised to the model)")
    disable = meta.get("disable-model-invocation")
    if disable is not None and not isinstance(disable, bool):
        defects.append(
            f"frontmatter disable-model-invocation must be a boolean, got {disable!r}"
        )
    return defects


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_hashes(root: Path) -> dict[str, str]:
    """relpath -> sha256 for every file under ``root`` (sorted): the identity
    of a directory-form skill for idempotence and drift measurement."""
    return {
        str(p.relative_to(root)): _sha256(p)
        for p in sorted(item for item in root.rglob("*") if item.is_file())
    }


# ---------------------------------------------------------------------------
# Engine advertisement probe
# ---------------------------------------------------------------------------


def engine_skills(
    directory: Path,
    attempts: int = _ENGINE_QUERY_RETRIES,
    retry_delay_s: float = _ENGINE_RETRY_DELAY_S,
) -> list[dict[str, Any]]:
    """Skill rows the local opencode engine discovers for ``directory``.

    Raises RuntimeError when the engine cannot be reached or answered (no
    binary, non-zero exit, unparseable JSON); an empty list after all warm-up
    attempts is a legitimate answer, not a failure (cold-start behaviour, see
    the constant block above).
    """
    url = f"/api/skill?location%5Bdirectory%5D={urllib.parse.quote(str(directory), safe='/')}"
    last_error = "engine query failed"
    for attempt in range(1, attempts + 1):
        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv list, no shell
                ["opencode", "api", "get", url],
                capture_output=True,
                text=True,
                timeout=_ENGINE_TIMEOUT_S,
                check=False,
            )
        except FileNotFoundError:
            raise RuntimeError("opencode binary not found on PATH") from None
        except subprocess.TimeoutExpired:
            last_error = f"query timed out after {_ENGINE_TIMEOUT_S}s"
            continue
        if proc.returncode != 0:
            last_error = (
                f"opencode api get exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:200]}"
            )
            continue
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            last_error = "opencode api returned unparseable JSON"
            continue
        rows: object = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            last_error = "opencode api response has no list under the 'data' key"
            continue
        typed = [row for row in rows if isinstance(row, dict)]
        if typed or attempt == attempts:
            return typed
        time.sleep(retry_delay_s)  # cold start: first hit on a new directory returns []
    raise RuntimeError(last_error)


def _advertised_state(rows: list[dict[str, Any]], skill_id: str) -> Optional[bool]:
    """Whether the engine advertises ``skill_id``: only skills with a
    non-empty description enter the model's list (docs/skills, "Loading").
    None = the engine did not report this ID at all."""
    for row in rows:
        if row.get("id") == skill_id:
            description = row.get("description")
            return isinstance(description, str) and bool(description.strip())
    return None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@skill_app.command("validate")
def skill_validate(
    path: Optional[Path] = typer.Argument(
        None, help="skill file (SKILL.md or flat <id>.md) or skill directory to check",
    ),
    all_skills: bool = typer.Option(
        False, "--all", help="validate every skill under the authoritative root",
    ),
) -> None:
    """Validate skill frontmatter — FAIL-CLOSED.

    Checks (any miss = non-zero exit naming the file and the missing field):
    frontmatter mapping; ``name`` matching the portable ID rule and equal to
    the path-derived ID; non-empty ``description`` (the engine never
    advertises a skill without one); boolean ``disable-model-invocation``
    when present.
    """
    if path is not None and all_skills:
        _fail("error: pass either PATH or --all, not both")
    targets: list[SkillFile] = []
    if all_skills:
        root = authoritative_root()
        if not root.is_dir():
            _fail(f"error: authoritative skills root not found: {root}")
        targets = discover_skills(root)
        if not targets:
            print(f"no skills found under {root}")
            return
    elif path is not None:
        targets = [_skill_file_for(path)]
    else:
        _fail("error: pass a PATH or --all")
    failed = False
    for skill in targets:
        defects = check_skill(skill)
        if defects:
            failed = True
            print(f"FAIL {skill.path} (id: {skill.skill_id})")
            for defect in defects:
                print(f"  - {defect}")
        else:
            print(f"OK   {skill.path} (id: {skill.skill_id})")
    if failed:
        raise typer.Exit(code=1)


@skill_app.command("list")
def skill_list(
    as_json: bool = typer.Option(
        False, "--json", help="strict JSON on stdout (machine-readable)",
    ),
) -> None:
    """List the authoritative skills and whether the engine advertises each.

    The engine column comes from ``opencode api get
    "/api/skill?location%5Bdirectory%5D=<root>"`` (warm-up retried). When the
    engine is unreachable the local facts are still listed and the engine
    column says so honestly — never silently blank.
    """
    root = authoritative_root()
    skills = discover_skills(root)
    engine_error: Optional[str] = None
    rows: list[dict[str, Any]] = []
    try:
        rows = engine_skills(root)
    except RuntimeError as exc:
        engine_error = str(exc)

    entries: list[dict[str, Any]] = []
    for skill in skills:
        defects = check_skill(skill)
        entries.append({
            "id": skill.skill_id,
            "path": str(skill.path),
            "layout": "dir" if skill.directory_form else "flat",
            "valid": not defects,
            "defects": defects,
            "advertised": None if engine_error else _advertised_state(rows, skill.skill_id),
        })

    if as_json:
        payload = {
            "root": str(root),
            "engine": {"reachable": engine_error is None, "error": engine_error},
            "skills": entries,
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return

    print(f"authoritative skills root: {root}")
    if not root.is_dir():
        print("  (root does not exist — no skills to list)")
    if engine_error:
        print(f"engine advertisement: UNREACHABLE ({engine_error})")
    for entry in entries:
        advertised = entry["advertised"]
        if advertised is None and engine_error is None:
            engine_state = "not reported"
        elif advertised is None:
            engine_state = "unreachable"
        else:
            engine_state = "advertised" if advertised else "NOT advertised (no description)"
        state = "valid" if entry["valid"] else "INVALID"
        print(f"  • {entry['id']} [{entry['layout']}, {state}] engine: {engine_state}")
        for defect in entry["defects"]:
            print(f"      - {defect}")


@skill_app.command("new")
def skill_new(
    name: str = typer.Argument(..., help="new skill ID (lowercase kebab-case, 1-64 chars)"),
    directory: Optional[Path] = typer.Option(
        None, "--dir",
        help="skills root to create under (default: the authoritative root)",
    ),
) -> None:
    """Scaffold a new skill at ``<root>/<name>/SKILL.md`` (V2 layout).

    The skeleton carries frontmatter that passes ``hr skill validate``
    immediately — the template itself is fail-closed: it cannot produce a
    skill the engine would not advertise.
    """
    if len(name) > SKILL_ID_MAX_LEN or not SKILL_ID_PATTERN.match(name):
        _fail(
            f"error: {name!r} is not a valid skill ID "
            "(lowercase kebab-case ^[a-z0-9]+(-[a-z0-9]+)*$, 1-64 chars)"
        )
    root = directory.expanduser().resolve() if directory else authoritative_root()
    target_dir = root / name
    flat = root / f"{name}.md"
    if target_dir.exists() or flat.exists():
        _fail(f"error: skill {name!r} already exists ({target_dir} or {flat})")
    root.mkdir(parents=True, exist_ok=True)
    target_dir.mkdir(parents=True)
    skill_md = target_dir / "SKILL.md"
    skill_md.write_text(_SKILL_TEMPLATE.format(name=name), encoding="utf-8")
    print(f"created {skill_md}")
    print("next: write the body, then `hr skill validate "
          f"{skill_md}` and `hr skill install {name}`")


_SKILL_TEMPLATE = """---
name: {name}
description: Replace with one line saying WHEN the model should use this skill (non-empty; the engine only advertises skills with a description).
---

# {name}

Use this skill when <state the trigger condition>.

## Steps

1. <first concrete step>
2. <second concrete step>

## Rules

- <rule the model must keep while following this skill>
"""


@skill_app.command("install")
def skill_install(
    name: str = typer.Argument(..., help="skill ID to install from the authoritative root"),
    dest: Optional[Path] = typer.Option(
        None, "--dest",
        help="destination skills dir (default: <opencode config dir>/skills)",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="report what would happen; write nothing",
    ),
    force: bool = typer.Option(
        False, "--force", help="replace a destination copy that differs from the authority",
    ),
) -> None:
    """Install one authoritative skill into a destination skills dir (idempotent).

    Validates first (an invalid skill is REFUSED, never installed), copies the
    whole skill directory to ``<dest>/<id>/`` (flat sources become the V2
    directory form), and prints the SHA-256 before and after. Re-running on an
    identical destination performs zero writes and exits 0; a differing
    destination fails unless ``--force``.
    """
    root = authoritative_root()
    skill = next((s for s in discover_skills(root) if s.skill_id == name), None)
    if skill is None:
        _fail(f"error: skill {name!r} not found in the authoritative root: {root}")
    defects = check_skill(skill)
    if defects:
        print(f"refusing to install INVALID skill {name!r} ({skill.path}):", file=sys.stderr)
        for defect in defects:
            print(f"  - {defect}", file=sys.stderr)
        raise typer.Exit(code=1)

    dest_dir = (dest.expanduser().resolve() if dest else default_dest().resolve())
    source_dir = skill.path.parent
    target_dir = dest_dir / name
    source_hashes = (
        _tree_hashes(source_dir) if skill.directory_form
        else {"SKILL.md": _sha256(skill.path)}
    )

    print(f"source: {skill.path} (id {name})")
    for rel in sorted(source_hashes):
        print(f"  src {rel} sha256 {source_hashes[rel]}")

    target_exists = target_dir.is_dir() or (dest_dir / f"{name}.md").is_file()
    target_hashes = _tree_hashes(target_dir) if target_dir.is_dir() else {}
    if target_exists:
        for rel in sorted(set(target_hashes) | set(source_hashes)):
            src = source_hashes.get(rel)
            dst = target_hashes.get(rel)
            print(f"  dst {rel} sha256 {dst if dst else '(missing)'}"
                  + ("" if src == dst else "  <-- differs"))
    if target_hashes == source_hashes and target_dir.is_dir():
        print(f"already installed, identical: {target_dir} (no writes)")
        return
    if dry_run:
        # Simulation-faithful: report the plan AND the verdict the real run
        # would reach, while writing nothing.
        if target_exists and not force:
            print(
                f"dry-run: {target_dir} exists and differs from the authority — "
                "the real run FAILS here without --force (nothing written)"
            )
            raise typer.Exit(code=1)
        action = "replace" if target_exists else "install"
        print(f"dry-run: would {action} {source_dir} -> {target_dir} (nothing written)")
        return
    if target_exists and not force:
        _fail(
            f"error: {target_dir} already exists and differs from the authority; "
            "pass --force to replace it"
        )

    if target_dir.is_dir():
        shutil.rmtree(target_dir)
    legacy_flat = dest_dir / f"{name}.md"
    if legacy_flat.is_file():
        legacy_flat.unlink()  # normalize the old flat layout out of the namespace
        print(f"removed legacy flat copy: {legacy_flat}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    if skill.directory_form:
        shutil.copytree(source_dir, target_dir)
    else:
        target_dir.mkdir(parents=True)
        shutil.copy2(skill.path, target_dir / "SKILL.md")
    written = _tree_hashes(target_dir)
    print(f"installed: {target_dir}")
    for rel in sorted(written):
        print(f"  dst {rel} sha256 {written[rel]}")
    if written != source_hashes:
        _fail(f"error: post-install verification failed under {target_dir}")


@skill_app.command("doctor")
def skill_doctor(
    copies: Optional[list[Path]] = typer.Option(
        None, "--copy",
        help="additional copy root to audit (repeatable); "
             f"the default install dir and {COPIES_ENV} entries are always audited",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="strict JSON on stdout (machine-readable)",
    ),
) -> None:
    """Hash-compare the authority against every copy; drift exits non-zero.

    DRIFT (same skill ID with different content between the authority and a
    copy) fails the run — three drifted copies of one skill is exactly the
    defect this command exists to catch. A copy whose ID the authority does
    not define is an ORPHAN: reported loudly, but it exits 0, because a
    third-party skill legitimately installed beside ours is not our drift.
    """
    root = authoritative_root()
    roots = _copy_roots(copies)
    authority = {s.skill_id: s for s in discover_skills(root)}
    drift = False
    report: list[dict[str, Any]] = []

    copy_maps: list[tuple[Path, dict[str, SkillFile]]] = [
        (r, {s.skill_id: s for s in discover_skills(r)}) for r in roots
    ]
    all_ids = sorted(set(authority) | {sid for _, m in copy_maps for sid in m})

    for skill_id in all_ids:
        authority_skill = authority.get(skill_id)
        authority_hash = _tree_hashes(authority_skill.path.parent) if authority_skill else None
        row: dict[str, Any] = {
            "id": skill_id,
            "authority": None if authority_skill is None else {
                "path": str(authority_skill.path),
                "valid": not check_skill(authority_skill),
                "content": authority_hash,
            },
            "copies": [],
            "status": "not installed",
        }
        copy_hashes: list[dict[str, str]] = []
        for copy_root, skill_map in copy_maps:
            copy_skill = skill_map.get(skill_id)
            if copy_skill is None:
                continue
            copy_hash = _tree_hashes(copy_skill.path.parent) if copy_skill.directory_form else {
                "SKILL.md": _sha256(copy_skill.path)
            }
            copy_hashes.append(copy_hash)
            row["copies"].append({"path": str(copy_skill.path), "content": copy_hash})
        if authority_hash is None:
            row["status"] = "ORPHAN (copy has no authoritative skill)"
        elif not copy_hashes:
            row["status"] = "not installed"
        elif all(h == authority_hash for h in copy_hashes):
            row["status"] = "in sync"
        else:
            row["status"] = "DRIFT (copy differs from authority)"
            drift = True
        report.append(row)

    if as_json:
        payload = {
            "authority": str(root),
            "copy_roots": [str(r) for r in roots],
            "skills": report,
            "drift": drift,
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        if drift:
            raise typer.Exit(code=1)
        return

    print(f"authority: {root}" + ("" if root.is_dir() else "  (MISSING)"))
    for copy_root, _ in copy_maps:
        label = "copy root" if copy_root != root else "copy root (= authority)"
        print(f"{label}: {copy_root}")
    for row in report:
        print(f"  {row['id']}: {row['status']}")
        authority_cell = row["authority"]
        if authority_cell is not None:
            assert isinstance(authority_cell, dict)
            content = authority_cell["content"]
            assert isinstance(content, dict)
            digest = _combined_digest(content)
            print(f"      authority {authority_cell['path']} {digest}"
                  + ("" if authority_cell["valid"] else "  [INVALID frontmatter]"))
        for copy in row["copies"]:
            content = copy["content"]
            print(f"      copy      {copy['path']} {_combined_digest(content)}")
    if drift:
        print("doctor: FAIL — drifted copies listed above")
        raise typer.Exit(code=1)
    print("doctor: no drift")


def _combined_digest(tree: dict[str, str]) -> str:
    """One digest standing for a whole skill directory (per-file hashes stay
    in the JSON surface; the text table needs a single comparable token)."""
    surface = json.dumps(sorted(tree.items()))
    return hashlib.sha256(surface.encode()).hexdigest()[:12]


app.add_typer(skill_app, name="skill")
