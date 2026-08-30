"""Plugin safety: transactional preview, backup, rollback, and compatibility checks.

Every ``hr apply`` write goes through this module so the FastDraw config files
under the opencode config dir can be recovered:

- ``create_backup()`` snapshots the current files into a manifest JSON
  (snapshot id, per-file presence + SHA-256) plus byte blobs.
- ``safe_apply()`` refuses on compatibility mismatch or preview drift, and
  AUTO-RESTORES the backup when the apply fails after any write.
- ``rollback()`` restores pre-existing files byte-for-byte and DELETES files
  that did not exist before the apply; corrupt backups refuse restoration.
- ``prune_backups()`` bounds retention (see MAX_BACKUPS / MAX_AGE_DAYS).

The FastDraw file contract itself is owned by ``hr.apply``; this module owns
only the safety envelope around it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg2

from hr.config import opencode_config_dir


# ---------------------------------------------------------------------------
# R3/C3-C5: shared name/path confinement validators
# ---------------------------------------------------------------------------


class UnsafeNameError(ValueError):
    """A caller-supplied name is not a safe single path component.

    Shared by the release machinery (hr.deployment_manager) and the apply
    machinery (this module): release names, backup names and manifest file
    keys all cross the same trust boundary (CLI arguments, opencode plugin
    tool arguments, on-disk backup manifests) and are rejected with the same
    operator-friendly contract.
    """


class UnsafePathError(ValueError):
    """A path resolves outside its confinement root."""


def safe_component(name: str) -> str:
    """Validate a caller-supplied name as a single, non-escaping path component.

    Refuses empty names, the special ``'.'``/``'..'`` components, any path
    separator (``os.sep``/``os.altsep``) and absolute paths — everything that
    could make a later ``root / name`` join escape ``root``. Returns the name
    unchanged on success.
    """
    if not isinstance(name, str) or not name:
        raise UnsafeNameError("name must be a non-empty string")
    if name in (".", ".."):
        raise UnsafeNameError(f"name {name!r} must not be '.' or '..'")
    for sep in (os.sep, os.altsep):
        if sep is not None and sep in name:
            raise UnsafeNameError(f"name {name!r} must not contain a path separator")
    if Path(name).name != name:
        raise UnsafeNameError(f"name {name!r} must be a single path component")
    return name


def contained(target: Path, root: Path) -> Path:
    """Resolve ``target`` (symlinks followed) and require it inside ``root``.

    ``target`` may be the root itself (``target == root``). Resolution is
    non-strict so nonexistent targets (e.g. a file a restore is about to
    create) are still judged by their resolved location. Returns the resolved
    (canonicalized) target on success; raises ``UnsafePathError`` otherwise.
    """
    root_resolved = root.resolve(strict=False)
    target_resolved = target.resolve(strict=False)
    if target_resolved != root_resolved and not target_resolved.is_relative_to(
        root_resolved
    ):
        raise UnsafePathError(
            f"path {target} resolves outside its root {root}; refusing"
        )
    return target_resolved


def _inside(target: Path, root: Path) -> bool:
    """True when ``contained`` would accept the target (foreign/following
    entries — e.g. a symlink pointing out of the directory — are skipped)."""
    try:
        contained(target, root)
    except ValueError:
        return False
    return True


PRESETS_FILENAME = "fastdraw-presets.json"
STATE_FILENAME = ".fastdraw.json"
BACKUP_DIR = "hr-apply-backups"
PREVIEW_SUBDIR = "previews"
MANIFEST_FILENAME = "manifest.json"

# Revisable retention defaults (plan row 7, recorded with rationale instead of
# appearing as magic constants): keep at most 10 recovery snapshots, drop
# anything older than 30 days, and ALWAYS preserve the newest valid recovery
# point even when it breaches either bound.
MAX_BACKUPS = 10
MAX_AGE_DAYS = 30

# HR's declared version of the FastDraw file contract (the JSON shapes
# hr/apply.py writes: presets store, isModelMap "/" rule, boot-time state).
# The authoritative declaration of that same contract lives in
# fastdraw/package.json ("version"); a mismatch means FastDraw may parse or
# write a different shape than this bridge produces, so apply refuses.
HR_FASTDRAW_SCHEMA_VERSION = "1.0.0"


def get_config_dir() -> Path:
    """Get the OpenCode config directory."""
    return Path(opencode_config_dir())


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _evidence_read_error(action: str, exc: Exception) -> str:
    """Concise, actionable refusal message for a failed evidence read.

    Mirrors the T6 ``_read_evidence`` philosophy: unreadable evidence degrades
    to a clean refusal, never a crash. The known live-DB drift is a missing
    ``hr.separation.directional`` column; any other unreadable read reports
    its type and message instead of leaking a raw traceback.
    """
    pg_errors = getattr(psycopg2, "errors", None)
    undefined_column = (
        getattr(pg_errors, "UndefinedColumn", ()) if pg_errors is not None else ()
    )
    if undefined_column and isinstance(exc, undefined_column):
        diag = getattr(exc, "diag", None)
        table = getattr(diag, "table_name", None) if diag is not None else None
        column = getattr(diag, "column_name", None) if diag is not None else None
        if table and column:
            detail = f"live schema missing {table}.{column}"
        elif column:
            detail = f"live schema missing column {column}"
        else:
            detail = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return f"cannot read {action}: {detail} (run schema migration)"
    return f"cannot read {action}: {type(exc).__name__}: {exc}"


def _snapshot_files(config_dir: Path) -> dict[str, Path]:
    return {
        PRESETS_FILENAME: config_dir / PRESETS_FILENAME,
        STATE_FILENAME: config_dir / STATE_FILENAME,
    }


def preview_apply(
    preset_name: str | None = None,
    include_state: bool = False,
    conn=None,
    *,
    record_preview: bool = False,
    config_dir: Path | None = None,
) -> dict[str, Any]:
    """Preview what changes hr apply would make without actually applying.

    With ``record_preview=True`` the current per-file SHA-256s are persisted as
    a preview record: the next ``safe_apply()`` refuses if those files drifted
    in the meantime (preview-to-apply binding).

    Returns a dict with:
    - current_preset: current preset content (if exists)
    - current_state: current state content (if exists and include_state=True)
    - new_preset: what the new preset would look like
    - new_state: what the new state would look like (if include_state=True)
    - changes: list of what would change
    - sweep_id: the sweep the seating comes from
    - preview_record: path of the binding record (only when record_preview=True)
    """
    cfg_dir = Path(config_dir) if config_dir is not None else get_config_dir()
    presets_path = cfg_dir / PRESETS_FILENAME
    state_path = cfg_dir / STATE_FILENAME

    current_preset = None
    current_state = None

    if presets_path.exists():
        with open(presets_path) as f:
            current_preset = json.load(f)

    if include_state and state_path.exists():
        with open(state_path) as f:
            current_state = json.load(f)

    from hr.apply import agents_from_assignments, latest_assignments  # avoid circular deps

    owns_connection = conn is None
    try:
        if conn is None:
            from hr.db import connect

            conn = connect()
        assignments, sweep_id = latest_assignments(conn)
        agents = agents_from_assignments(assignments)

        target = preset_name or f"hr-verdict-{sweep_id[:8]}"

        new_preset = {
            "presets": {
                target: {
                    "description": f"hr verdict seating from sweep {sweep_id}",
                    "createdAt": datetime.now(timezone.utc).isoformat(),
                    "agents": agents,
                }
            }
        }
        if current_preset and "presets" in current_preset:
            new_preset["presets"].update(current_preset["presets"])
            new_preset["presets"][target] = {
                "description": f"hr verdict seating from sweep {sweep_id}",
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "agents": agents,
            }

        new_state = {"agents": agents} if include_state else None

        changes = []
        if current_preset is None:
            changes.append(f"Would create new preset '{target}'")
        elif target in (current_preset.get("presets") or {}):
            changes.append(f"Would update existing preset '{target}'")
        else:
            changes.append(f"Would add new preset '{target}'")

        if include_state:
            if current_state is None:
                changes.append("Would create new .fastdraw.json state file")
            else:
                changes.append("Would update .fastdraw.json state file")

        result = {
            "current_preset": current_preset,
            "current_state": current_state,
            "new_preset": new_preset,
            "new_state": new_state,
            "changes": changes,
            "sweep_id": sweep_id,
        }

        if record_preview:
            record = {
                "preview_id": uuid.uuid4().hex,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "preset_name": target,
                "sweep_id": sweep_id,
                "files": {
                    name: _sha256(path) for name, path in _snapshot_files(cfg_dir).items()
                },
            }
            record_dir = cfg_dir / BACKUP_DIR / PREVIEW_SUBDIR
            record_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            record_path = record_dir / f"preview-{stamp}-{uuid.uuid4().hex[:8]}.json"
            record_path.write_text(json.dumps(record, indent=2) + "\n")
            result["preview_record"] = str(record_path)

        return result
    except Exception as exc:
        # Unreadable evidence → clean refusal, never a leak of the raw driver
        # exception (mirrors the T6 _read_evidence philosophy: live DBs may
        # predate columns the contract-test schema has — e.g. the live
        # hr.separation table has no `directional` column).
        return {"success": False, "error": _evidence_read_error("seat assignments", exc)}
    finally:
        if owns_connection and conn is not None:
            conn.close()


def _latest_preview_record(config_dir: Path) -> dict[str, Any] | None:
    record_dir = config_dir / BACKUP_DIR / PREVIEW_SUBDIR
    if not record_dir.exists():
        return None
    records = sorted(record_dir.glob("preview-*.json"), reverse=True)
    if not records:
        return None
    try:
        record = json.loads(records[0].read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return record if isinstance(record, dict) else None


def ensure_no_preview_drift(config_dir: Path | None = None) -> dict[str, Any]:
    """Compare the latest preview record against the current files.

    Returns ``{"ok": bool, "record": ..., "drifted": [names]}``. A preview
    record exists (apply-preview was run) and any tracked file changed since
    → ok=False; the apply must refuse (what was previewed must be applied).
    """
    cfg_dir = Path(config_dir) if config_dir is not None else get_config_dir()
    record = _latest_preview_record(cfg_dir)
    if record is None:
        return {"ok": True, "record": None, "drifted": []}
    drifted = [
        name
        for name, recorded_hash in (record.get("files") or {}).items()
        if _sha256(cfg_dir / name) != recorded_hash
    ]
    return {"ok": not drifted, "record": record, "drifted": drifted}


def _consume_preview_records(config_dir: Path) -> int:
    record_dir = config_dir / BACKUP_DIR / PREVIEW_SUBDIR
    if not record_dir.exists():
        return 0
    removed = 0
    for record in record_dir.glob("preview-*.json"):
        record.unlink()
        removed += 1
    try:
        record_dir.rmdir()
    except OSError:
        pass
    return removed


def create_backup(
    backup_name: str | None = None,
    *,
    prune: bool = True,
    config_dir: Path | None = None,
) -> Path:
    """Snapshot the current FastDraw files into a manifest + blobs backup.

    Returns the path to the backup directory. The manifest records, per file,
    whether it existed before (so rollback can delete newly-created files) and
    its SHA-256 (so restore can verify the blob is untampered), plus a unique
    snapshot identity.
    """
    cfg_dir = Path(config_dir) if config_dir is not None else get_config_dir()
    if backup_name is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_name = f"backup-{stamp}-{uuid.uuid4().hex[:8]}"
    try:
        safe_component(backup_name)
    except UnsafeNameError as exc:
        raise UnsafeNameError(f"unsafe backup name {backup_name!r}: {exc}") from exc
    backup_dir = cfg_dir / BACKUP_DIR
    backup_dir.mkdir(exist_ok=True)

    backup_path = backup_dir / backup_name
    if backup_path.exists() and (backup_path / MANIFEST_FILENAME).exists():
        raise ValueError(f"backup '{backup_name}' already exists")
    backup_path.mkdir(exist_ok=True)

    files = _snapshot_files(cfg_dir)
    manifest = {
        "snapshot_id": uuid.uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "retention": {
            "max_backups": MAX_BACKUPS,
            "max_age_days": MAX_AGE_DAYS,
            "note": "revisable defaults: keep at most 10 snapshots, drop older than 30 "
            "days, always preserve the newest valid recovery point",
        },
        "files": {
            name: {"existed_before": path.exists(), "sha256": _sha256(path)}
            for name, path in files.items()
        },
    }
    for name, path in files.items():
        if path.exists():
            shutil.copy2(path, backup_path / name)
    (backup_path / MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2) + "\n")

    if prune:
        prune_backups(config_dir=cfg_dir)
    return backup_path


def _validate_backup(backup_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Parse the manifest and verify every blob: (manifest, None) or (None, error)."""
    manifest_path = backup_path / MANIFEST_FILENAME
    if not manifest_path.exists():
        return None, f"corrupt backup: missing {MANIFEST_FILENAME} (not a backup snapshot)"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return None, f"corrupt backup: manifest not readable JSON ({exc})"
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        return None, "corrupt backup: manifest has an invalid shape (missing files map)"
    if not isinstance(manifest.get("snapshot_id"), str) or not manifest["snapshot_id"]:
        return None, "corrupt backup: manifest missing snapshot_id"
    for name, info in manifest["files"].items():
        try:
            safe_component(name)
        except ValueError:
            return (
                None,
                f"corrupt backup: manifest file name {name!r} is not a safe "
                "single path component (would escape the config dir)",
            )
        if not isinstance(info, dict):
            return None, f"corrupt backup: manifest entry for {name!r} is not an object"
        if info.get("existed_before"):
            blob = backup_path / name
            if not blob.exists():
                return None, (
                    f"corrupt backup: blob for {name!r} missing "
                    f"(snapshot {manifest['snapshot_id']})"
                )
            if _sha256(blob) != info.get("sha256"):
                return None, (
                    f"corrupt backup: blob for {name!r} hash mismatch "
                    f"(snapshot {manifest['snapshot_id']})"
                )
    return manifest, None


