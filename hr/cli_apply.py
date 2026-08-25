"""Apply-family CLI commands: transactional FastDraw apply + preview/rollback/prune.

Self-contained at import time (stdlib + typer + rich only): the shared typer
app lives in ``hr.cli_app``, which is not tracked at some fresh HEAD checkouts
(T8 source-control disposition). Commands therefore never import it at module
level — they attach to the worktree app when it is importable, and
``register_apply_commands()`` lets callers and tests attach them to any app.
"""

from __future__ import annotations

import json
import sys
from typing import Optional

import typer
from rich.console import Console

console = Console()


def _fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise typer.Exit(code=1)


def _with_conn(builder) -> None:
    try:
        from . import cli as _cli  # runtime import: hr.cli is tracked

        conn = _cli.connect()
        try:
            console.print(builder(conn))
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        _fail(f"error: {exc}")


def status() -> None:
    """Show DB status: sweeps + latest-sweep capability means (DB-only)."""
    from .cli_report_verdict import build_status_report  # untracked at some checkouts (T8)

    _with_conn(build_status_report)


def apply(
    preset: Optional[str] = typer.Option(
        None, "--preset",
        help="preset name in fastdraw-presets.json (default: verdict-<today>)",
    ),
    set_state: bool = typer.Option(
        False, "--set-state",
        help="also write .fastdraw.json (boot-time state; opencode restart required)",
    ),
) -> None:
    """Bridge the latest verdict seating into a FastDraw preset (transactional).

    Reads the same per-seat ranking ``hr verdict --latest`` computes for the
    latest sweep (the seating is computed, never stored — there is no
    results table) and writes fastdraw-presets.json under the opencode
    config dir: {presets: {NAME: {description, createdAt, agents: {agent:
    provider/model}}}}. Seat keys are normalized to runtime agent names
    (underscores → hyphens). Every agents value must contain "/" (FastDraw's
    isModelMap rule). Refuses to write when no verdict seating exists —
    presets are never clobbered with empty data.

    Safety envelope (hr.plugin_safety): refuses on FastDraw schema-version
    mismatch and on preview drift (files changed since ``apply-preview``);
    snapshots the current files into a manifest+blob backup before writing;
    and AUTO-RESTORES that backup if the apply fails after any write.
    """
    from hr.plugin_safety import safe_apply

    def run(conn) -> str:
        result = safe_apply(preset_name=preset, include_state=set_state, conn=conn)
        if not result["success"]:
            raise RuntimeError(result["error"])
        backup = result.get("backup")
        msg = result["result"]
        if backup:
            msg = f"{msg}\nbackup -> {backup} (snapshot {result.get('snapshot_id', '?')})"
        return msg

    _with_conn(run)


def apply_preview(
    preset: Optional[str] = typer.Option(None, "--preset"),
    set_state: bool = typer.Option(False, "--set-state"),
) -> None:
    """Show the FastDraw changes a safe apply would make — and record them.

    The current file hashes are persisted as a preview record: the next
    ``hr apply`` REFUSES if the files drifted in the meantime (what was
    previewed must be what is applied).
    """
    from hr.plugin_safety import preview_apply

    def run(conn) -> str:
        result = preview_apply(preset, set_state, conn=conn, record_preview=True)
        if result.get("success") is False:
            raise RuntimeError(result["error"])
        return json.dumps(result, default=str)

    _with_conn(run)


def apply_rollback(backup: str = typer.Argument(...)) -> None:
    """Restore FastDraw files from a named HR apply backup (manifest-driven).

    Pre-existing files are restored byte-for-byte; files that did not exist
    before the apply are DELETED. Corrupt backups (missing/tampered manifest,
    missing blob, hash mismatch) are refused without any write.
    """
    from hr.plugin_safety import rollback

    result = rollback(backup)
    if not result["success"]:
        _fail(f"error: {result['error']}")
    console.print(result["message"])


def apply_backups() -> None:
    """List backups available for FastDraw rollback."""
    from hr.plugin_safety import list_backups

    console.print_json(json.dumps(list_backups()))


def apply_prune() -> None:
    """Prune old HR apply backups (retention: 10 backups / 30 days).

    Always preserves the newest VALID recovery point; corrupt snapshots are
    never kept. Defaults are revisable in hr.plugin_safety (MAX_BACKUPS /
    MAX_AGE_DAYS).
    """
    from hr.plugin_safety import prune_backups

    result = prune_backups()
    console.print(f"Pruned {len(result['removed'])} backup(s); kept {len(result['kept'])}.")
    for name in result["removed"]:
        console.print(f"  removed {name}")
    for name in result["kept"]:
        console.print(f"  kept {name}")


def register_apply_commands(tp: typer.Typer) -> None:
    """Attach the apply-family commands to a typer app."""
    tp.command(epilog=(
        "RESTART NOTE: --set-state effects require an opencode restart — FastDraw's "
        "config hook applies its state file (.fastdraw.json) only at opencode boot. "
        "Presets take effect at tool-call time (fastdraw_load_preset), no restart. "
        "FastDraw code is never imported or run — this is a plain JSON file contract "
        "on the opencode config dir (OPENCODE_CONFIG_DIR env or ~/.config/opencode)."
    ))(apply)
    tp.command(name="apply-preview")(apply_preview)
    tp.command(name="apply-rollback")(apply_rollback)
    tp.command(name="apply-backups")(apply_backups)
    tp.command(name="apply-prune")(apply_prune)
    tp.command()(status)


try:  # worktree wiring: hr/cli_app.py exists in the dirty tree (T8 will finalize it)
    from .cli_app import app as _worktree_app  # noqa: PLC0415
except ModuleNotFoundError:  # pragma: no cover — fresh HEAD checkout (cli_app untracked)
    _worktree_app = None

if _worktree_app is not None:
    register_apply_commands(_worktree_app)