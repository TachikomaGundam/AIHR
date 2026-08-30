"""Deployment manager: the HR release lifecycle (hr-evolution todo 9).

One auditable pipeline: build -> verify -> activate -> rollback / retain.

* ``build_release`` copies the T8 release manifest surface (INCLUDED_HR_MODULES
  + RELEASE_ASSETS + the tracked itemrepo tree + the manifest file itself)
  into a candidate under ``releases_root`` and writes ``metadata.json`` with
  the SHA-256 of EVERY payload plus the manifest digest it was built from.
  The build FAILS loudly (no directory created) when any listed file is
  missing — a surface-completeness gate.
* ``verify_release`` re-hashes every payload snapshot-locally and records the
  verified manifest hash + per-payload check results in ``verification.json``.
  A failed verification REMOVES the candidate (``remove_on_failure=False``
  for read-only checks).
* ``activate_release`` re-verifies the candidate, takes a Todo-7 backup of
  the opencode config dir (``hr.plugin_safety.create_backup``) BEFORE any
  change, atomically swaps ONLY the ``hr`` symlink (temp symlink +
  ``os.replace``), then registers the released plugin path in the opencode
  config's ``plugin`` array (format-preserving, idempotent). Activating the
  already-active release is a no-op; any failure after a write restores the
  prior pointers.
* ``rollback_release`` restores the previous symlink target AND rolls back
  the config through the Todo-7 ``rollback`` primitive plus the activation
  ledger/blob recorded inside the same backup.
* ``enforce_retention_policy`` bounds the release/archive history (mirrors
  the T7 recovery-store policy constants) and cleans stale plugin entries
  (config entries pointing at releases that no longer exist).

Every function takes its paths explicitly (defaults are the real deployment
locations) so tests can drive a complete lifecycle against ``tmp_path`` —
the real symlink/config/archives are never touched by the test suite.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console

from hr.opencfg import parse_config_file
from hr.plugin_safety import (
    BACKUP_DIR,
    MAX_AGE_DAYS,
    MAX_BACKUPS,
    UnsafeNameError,
    UnsafePathError,
    contained,
    create_backup,
    get_config_dir,
    rollback,
    safe_component,
)
from hr.release_manifest import INCLUDED_HR_MODULES, RELEASE_ASSETS

console = Console()

RELEASES_DIR = Path.home() / ".local" / "share" / "hr-agent" / "releases"
# Deploy target for the hr symlink: $HOME/hr. No literal absolute path here:
# universality check (b) in scripts/check_universal.sh forbids machine-specific
# /home paths in source, and Path.home() resolves to the canonical deploy
# location on the deployment host anyway. Override order: explicit argument >
# HR_HR_SYMLINK env var (_resolve_symlink) > this fallback.
HR_SYMLINK = Path.home() / "hr"

# Retention bounds mirror the T7 recovery-store policy (MAX_BACKUPS /
# MAX_AGE_DAYS) so release history and backup history share one revisable
# retention contract: keep at most 10 releases, drop anything older than
# 30 days, always preserve the newest VALID release and the ACTIVE release.
MAX_RELEASES_TO_KEEP = MAX_BACKUPS
MAX_RELEASE_AGE_DAYS = MAX_AGE_DAYS

# Bump when the surface semantics recorded in metadata.json change.
MANIFEST_VERSION = "1"

PLUGIN_SUBDIR = "opencode_plugin"
CONFIG_FILENAME = "opencode.jsonc"
ACTIVATION_LEDGER = "release-activation.json"
ARCHIVE_PREFIX = "hr-release-"


def _fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise typer.Exit(code=1)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Surface
# ---------------------------------------------------------------------------


def _surface_paths(workspace: Path) -> list[str]:
    """Repo-relative release surface (sorted).

    The manifest's included modules + release assets, the manifest file
    itself (deployment_manager imports it at runtime, so it ships), and the
    TRACKED itemrepo tree (the reasoning/vision/tool items are runtime data;
    T8's ITEMREPO_CONTRACT_GLOBS reference it).
    """
    paths = set(INCLUDED_HR_MODULES) | set(RELEASE_ASSETS) | {"hr/release_manifest.py"}
    result = subprocess.run(
        ["git", "ls-files", "--", "itemrepo"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "cannot enumerate tracked itemrepo files: "
            f"{workspace} is not a git checkout (git rc={result.returncode})"
        )
    paths |= {
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and line.startswith("itemrepo/")
    }
    return sorted(paths)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_release(
    workspace: Path,
    releases_root: Path | None = None,
    release_name: str | None = None,
) -> dict[str, Any]:
    """Build a release candidate from the workspace release surface.

    Copies every manifest payload into ``<releases_root>/<release_name>`` and
    writes ``metadata.json`` containing the SHA-256 of every payload plus the
    manifest digest (``manifest_hash``) and the digest of the manifest file
    itself (``manifest_source_sha256``), so verification can re-derive both.
    Refuses (and creates nothing) when any listed payload is missing or the
    itemrepo tree cannot be enumerated.
    """
    root = Path(releases_root) if releases_root is not None else RELEASES_DIR
    ws = Path(workspace)
    if release_name is None:
        release_name = datetime.now(timezone.utc).strftime("release-%Y%m%d-%H%M%S")
    try:
        safe_component(release_name)
    except UnsafeNameError as exc:
        return {"success": False, "error": f"unsafe release name {release_name!r}: {exc}"}
    candidate = root / release_name

    if not (ws / "hr").is_dir():
        return {"success": False, "error": f"Source directory not found: {ws / 'hr'}"}
    try:
        surface = _surface_paths(ws)
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}

    missing = [rel for rel in surface if not (ws / rel).is_file()]
    if missing:
        return {
            "success": False,
            "error": "release surface incomplete; missing payloads: " + ", ".join(missing),
            "missing": missing,
        }

    root.mkdir(parents=True, exist_ok=True)
    candidate.mkdir(parents=True, exist_ok=False)
    payloads: dict[str, str] = {}
    try:
        for rel in surface:
            dst = candidate / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ws / rel, dst)
            payloads[rel] = _sha256_file(dst)
    except BaseException:
        shutil.rmtree(candidate, ignore_errors=True)
        raise

    manifest_surface = [rel for rel in surface if not rel.startswith("itemrepo/")]
    metadata = {
        "name": release_name,
        "created_at": _utcnow(),
        "source": str(ws),
        "manifest_version": MANIFEST_VERSION,
        # The manifest surface is the authoritative copy list from
        # release_manifest; tracked itemrepo files ship as payloads but are
        # NOT part of the manifest digest — hash and record the SAME list so
        # verification can re-derive the digest.
        "manifest_hash": hashlib.sha256(json.dumps(manifest_surface).encode()).hexdigest(),
        "manifest_source_sha256": _sha256_file(ws / "hr" / "release_manifest.py"),
        "surface": manifest_surface,
        "payloads": payloads,
    }
    (candidate / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return {
        "success": True,
        "release_name": release_name,
        "release_path": str(candidate),
        "payload_count": len(payloads),
        "manifest_hash": metadata["manifest_hash"],
    }


def compute_release_hash(release_name: str, releases_root: Path | None = None) -> str:
    """SHA-256 over every payload of a release, in sorted path order."""
    root = Path(releases_root) if releases_root is not None else RELEASES_DIR
    try:
        safe_component(release_name)
    except UnsafeNameError:
        return ""
    metadata_path = root / release_name / "metadata.json"
    if not metadata_path.is_file():
        return ""
    try:
        payloads = json.loads(metadata_path.read_text()).get("payloads", {})
    except (json.JSONDecodeError, OSError):
        return ""
    hasher = hashlib.sha256()
    for rel in sorted(payloads):
        path = root / release_name / rel
        if path.is_file():
            hasher.update(path.read_bytes())
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_release(
    release_name: str,
    releases_root: Path | None = None,
    *,
    remove_on_failure: bool = True,
) -> dict[str, Any]:
    """Re-hash every payload snapshot-locally and record the outcome.

    ``verification.json`` records the verified manifest hash + per-payload
    check results. A failed verification REMOVES the candidate by default
    (``remove_on_failure=False`` keeps it for inspection).
    """
    root = Path(releases_root) if releases_root is not None else RELEASES_DIR
    try:
        safe_component(release_name)
    except UnsafeNameError as exc:
        return {
            "valid": False,
            "error": f"unsafe release name {release_name!r}: {exc}",
            "removed": False,
            "checks": [],
            "verified_at": _utcnow(),
            "manifest_hash": None,
            "payload_count": 0,
        }
    candidate = root / release_name
    if not candidate.is_dir():
        return {"valid": False, "error": f"Release directory not found: {candidate}"}

    checks: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    try:
        metadata = json.loads((candidate / "metadata.json").read_text())
    except (json.JSONDecodeError, OSError) as exc:
        checks.append({"check": "metadata", "status": "error", "error": str(exc)})

    if metadata:
        surface = metadata.get("surface")
        manifest_hash = metadata.get("manifest_hash")
        if isinstance(surface, list) and isinstance(manifest_hash, str):
            recomputed = hashlib.sha256(json.dumps(surface).encode()).hexdigest()
            checks.append(
                {
                    "check": "manifest_hash",
                    "status": "ok" if recomputed == manifest_hash else "mismatch",
                }
            )
        else:
            checks.append(
                {"check": "manifest_hash", "status": "error", "error": "metadata surface/hash missing"}
            )
        payloads = metadata.get("payloads")
        if isinstance(payloads, dict):
            for rel in sorted(payloads):
                path = candidate / rel
                if not path.is_file():
                    checks.append({"check": f"payload:{rel}", "status": "missing"})
                    continue
                digest = _sha256_file(path)
                checks.append(
                    {
                        "check": f"payload:{rel}",
                        "status": "ok" if digest == payloads[rel] else "mismatch",
                    }
                )
        manifest_source = candidate / "hr" / "release_manifest.py"
        stored_source = metadata.get("manifest_source_sha256")
        if manifest_source.is_file() and isinstance(stored_source, str):
            checks.append(
                {
                    "check": "manifest_source",
                    "status": "ok" if _sha256_file(manifest_source) == stored_source else "mismatch",
                }
            )

    valid = bool(checks) and all(c["status"] == "ok" for c in checks)
    verification = {
        "verified_at": _utcnow(),
        "valid": valid,
        "manifest_hash": metadata.get("manifest_hash") if metadata else None,
        "payload_count": len(metadata.get("payloads", {})) if metadata else 0,
        "checks": checks,
    }
    removed = False
    if valid:
        (candidate / "verification.json").write_text(
            json.dumps(verification, indent=2) + "\n"
        )
    elif remove_on_failure:
        try:
            contained(candidate, root)
        except UnsafePathError as exc:
            return {
                **verification,
                "removed": False,
                "error": f"refusing to remove candidate: {exc}",
            }
        shutil.rmtree(candidate, ignore_errors=True)
        removed = True
    return {**verification, "removed": removed}


def list_releases(releases_root: Path | None = None) -> list[dict[str, Any]]:
    """List releases (directories with metadata.json), newest first."""
    root = Path(releases_root) if releases_root is not None else RELEASES_DIR
    if not root.is_dir():
        return []
    releases: list[dict[str, Any]] = []
    for release_path in sorted(root.iterdir(), reverse=True):
        metadata_path = release_path / "metadata.json"
        if not release_path.is_dir() or not metadata_path.is_file():
            continue
        try:
            safe_component(release_path.name)
            contained(release_path, root)
        except (UnsafeNameError, UnsafePathError):
            # Security: foreign entries (escaped names, symlinks pointing out
            # of the releases root) are skipped, never touched.
            continue
        try:
            metadata = json.loads(metadata_path.read_text())
        except (json.JSONDecodeError, OSError):
            metadata = {}
        created_at = metadata.get("created_at")
        if not isinstance(created_at, str):
            created_at = datetime.fromtimestamp(
                release_path.stat().st_ctime, tz=timezone.utc
            ).isoformat()
        verification: dict[str, Any] = {}
        verification_path = release_path / "verification.json"
        if verification_path.is_file():
            try:
                verification = json.loads(verification_path.read_text())
            except (json.JSONDecodeError, OSError):
                verification = {}
        payloads = metadata.get("payloads")
        releases.append(
            {
                "name": release_path.name,
                "path": str(release_path),
                "created_at": created_at,
                "verified": bool(verification.get("valid")),
                "manifest_hash": metadata.get("manifest_hash"),
                "payload_count": len(payloads) if isinstance(payloads, dict) else 0,
            }
        )
    return releases


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------


def _atomic_symlink(target: Path, link: Path) -> None:
    """Atomically point ``link`` at ``target`` (temp symlink + os.replace).

    ``rename(2)`` swaps the symlink itself in one step: a reader either sees
    the old target or the new one, never a missing link.
    """
    tmp = link.parent / f".{link.name}.tmp-{uuid.uuid4().hex[:8]}"
    tmp.symlink_to(target)
    try:
        os.replace(tmp, link)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex[:8]}"
    tmp.write_text(text, encoding="utf-8")
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _config_entries(config_file: Path) -> list[str]:
    """Plugin paths currently registered in the opencode config."""
    if not config_file.is_file():
        return []
    try:
        data = parse_config_file(config_file)
    except ValueError:
        return []
    entries = data.get("plugin")
    if not isinstance(entries, list):
        return []
    return [str(entry) for entry in entries if isinstance(entry, str)]


def _jsonc_tokens(raw: str) -> list[tuple[str, int, int]]:
    """Tokenize raw JSONC text: ("s", start, end) for strings, the character
    itself otherwise; comments and whitespace are skipped."""
    tokens: list[tuple[str, int, int]] = []
    i, n = 0, len(raw)
    while i < n:
        char = raw[i]
        if char in " \t\r\n":
            i += 1
            continue
        if char == "/" and i + 1 < n and raw[i + 1] == "/":
            while i < n and raw[i] != "\n":
                i += 1
            continue
        if char == "/" and i + 1 < n and raw[i + 1] == "*":
            end = raw.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if char in {'"', "'"}:
            j = i + 1
            while j < n:
                if raw[j] == "\\":
                    j += 2
                    continue
                if raw[j] == char:
                    break
                j += 1
            tokens.append(("s", i, min(j + 1, n)))
            i = j + 1 if j < n else n
            continue
        tokens.append((char, i, i + 1))
        i += 1
    return tokens


def _plugin_array_position(
    tokens: list[tuple[str, int, int]], raw: str
) -> tuple[tuple[str, int, int], tuple[str, int, int]] | None:
    """Span (open, close) of the top-level ``plugin`` array, or None when the
    config has no ``plugin`` key. Raises ValueError for a non-array plugin."""
    non_array_plugin = False
    for idx, (kind, start, end) in enumerate(tokens):
        if kind != "s":
            continue
        try:
            value = json.loads(raw[start:end])
        except json.JSONDecodeError:
            continue
        if value != "plugin":
            continue
        colon = next(
            (j for j in range(idx + 1, len(tokens)) if tokens[j][0] == ":"), None
        )
        if colon is None:
            continue
        array = next(
            (j for j in range(colon + 1, len(tokens)) if tokens[j][0] == "["), None
        )
        if array is None:
            non_array_plugin = True
            continue
        depth = 0
        for j in range(array, len(tokens)):
            if tokens[j][0] == "[":
                depth += 1
            elif tokens[j][0] == "]":
                depth -= 1
                if depth == 0:
                    return tokens[array], tokens[j]
        break
    if non_array_plugin:
        raise ValueError("'plugin' entry is not an array; refusing auto-registration")
    return None


def _insert_plugin_path(raw: str, plugin_path: str) -> str:
    """Append ``plugin_path`` to the config's ``plugin`` array, preserving
    everything else byte-for-byte (comments, formatting, other keys)."""
    tokens = _jsonc_tokens(raw)
    position = _plugin_array_position(tokens, raw)
    entry = json.dumps(plugin_path)
    if position is None:
        for kind, _, end in tokens:
            if kind == "{":
                return raw[:end] + '\n  "plugin": [' + entry + "]," + raw[end:]
        raise ValueError("config has no top-level object; refusing auto-registration")
    array_open, array_close = position
    inner = raw[array_open[2] : array_close[1]]
    if not inner.strip():
        new_inner = entry
    elif inner.rstrip().endswith(","):
        new_inner = inner + "\n    " + entry
    else:
        new_inner = inner + ",\n    " + entry
    return raw[: array_open[2]] + new_inner + raw[array_close[1] :]


def _register_plugin_path(config_file: Path, plugin_path: str) -> tuple[bool, list[str]]:
    """Register ``plugin_path`` in the config's plugin array (idempotent,
    format-preserving). Returns (changed, resulting entries)."""
    if not config_file.is_file():
        _atomic_write(
            config_file, "{\n  \"plugin\": [" + json.dumps(plugin_path) + "]\n}\n"
        )
        return True, [plugin_path]
    raw = config_file.read_text(encoding="utf-8")
    entries = _config_entries(config_file)
    if plugin_path in entries:
        return False, entries
    try:
        new_raw = _insert_plugin_path(raw, plugin_path)
    except ValueError as exc:
        raise ValueError(f"cannot register plugin path in {config_file}: {exc}") from exc
    _atomic_write(config_file, new_raw)
    return True, [*entries, plugin_path]


def _remove_plugin_entries(
    config_file: Path, entries_to_remove: list[str]
) -> tuple[bool, list[str]]:
    """Remove plugin entries from the config's plugin array (the array itself
    is deployment-managed, so it is normalized via json.dumps; the rest of
    the file is preserved byte-for-byte). Returns (changed, removed)."""
    if not config_file.is_file():
        return False, []
    raw = config_file.read_text(encoding="utf-8")
    tokens = _jsonc_tokens(raw)
    position = _plugin_array_position(tokens, raw)
    if position is None:
        return False, []
    array_open, array_close = position
    inner_start, inner_end = array_open[1], array_close[2]
    inner_tokens = [
        token for token in tokens if token[1] >= inner_start and token[2] <= inner_end
    ]
    removed: list[str] = []
    remaining: list[str] = []
    for kind, start, end in inner_tokens:
        if kind != "s":
            continue
        try:
            value = json.loads(raw[start:end])
        except json.JSONDecodeError:
            continue
        if value in entries_to_remove:
            removed.append(value)
        else:
            remaining.append(value)
    if not removed:
        return False, []
    body = json.dumps(remaining, indent=2)
    _atomic_write(config_file, raw[:inner_start] + body + raw[inner_end:])
    return True, removed


def _stale_plugin_entries(
    entries: list[str], releases_root: Path, release_names: set[str]
) -> list[str]:
    root_str = str(releases_root)
    stale: list[str] = []
    for entry in entries:
        if not entry.startswith(root_str + "/"):
            continue
        name = entry[len(root_str) + 1 :].split("/", 1)[0]
        if not name or name not in release_names:
            stale.append(entry)
    return stale


def _snapshot_id(backup_path: Path) -> str | None:
    try:
        manifest = json.loads((backup_path / "manifest.json").read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return manifest.get("snapshot_id") if isinstance(manifest, dict) else None


def _restore_link(
    link: Path, previous_target: str | None, previous_existed: bool
) -> None:
    if previous_existed and previous_target:
        _atomic_symlink(Path(previous_target), link)
    elif link.is_symlink() or link.exists():
        link.unlink()


def _restore_config_file(
    config_file: Path, backup_path: Path, config_existed: bool
) -> None:
    blob = backup_path / CONFIG_FILENAME
    if config_existed:
        if blob.exists():
            shutil.copy2(blob, config_file)
    elif config_file.exists():
        config_file.unlink()


def activate_release(
    release_name: str,
    *,
    releases_root: Path | None = None,
    hr_symlink: Path | None = None,
    config_dir: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Activate a verified release.

    Refuses on verification failure, on a non-symlink activation target, and
    on an un-parseable/non-array plugin entry. Real mode: T7 create_backup of
    the config dir BEFORE any change, atomic swap of ONLY the ``hr`` symlink,
    then idempotent registration of the released plugin path. Activating the
    already-active release is a no-op. Any failure after the symlink swap
    restores the prior pointers.
    """
    root = Path(releases_root) if releases_root is not None else RELEASES_DIR
    link = Path(hr_symlink) if hr_symlink is not None else HR_SYMLINK
    cfg_dir = Path(config_dir) if config_dir is not None else get_config_dir()
    try:
        safe_component(release_name)
    except UnsafeNameError as exc:
        return {"success": False, "error": f"unsafe release name {release_name!r}: {exc}"}
    candidate = root / release_name
    config_file = cfg_dir / CONFIG_FILENAME
    try:
        plugin_path = str(contained(candidate / PLUGIN_SUBDIR, root))
    except UnsafePathError as exc:
        return {
            "success": False,
            "error": f"cannot register plugin path outside the releases root: {exc}",
        }

    verification = verify_release(release_name, root, remove_on_failure=False)
    if not verification["valid"]:
        bad = next((c for c in verification["checks"] if c["status"] != "ok"), None)
        detail = (
            f"{bad['check']}: {bad['status']}"
            if bad
            else verification.get("error", "verification failed")
        )
        return {
            "success": False,
            "error": f"release did not verify: {detail}",
            "checks": verification["checks"],
        }
    if link.exists() and not link.is_symlink():
        return {
            "success": False,
            "error": f"activation target {link} exists and is not a symlink; refusing",
        }

    already_linked = (
        link.is_symlink()
        and Path(os.readlink(link)).resolve() == (candidate / "hr").resolve()
    )
    already_registered = plugin_path in _config_entries(config_file)
    if already_linked and already_registered:
        return {
            "success": True,
            "already_active": True,
            "release_name": release_name,
            "plugin_path": plugin_path,
            "message": f"Release {release_name} is already active; nothing to do.",
        }

    previous_target = os.readlink(link) if link.is_symlink() else None
    previous_existed = link.is_symlink() or link.exists()

    plan = {
        "release_name": release_name,
        "symlink": str(link),
        "symlink_target_after": str(candidate / "hr"),
        "previous_symlink_target": previous_target,
        "plugin_path": plugin_path,
        "config_file": str(config_file),
        "will_backup": f"T7 create_backup of {cfg_dir}",
    }
    if dry_run:
        return {"success": True, "dry_run": True, "plan": plan}

    cfg_dir.mkdir(parents=True, exist_ok=True)
    backup_path = create_backup(config_dir=cfg_dir)
    config_existed = config_file.is_file()
    if config_existed:
        shutil.copy2(config_file, backup_path / CONFIG_FILENAME)
    ledger = {
        "release_name": release_name,
        "activated_at": _utcnow(),
        "previous_symlink_target": previous_target,
        "previous_symlink_existed": previous_existed,
        "previous_symlink_target_absolute": isinstance(previous_target, str)
        and bool(previous_target)
        and previous_target.startswith(os.sep),
        "config_existed_before": config_existed,
        "plugin_path": plugin_path,
        "config_file": str(config_file),
    }
    (backup_path / ACTIVATION_LEDGER).write_text(json.dumps(ledger, indent=2) + "\n")

    symlink_swapped = False
    config_written = False
    try:
        _atomic_symlink(candidate / "hr", link)
        symlink_swapped = True
        registered, entries = _register_plugin_path(config_file, plugin_path)
        config_written = registered
    except BaseException as exc:  # noqa: BLE001 - restore and report, never leak
        if symlink_swapped:
            _restore_link(link, previous_target, previous_existed)
        if config_written or (config_existed and (backup_path / CONFIG_FILENAME).exists()):
            _restore_config_file(config_file, backup_path, config_existed)
        return {
            "success": False,
            "error": f"activation failed: {exc}",
            "restored": True,
            "backup": str(backup_path),
            "symlink_swapped": symlink_swapped,
        }

    return {
        "success": True,
        "release_name": release_name,
        "backup": str(backup_path),
        "snapshot_id": _snapshot_id(backup_path),
        "plugin_registered": registered,
        "plugin_path": plugin_path,
        "previous_target": previous_target,
        "symlink": str(link),
        "message": f"Activated release {release_name} (backup {backup_path.name}).",
    }