def list_backups() -> list[dict[str, Any]]:
    """List all available backups, newest first."""
    config_dir = get_config_dir()
    backup_dir = config_dir / BACKUP_DIR

    if not backup_dir.exists():
        return []

    backups = []
    for backup_path in sorted(backup_dir.iterdir(), reverse=True):
        if (
            not backup_path.is_dir()
            or backup_path.name == PREVIEW_SUBDIR
            or not _inside(backup_path, backup_dir)
        ):
            continue
        manifest, _ = _validate_backup(backup_path)
        backups.append(
            {
                "name": backup_path.name,
                "path": str(backup_path),
                "has_presets": (backup_path / PRESETS_FILENAME).exists(),
                "has_state": (backup_path / STATE_FILENAME).exists(),
                "valid": manifest is not None,
                "created_at": manifest["created_at"] if manifest else None,
                "snapshot_id": manifest["snapshot_id"] if manifest else None,
            }
        )

    return backups


def _restore_from_backup(
    backup_path: Path,
    config_dir: Path,
) -> dict[str, Any]:
    """Restore files from a validated backup snapshot.

    Files that existed before the apply are restored byte-for-byte from their
    verified blobs; files that did not exist before are DELETED. Corrupt
    backups (missing/tampered manifest, missing blob, hash mismatch) are
    refused BEFORE any write.
    """
    manifest, error = _validate_backup(backup_path)
    if error is not None:
        return {"success": False, "error": error}
    assert manifest is not None  # validated above

    restored: list[str] = []
    deleted: list[str] = []
    failed: list[str] = []
    for name, info in manifest["files"].items():
        try:
            target = contained(config_dir / name, config_dir)
        except ValueError as exc:
            failed.append(f"{name} ({exc})")
            continue
        try:
            if info.get("existed_before"):
                shutil.copy2(backup_path / name, target)
                restored.append(name)
            elif target.exists():
                target.unlink()
                deleted.append(name)
        except OSError as exc:
            failed.append(f"{name} ({exc})")

    if failed:
        return {
            "success": False,
            "error": "restore failed for: " + "; ".join(failed),
            "restored": restored,
            "deleted": deleted,
            "snapshot_id": manifest["snapshot_id"],
        }
    return {
        "success": True,
        "restored": restored,
        "deleted": deleted,
        "snapshot_id": manifest["snapshot_id"],
    }


