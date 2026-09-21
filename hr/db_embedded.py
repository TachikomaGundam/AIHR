"""Embedded-backend orchestration: the turnkey flow behind ``hr db-up`` (2).

Sequences the vendored-binary pieces from :mod:`hr.pg_env` (credentials,
cluster bootstrap) and :mod:`hr.pg_launcher` (start/stop/ready), hands the
running server to :func:`hr.db.init_schema`, and prints the same summary
shape as the compose path with ``backend: embedded``. Every subprocess goes
through the injectable ``PgRunner`` seam; every filesystem effect lands in
the owned dir ``D``.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Optional

from hr import db, pg_env, pg_launcher
from hr.db_compose import _tail, is_port_bound


def installed(d: Path, *, root: Path) -> bool:
    """True when the bundle has embedded state: a cluster or a live server."""
    return pg_env.cluster_exists(d) or pg_launcher.embedded_pid(d) is not None


def up(
    d: Path,
    port: Optional[int],
    *,
    root: Path,
    pg_run: pg_launcher.PgRunner = pg_launcher.default_pg_runner,
    sleep: Callable[[float], None] = time.sleep,
    connect: Callable[[int], bool] = pg_launcher.tcp_reachable,
    say: Callable[[str], None] = print,
) -> int:
    """Bring up the embedded server and initialize the schema (idempotent).

    Never rewrites a persisted password; an already-running cluster (live
    ``postmaster.pid``) skips bootstrap and start, re-running only the
    idempotent schema init — the embedded mirror of the compose re-up.
    """
    env_file, effective, created = pg_env.ensure_env(d, port)
    if created:
        say(f"db env file: wrote {env_file} (random password, mode 0600)")
    running = pg_launcher.embedded_pid(d) is not None
    if not running and is_port_bound(effective):
        say(
            f"error: 127.0.0.1:{effective} is already bound and the embedded "
            f"{pg_launcher.PG_DATABASE} server is not running — this is not the "
            "AIHR database. Pick another port: hr db-up --port <N>"
        )
        return 1
    if not running:
        try:
            pg_env.ensure_pgdata(d, effective, root=root, pg_run=pg_run)
        except (RuntimeError, OSError) as exc:  # noqa: BLE001 — CLI boundary: report, never traceback
            say(f"error: embedded cluster bootstrap failed: {exc}")
            return 1
        res = pg_launcher.start(d, effective, root=root, pg_run=pg_run)
        if res.rc != 0:
            say(f"error: pg_ctl start failed (rc={res.rc}): {_tail(res.stderr or res.stdout)}")
            return res.rc
        say(
            f"waiting for embedded postgres readiness (psql SELECT 1, "
            f"up to {pg_launcher.READY_TIMEOUT_S:.0f}s)..."
        )
        if not pg_launcher.wait_until_ready(
            d, effective, root=root, pg_run=pg_run, connect=connect, sleep=sleep
        ):
            say(
                f"error: embedded postgres not ready after "
                f"{pg_launcher.READY_TIMEOUT_S:.0f}s — check: {pg_launcher.pg_log_file(d)}"
            )
            return 1
        db_res = pg_launcher.ensure_database(d, effective, root=root, pg_run=pg_run)
        if db_res.rc != 0:
            say(
                f"error: could not create the {pg_launcher.PG_DATABASE} database "
                f"(rc={db_res.rc}): {_tail(db_res.stderr or db_res.stdout)}"
            )
            return 1
    try:
        db.init_schema()
        conn = db.connect()
        try:
            tables, rows = db.schema_stats(conn)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — CLI boundary: report, never traceback
        say(f"error: schema initialization failed: {exc}")
        return 1
    say("backend: embedded")
    say(f"port: 127.0.0.1:{effective}")
    say(f"schema: hr — {tables} tables, {rows} rows")
    say("AIHR database is up.")
    return 0


def down(
    d: Path,
    *,
    root: Path,
    purge: bool = False,
    yes: bool = False,
    pg_run: pg_launcher.PgRunner = pg_launcher.default_pg_runner,
    say: Callable[[str], None] = print,
) -> int:
    """``pg_ctl stop -m fast``; ``--purge --yes`` additionally removes ``D/data``."""
    if purge and not yes:
        say(
            f"error: --purge destroys the embedded data dir {d / 'data'}; "
            "add --yes to confirm the data loss."
        )
        return 1
    if pg_launcher.embedded_pid(d) is not None or pg_env.cluster_exists(d):
        res = pg_launcher.stop(d, root=root, pg_run=pg_run)
        if res.rc != 0:
            say(f"error: pg_ctl stop failed (rc={res.rc}): {_tail(res.stderr or res.stdout)}")
            return res.rc
    if purge:
        shutil.rmtree(d / "data", ignore_errors=True)
    say(
        "embedded postgres: "
        f"{'stopped' if not purge else 'stopped (data dir purged)'}"
    )
    return 0


def status_lines(
    d: Path,
    *,
    root: Path,
    say: Callable[[str], None] = print,
) -> None:
    """One ``backend:`` line describing the embedded server state."""
    pid = pg_launcher.embedded_pid(d)
    state = f"running, pid {pid}" if pid is not None else "stopped"
    say(f"backend: embedded ({state}, port {pg_launcher.embedded_port(d)})")