def rollback_release(
    backup_name: str,
    *,
    hr_symlink: Path | None = None,
    config_dir: Path | None = None,
) -> dict[str, Any]:
    """Roll back a release activation.

    Restores the previous symlink target (removes the link when it did not
    exist before activation), rolls back the FastDraw files through the T7
    ``rollback`` primitive, and restores opencode.jsonc byte-for-byte from the
    activation blob (or deletes it when the activation created it). Backups
    without a release-activation ledger are refused.
    """
    link = Path(hr_symlink) if hr_symlink is not None else HR_SYMLINK
    cfg_dir = Path(config_dir) if config_dir is not None else get_config_dir()
    config_file = cfg_dir / CONFIG_FILENAME
    backup_dir = cfg_dir / BACKUP_DIR
    name_path = Path(backup_name)
    if name_path.is_absolute():
        # The caller may pass the absolute path returned by activate_release;
        # it is only acceptable when it resolves INSIDE the backups dir.
        try:
            backup_path = contained(name_path, backup_dir)
        except UnsafePathError as exc:
            return {
                "success": False,
                "error": f"unsafe backup name {backup_name!r}: {exc}",
            }
    else:
        try:
            safe_component(backup_name)
        except UnsafeNameError as exc:
            return {
                "success": False,
                "error": f"unsafe backup name {backup_name!r}: {exc}",
            }
        backup_path = backup_dir / backup_name
    if not backup_path.is_dir():
        return {"success": False, "error": f"Backup '{backup_name}' not found"}
    ledger_path = backup_path / ACTIVATION_LEDGER
    if not ledger_path.is_file():
        return {
            "success": False,
            "error": (
                f"backup '{backup_name}' is not a release activation backup "
                f"(no {ACTIVATION_LEDGER}); use apply-rollback for FastDraw backups"
            ),
        }
    try:
        ledger = json.loads(ledger_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return {
            "success": False,
            "error": f"corrupt release activation ledger in '{backup_name}': {exc}",
        }

    previous_target = ledger.get("previous_symlink_target")
    previous_existed = bool(ledger.get("previous_symlink_existed"))
    config_existed = bool(ledger.get("config_existed_before"))

    if previous_existed:
        absolute_at_activation = ledger.get("previous_symlink_target_absolute") is True
        if (
            not isinstance(previous_target, str)
            or not previous_target
            or not previous_target.startswith(os.sep)
            or not absolute_at_activation
        ):
            return {
                "success": False,
                "error": (
                    f"corrupt release activation ledger in '{backup_name}': "
                    "previous_symlink_target is missing, relative, or was not "
                    "recorded as existing at activation time; refusing to "
                    "restore the symlink"
                ),
            }

    t7 = rollback(backup_name, config_dir=cfg_dir)
    if not t7["success"]:
        return {"success": False, "error": t7["error"], "t7": t7}

    _restore_config_file(config_file, backup_path, config_existed)
    _restore_link(link, previous_target, previous_existed)

    restored_target = previous_target if previous_existed else None
    return {
        "success": True,
        "restored_from": backup_name,
        "symlink_target": restored_target,
        "message": (
            f"Rolled back release activation from '{backup_name}'. "
            f"FastDraw files restored ({len(t7.get('restored', []))} restored, "
            f"{len(t7.get('deleted', []))} deleted); symlink restored to "
            f"{restored_target or '<absent>'}; opencode config restored."
        ),
    }


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def _release_age(release_path: Path, metadata: dict[str, Any]) -> timedelta:
    created = metadata.get("created_at")
    if isinstance(created, str):
        try:
            created_at = datetime.fromisoformat(created)
        except ValueError:
            created_at = None
        if created_at is not None:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) - created_at
    mtime = datetime.fromtimestamp(release_path.stat().st_mtime, tz=timezone.utc)
    return datetime.now(timezone.utc) - mtime