def rollback(
    backup_name: str,
    *,
    config_dir: Path | None = None,
) -> dict[str, Any]:
    """Rollback to a specific backup.

    Returns a dict with the result of the rollback operation.
    """
    cfg_dir = Path(config_dir) if config_dir is not None else get_config_dir()
    backup_dir = cfg_dir / BACKUP_DIR
    name_path = Path(backup_name)
    if name_path.is_absolute():
        # The caller may pass the absolute path returned by create_backup;
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

    if not backup_path.exists() or not backup_path.is_dir():
        return {"success": False, "error": f"Backup '{backup_name}' not found"}

    result = _restore_from_backup(backup_path, cfg_dir)
    if not result["success"]:
        return {"success": False, "error": result["error"]}

    message = f"Rolled back to backup '{backup_name}' (snapshot {result['snapshot_id']})."
    if result["deleted"]:
        message += (
            " Removed newly-created files that did not exist before the apply: "
            + ", ".join(result["deleted"])
            + "."
        )
    message += " Restart OpenCode to apply state changes."

    return {
        "success": True,
        "restored_from": backup_name,
        "snapshot_id": result["snapshot_id"],
        "message": message,
        "restored": result["restored"],
        "deleted": result["deleted"],
    }


def _backup_age(backup_path: Path, manifest: dict[str, Any]) -> timedelta:
    try:
        created = datetime.fromisoformat(manifest["created_at"])
    except (KeyError, ValueError, TypeError):
        created = datetime.fromtimestamp(backup_path.stat().st_mtime, tz=timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created


def prune_backups(
    *,
    max_backups: int = MAX_BACKUPS,
    max_age_days: int = MAX_AGE_DAYS,
    config_dir: Path | None = None,
) -> dict[str, Any]:
    """Drop backups beyond the retention bounds.

    Retention rule (revisable defaults, see MAX_BACKUPS / MAX_AGE_DAYS):
    keep at most ``max_backups`` snapshots, drop anything older than
    ``max_age_days``, always preserve the newest VALID recovery point, and
    never keep a corrupt (unrestorable) snapshot.
    """
    cfg_dir = Path(config_dir) if config_dir is not None else get_config_dir()
    backup_dir = cfg_dir / BACKUP_DIR
    if not backup_dir.exists():
        return {"removed": [], "kept": []}

    snapshots = [
        p
        for p in backup_dir.iterdir()
        if p.is_dir()
        and p.name != PREVIEW_SUBDIR
        and _inside(p, backup_dir)
    ]
    snapshots.sort(key=lambda p: p.name, reverse=True)

    valid: list[tuple[Path, dict[str, Any]]] = []
    corrupt: list[Path] = []
    for snapshot in snapshots:
        manifest, error = _validate_backup(snapshot)
        if manifest is not None:
            valid.append((snapshot, manifest))
        else:
            corrupt.append(snapshot)

    newest_valid = valid[0][0] if valid else None
    removed = [p.name for p in corrupt]
    kept: list[str] = []

    for snapshot, manifest in valid:  # newest first
        if len(kept) >= max_backups:
            removed.append(snapshot.name)
        elif snapshot == newest_valid or _backup_age(snapshot, manifest) <= timedelta(
            days=max_age_days
        ):
            kept.append(snapshot.name)
        else:
            removed.append(snapshot.name)

    for name in removed:
        path = backup_dir / name
        if not _inside(path, backup_dir):
            continue
        shutil.rmtree(path, ignore_errors=True)

    return {"removed": removed, "kept": kept}


def check_compatibility(
    plugin_package_json: Path | None = None,
) -> dict[str, Any]:
    """Check that the FastDraw plugin's declared schema version matches ours.

    The HR bridge declares ``HR_FASTDRAW_SCHEMA_VERSION`` (the file-contract
    version it writes); fastdraw/package.json's ``version`` is the plugin's
    authoritative declaration of the same contract. The check fails loudly on
    mismatch and fails closed (refuses) when the declaration is unreadable.
    """
    from hr import __version__ as hr_version

    if plugin_package_json is None:
        plugin_package_json = Path(__file__).parent.parent / "fastdraw" / "package.json"

    plugin_version = "unknown"
    if plugin_package_json.exists():
        try:
            with open(plugin_package_json) as f:
                package_data = json.load(f)
            plugin_version = str(package_data.get("version", "unknown"))
        except (json.JSONDecodeError, OSError):
            plugin_version = "unknown"

    warnings: list[str] = []
    if plugin_version == "unknown":
        warnings.append(
            "Could not determine FastDraw plugin version (missing or unreadable "
            "fastdraw/package.json)"
        )
    if plugin_version != HR_FASTDRAW_SCHEMA_VERSION:
        warnings.append(
            f"FastDraw plugin version {plugin_version} does not match the schema "
            f"version this HR build writes ({HR_FASTDRAW_SCHEMA_VERSION})"
        )

    return {
        "compatible": plugin_version == HR_FASTDRAW_SCHEMA_VERSION,
        "hr_version": hr_version,
        "plugin_version": plugin_version,
        "expected_schema_version": HR_FASTDRAW_SCHEMA_VERSION,
        "warnings": warnings,
    }


def safe_apply(
    preset_name: str | None = None,
    include_state: bool = False,
    dry_run: bool = False,
    create_backup_before: bool = True,
    conn=None,
    *,
    config_dir: Path | None = None,
    plugin_package_json: Path | None = None,
) -> dict[str, Any]:
    """Safely apply HR verdict with compatibility, preview-binding, and rollback.

    Flow: compatibility check (refuses on mismatch) → preview-drift guard
    (refuses if files changed since ``apply-preview``) → backup snapshot →
    apply → on any failure AUTO-RESTORE the backup byte-for-byte. A successful
    apply consumes the preview records (the binding is satisfied).
    """
    cfg_dir = Path(config_dir) if config_dir is not None else get_config_dir()

    compat = check_compatibility(plugin_package_json=plugin_package_json)
    if not compat["compatible"]:
        reason = compat["warnings"][0] if compat["warnings"] else "version mismatch"
        return {
            "success": False,
            "error": f"Compatibility check failed: {reason}",
            "compatibility": compat,
        }

    drift = ensure_no_preview_drift(cfg_dir)
    if not drift["ok"]:
        return {
            "success": False,
            "error": (
                "preview drift: FastDraw files changed since apply-preview "
                f"({', '.join(drift['drifted'])}); re-run apply-preview"
            ),
            "drift": drift,
        }

    preview = preview_apply(preset_name, include_state, conn=conn, config_dir=cfg_dir)
    if preview.get("success") is False:
        return {"success": False, "error": preview["error"], "preview": preview}

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "preview": preview,
            "message": "Dry run complete. No changes made.",
        }

    backup_path = None
    if create_backup_before:
        backup_path = create_backup(config_dir=cfg_dir)

    owns_connection = conn is None
    if conn is None:
        from hr.db import connect

        try:
            conn = connect()
        except Exception as exc:
            return {
                "success": False,
                "error": _evidence_read_error("seat assignments", exc),
            }
    try:
        from hr.apply import apply

        result = apply(
            conn,
            preset_name=preset_name,
            set_state=include_state,
            config_dir=cfg_dir,
        )
        consumed = _consume_preview_records(cfg_dir)
        return {
            "success": True,
            "result": result,
            "backup": str(backup_path) if backup_path else None,
            "snapshot_id": _snapshot_id(backup_path),
            "preview": preview,
            "preview_records_consumed": consumed,
            "message": "Apply completed successfully.",
        }
    except Exception as e:
        restore: dict[str, Any] = {"success": False, "error": "no backup was created"}
        if backup_path is not None:
            restore = _restore_from_backup(backup_path, cfg_dir)
        error = str(e)
        if isinstance(e, psycopg2.Error):
            error = _evidence_read_error("seat assignments", e)
        if not restore["success"]:
            error += f"; auto-restore failed: {restore['error']}"
        return {
            "success": False,
            "error": error,
            "backup": str(backup_path) if backup_path else None,
            "snapshot_id": _snapshot_id(backup_path),
            "restore": restore,
            "message": "Apply failed; backup restored.",
        }
    finally:
        if owns_connection:
            conn.close()


def _snapshot_id(backup_path: Path | None) -> str | None:
    if backup_path is None:
        return None
    try:
        manifest = json.loads((backup_path / MANIFEST_FILENAME).read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return manifest.get("snapshot_id") if isinstance(manifest, dict) else None


__all__ = [
    "HR_FASTDRAW_SCHEMA_VERSION",
    "MAX_AGE_DAYS",
    "MAX_BACKUPS",
    "UnsafeNameError",
    "UnsafePathError",
    "check_compatibility",
    "contained",
    "create_backup",
    "ensure_no_preview_drift",
    "list_backups",
    "preview_apply",
    "prune_backups",
    "rollback",
    "safe_apply",
    "safe_component",
]