def _entry_names(entries: list[dict[str, Any]]) -> list[str]:
    return [e["name"] for e in entries]


def enforce_retention_policy(
    releases_root: Path | None = None,
    archive_dir: Path | None = None,
    config_dir: Path | None = None,
    hr_symlink: Path | None = None,
    *,
    max_releases: int = MAX_RELEASES_TO_KEEP,
    max_age_days: int = MAX_RELEASE_AGE_DAYS,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Enforce bounded release history (retention mirrors the T7 policy).

    Keeps at most ``max_releases`` releases no older than ``max_age_days``,
    ALWAYS preserving the newest VALID release and the ACTIVE release
    (symlink target); releases beyond the bounds are archived (tar.gz into
    ``archive_dir``, default ``<releases_root>/../archive``) and the release
    directory is removed. Archives beyond the same bounds are removed (newest
    archive preserved; an archive inherits its release's age via mtime).
    Corrupt releases (no metadata.json) are never kept. When ``config_dir``
    is given, plugin entries pointing at releases that no longer exist are
    removed behind a T7 backup.
    """
    root = Path(releases_root) if releases_root is not None else RELEASES_DIR
    arch = (
        Path(archive_dir)
        if archive_dir is not None
        else root.parent / "archive"
    )

    if not root.is_dir():
        return {
            "action": "none",
            "dry_run": dry_run,
            "releases": {
                "kept": [],
                "archived": [],
                "to_archive": [],
                "corrupt": [],
                "foreign": [],
            },
            "archives": {"removed": [], "kept": []},
            "plugin_entries": {"removed": [], "backup": None},
        }

    active_name: str | None = None
    if hr_symlink is not None and Path(hr_symlink).is_symlink():
        try:
            target = Path(os.readlink(Path(hr_symlink))).resolve()
            rel = target.relative_to(root.resolve())
            active_name = rel.parts[0]
        except (ValueError, OSError):
            active_name = None

    valid_entries: list[tuple[Path, dict[str, Any]]] = []
    corrupt: list[str] = []
    foreign: list[str] = []
    for item in sorted(root.iterdir(), reverse=True):
        if not item.is_dir():
            continue
        try:
            safe_component(item.name)
            contained(item, root)
        except (UnsafeNameError, UnsafePathError):
            # Security: entries with escaped names or symlinks pointing out
            # of the releases root are flagged, never removed.
            foreign.append(item.name)
            continue
        metadata_path = item / "metadata.json"
        if not metadata_path.is_file():
            corrupt.append(item.name)
            continue
        try:
            metadata = json.loads(metadata_path.read_text())
        except (json.JSONDecodeError, OSError):
            corrupt.append(item.name)
            continue
        valid_entries.append((item, metadata))
    if corrupt and not dry_run:
        for name in corrupt:
            shutil.rmtree(root / name, ignore_errors=True)

    newest_valid = valid_entries[0][0].name if valid_entries else None
    kept: list[str] = []
    to_archive: list[str] = []
    for item, metadata in valid_entries:
        is_newest = item.name == newest_valid
        is_active = item.name == active_name
        within_count = len(kept) < max_releases
        within_age = _release_age(item, metadata) <= timedelta(days=max_age_days)
        if is_newest or is_active or (within_count and within_age):
            kept.append(str(item))
        else:
            to_archive.append(item.name)

    archived: list[dict[str, Any]] = []
    for name in to_archive:
        item = root / name
        archive_file = arch / f"{ARCHIVE_PREFIX}{name}.tar.gz"
        if dry_run:
            archived.append({"name": name, "action": "would_archive"})
            continue
        arch.mkdir(parents=True, exist_ok=True)
        created = _release_age(item, json.loads((item / "metadata.json").read_text()))
        with tarfile.open(archive_file, "w:gz") as tar:
            tar.add(item, arcname=name)
        # The archive inherits its release's age (mtime) so stale releases do
        # not live forever as fresh-looking archives.
        try:
            stamp = datetime.now(timezone.utc) - created
            os.utime(archive_file, (stamp.timestamp(), stamp.timestamp()))
        except OSError:
            pass
        try:
            contained(item, root)
        except UnsafePathError:
            continue
        shutil.rmtree(item, ignore_errors=True)
        archived.append({"name": name, "action": "archived"})

    archives_removed: list[str] = []
    archives_kept: list[str] = []
    if arch.is_dir():
        tarballs = sorted(arch.glob(f"{ARCHIVE_PREFIX}*.tar.gz"), reverse=True)
        newest_archive = tarballs[0].name if tarballs else None
        for tarball in tarballs:
            is_newest = tarball.name == newest_archive
            within_count = len(archives_kept) < max_releases
            mtime = datetime.fromtimestamp(tarball.stat().st_mtime, tz=timezone.utc)
            within_age = datetime.now(timezone.utc) - mtime <= timedelta(days=max_age_days)
            if is_newest or (within_count and within_age):
                archives_kept.append(tarball.name)
            else:
                archives_removed.append(tarball.name)
        if not dry_run:
            for name in archives_removed:
                (arch / name).unlink(missing_ok=True)

    plugin_removed: list[str] = []
    plugin_backup: str | None = None
    plugin_error: str | None = None
    if config_dir is not None:
        cfg_dir = Path(config_dir)
        config_file = cfg_dir / CONFIG_FILENAME
        release_names = {item.name for item, _ in valid_entries}
        stale = _stale_plugin_entries(_config_entries(config_file), root, release_names)
        if stale:
            if dry_run:
                plugin_removed = stale
            else:
                backup_path = create_backup(config_dir=cfg_dir)
                plugin_backup = backup_path.name
                try:
                    _, plugin_removed = _remove_plugin_entries(config_file, stale)
                except ValueError as exc:
                    plugin_error = str(exc)
                    plugin_removed = []
            if plugin_error:
                plugin_backup = None

    return {
        "action": "enforced" if (to_archive or archives_removed or plugin_removed or corrupt) else "none",
        "dry_run": dry_run,
        "releases": {
            "kept": kept,
            "archived": archived,
            "to_archive": to_archive,
            "corrupt": corrupt,
            "foreign": foreign,
        },
        "archives": {"removed": archives_removed, "kept": archives_kept},
        "plugin_entries": {
            "removed": plugin_removed,
            "backup": plugin_backup,
            "error": plugin_error,
        },
    }


# ---------------------------------------------------------------------------
# CLI (mirrors hr.cli_apply: self-contained commands + a register helper)
# ---------------------------------------------------------------------------


def _releases_root() -> Path:
    return Path(os.environ.get("HR_RELEASES_DIR") or RELEASES_DIR)


def _hr_symlink() -> Path:
    return Path(os.environ.get("HR_HR_SYMLINK") or HR_SYMLINK)


def release_build(
    name: Optional[str] = typer.Option(
        None, "--name", help="release name (default: release-<UTC timestamp>)"
    ),
) -> None:
    """Build a release candidate from the manifest surface (workspace: cwd or $HR_WORKSPACE)."""

    workspace = Path(os.environ.get("HR_WORKSPACE") or ".")
    result = build_release(workspace, _releases_root(), name)
    if not result["success"]:
        _fail(f"error: {result['error']}")
    console.print_json(json.dumps(result, default=str))


def release_verify(
    name: str = typer.Argument(...),
    keep: bool = typer.Option(
        False, "--keep", help="keep the candidate even when verification fails"
    ),
) -> None:
    """Re-hash every payload snapshot-locally; failed verification removes the candidate."""

    result = verify_release(name, _releases_root(), remove_on_failure=not keep)
    if not result["valid"]:
        _fail(
            f"release {name}: INVALID ({result.get('error', 'verification failed')}); "
            f"candidate {'removed' if result.get('removed') else 'retained'}"
        )
    console.print(
        f"release {name}: valid (manifest {result['manifest_hash']}, "
        f"{result['payload_count']} payloads)"
    )


def release_activate(
    name: str = typer.Argument(...),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Activate a verified release: T7-backup the config, atomically swap the hr symlink, register the plugin path."""

    result = activate_release(
        name,
        releases_root=_releases_root(),
        hr_symlink=_hr_symlink(),
        dry_run=dry_run,
    )
    if not result["success"]:
        _fail(f"error: {result['error']}")
    console.print_json(json.dumps(result, default=str))


def release_rollback(backup: str = typer.Argument(...)) -> None:
    """Roll back a release activation: restore the previous symlink target and the config."""

    result = rollback_release(backup, hr_symlink=_hr_symlink())
    if not result["success"]:
        _fail(f"error: {result['error']}")
    console.print(result["message"])


def release_list() -> None:
    """List releases (newest first) with their verification state."""

    console.print_json(json.dumps(list_releases(_releases_root()), default=str))


def release_prune(
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Enforce bounded release/archive history and clean stale plugin entries."""

    result = enforce_retention_policy(
        _releases_root(),
        config_dir=get_config_dir(),
        hr_symlink=_hr_symlink(),
        dry_run=dry_run,
    )
    if result.get("plugin_entries", {}).get("error"):
        print(
            f"warning: plugin cleanup skipped: {result['plugin_entries']['error']}",
            file=sys.stderr,
        )
    console.print(
        f"releases kept {len(result['releases']['kept'])}; "
        f"archived {len(result['releases']['archived'])}; "
        f"corrupt {len(result['releases']['corrupt'])}; "
        f"archives removed {len(result['archives']['removed'])}; "
        f"plugin entries removed {len(result['plugin_entries']['removed'])}"
    )


def register_release_commands(tp: typer.Typer) -> None:
    """Attach the release-lifecycle commands to a typer app (mirrors
    hr.cli_apply.register_apply_commands; wired into the shipped app by
    hr/cli.py, the unified CLI facade)."""
    tp.command(name="release-build")(release_build)
    tp.command(name="release-verify")(release_verify)
    tp.command(name="release-activate")(release_activate)
    tp.command(name="release-rollback")(release_rollback)
    tp.command(name="release-list")(release_list)
    tp.command(name="release-prune")(release_prune)


__all__ = [
    "HR_SYMLINK",
    "MANIFEST_VERSION",
    "MAX_AGE_DAYS",
    "MAX_RELEASES_TO_KEEP",
    "MAX_RELEASE_AGE_DAYS",
    "RELEASES_DIR",
    "activate_release",
    "build_release",
    "compute_release_hash",
    "enforce_retention_policy",
    "list_releases",
    "register_release_commands",
    "release_activate",
    "release_build",
    "release_list",
    "release_prune",
    "release_rollback",
    "release_verify",
    "rollback_release",
    "verify_release",
]