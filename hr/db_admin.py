"""AIHR turnkey database lifecycle: the logic behind hr db-up/db-down/db-status.

The backend decision tree. At ``db_up`` entry:

1. **compose** — docker on PATH and (an ``aihr-db`` container already exists
   or the effective port is free): the existing docker-compose path
   (:mod:`hr.db_compose`), argv byte-identical, foreign-takeover and port
   guards included. This lane never changes for existing installs.
2. **embedded** — otherwise, if the turnkey bundle's vendored Postgres is
   present (:func:`hr.pg_launcher.pg_home`): :mod:`hr.db_embedded` drives
   db.env → initdb → pg_ctl → readiness → schema init.
3. **neither** — the docker-missing error text plus the turnkey hint line.

Pure functions with injectable subprocess seams (``Runner`` for docker,
``PgRunner`` for the vendored binaries — argv lists only, never a shell), so
every branch is testable hermetically. The schema itself is owned solely by
:func:`hr.db.init_schema` (``hr/db_schema.py`` is the single source of
truth); this module only brings a server up, waits for readiness, and hands
off to it.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from typing import Optional

from hr import config, db, db_embedded, db_compose, pg_launcher
from hr.config_resources import DB_DEFAULT_PORT, aihr_home_dir, db_env_file
from hr.db_compose import (
    CONTAINER as CONTAINER,
)
from hr.db_compose import (
    READY_POLL_INTERVAL_S as READY_POLL_INTERVAL_S,
    READY_TIMEOUT_S as READY_TIMEOUT_S,
    Runner as Runner,
)
from hr.db_compose import CommandResult as CommandResult
from hr.db_compose import _tail as _tail
from hr.db_compose import (
    compose_argv as compose_argv,
    compose_file_path as compose_file_path,
    container_status as container_status,
    default_runner as default_runner,
    docker_compose_dir as docker_compose_dir,
    ensure_db_env_file as ensure_db_env_file,
    foreign_compose_conflict as foreign_compose_conflict,
    generate_db_password as generate_db_password,
    is_port_bound as is_port_bound,
    mask_dsn as mask_dsn,
    read_db_env_file as read_db_env_file,
    render_db_env as render_db_env,
    wait_for_ready as wait_for_ready,
    write_db_env_file as write_db_env_file,
)


def _effective_port(port: Optional[int]) -> int:
    """Port the decision tree probes, read-only (never creates an env file)."""
    if port is not None:
        return port
    env_file = db_env_file()
    raw = db_compose.read_db_env_file(env_file).get("AIHR_DB_PORT", "") if env_file else ""
    return int(raw) if raw.isdigit() else DB_DEFAULT_PORT


def db_up(
    port: Optional[int] = None,
    *,
    force: bool = False,
    run: Runner = default_runner,
    pg_run: pg_launcher.PgRunner = pg_launcher.default_pg_runner,
    sleep: Callable[[float], None] = time.sleep,
    connect: Callable[[int], bool] = pg_launcher.tcp_reachable,
    say: Callable[[str], None] = print,
) -> int:
    """Bring up the AIHR postgres server (compose or embedded) and init schema.

    Idempotent on both backends: an already-running stack re-runs only the
    (idempotent) schema init and never rewrites an existing password. Refuses
    a pre-existing foreign-managed ``aihr-db`` container unless ``force`` is
    set (compose lane; see :func:`hr.db_compose.foreign_compose_conflict`).
    """
    docker_present = shutil.which("docker") is not None
    compose_ready = docker_present and compose_file_path().is_file()
    if compose_ready and (container_status(run) or not is_port_bound(_effective_port(port))):
        return _db_up_compose(port, force=force, run=run, sleep=sleep, say=say)
    embedded_root = pg_launcher.pg_home()
    if embedded_root is not None:
        return db_embedded.up(
            aihr_home_dir(), port, root=embedded_root, pg_run=pg_run,
            sleep=sleep, connect=connect, say=say,
        )
    if compose_ready:  # port blocked by a foreign listener: the port guard below reports it
        return _db_up_compose(port, force=force, run=run, sleep=sleep, say=say)
    if not docker_present:
        say("error: docker not found on PATH — install Docker Engine, then re-run: hr db-up")
    else:
        say(f"error: compose manifest missing: {compose_file_path()} (broken aihr install?)")
    say("or install the turnkey bundle (see README) / BYO-Postgres via HR_DSN")
    return 1


def _db_up_compose(
    port: Optional[int],
    *,
    force: bool,
    run: Runner,
    sleep: Callable[[float], None],
    say: Callable[[str], None],
) -> int:
    """The docker-compose lane: unchanged behavior, argv byte-identical to the
    pre-bundle ``hr db-up`` (foreign guard + --force + port guard included)."""
    env_file, effective_port, created = ensure_db_env_file(port)
    if created:
        say(f"db env file: wrote {env_file} (random password, mode 0600)")
    if not force:
        refusal = foreign_compose_conflict(run)
        if refusal is not None:
            say(refusal)
            return 1
    if is_port_bound(effective_port) and container_status(run) != "running":
        say(
            f"error: 127.0.0.1:{effective_port} is already bound and {CONTAINER} "
            f"is not running — this is not the AIHR database. "
            "Pick another port: hr db-up --port <N>"
        )
        return 1
    res = run(db_compose.compose_argv(env_file, ["up", "-d"]), db_compose._UP_TIMEOUT_S)
    if res.rc != 0:
        say(f"error: docker compose up failed (rc={res.rc}): {_tail(res.stderr or res.stdout)}")
        return res.rc
    say(f"waiting for {CONTAINER} readiness (pg_isready, up to {READY_TIMEOUT_S:.0f}s)...")
    if not wait_for_ready(
        run, timeout_s=READY_TIMEOUT_S, interval_s=READY_POLL_INTERVAL_S, sleep=sleep
    ):
        say(f"error: {CONTAINER} not ready after {READY_TIMEOUT_S:.0f}s — check: docker logs {CONTAINER}")
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
    say(f"container: {container_status(run) or 'unknown'} ({CONTAINER})")
    say(f"port: 127.0.0.1:{effective_port}")
    say(f"schema: hr — {tables} tables, {rows} rows")
    say("AIHR database is up.")
    return 0


def db_down(
    purge: bool = False,
    yes: bool = False,
    *,
    run: Runner = default_runner,
    pg_run: pg_launcher.PgRunner = pg_launcher.default_pg_runner,
    say: Callable[[str], None] = print,
) -> int:
    """Stop the stack; ``--purge --yes`` additionally removes the data.

    Backend selection mirrors :func:`db_up`: our ``aihr-db`` container wins
    when it exists; otherwise an installed embedded cluster stops with
    ``pg_ctl -m fast`` (and ``--purge --yes`` deletes ``D/data``).
    """
    embedded_root = pg_launcher.pg_home()
    d = aihr_home_dir()
    if embedded_root is not None and db_embedded.installed(d, root=embedded_root):
        if shutil.which("docker") is None or not container_status(run):
            return db_embedded.down(
                d, root=embedded_root, purge=purge, yes=yes, pg_run=pg_run, say=say
            )
    if shutil.which("docker") is None:
        say("error: docker not found on PATH — nothing to stop from here.")
        return 1
    if purge and not yes:
        say("error: --purge destroys the aihr-db-data volume; add --yes to confirm the data loss.")
        return 1
    args = ["down", "-v"] if purge else ["stop"]
    res = run(compose_argv(db_env_file(), args), db_compose._UP_TIMEOUT_S)
    if res.rc != 0:
        say(f"error: docker compose {' '.join(args)} failed (rc={res.rc}): {_tail(res.stderr or res.stdout)}")
        return res.rc
    say(f"{CONTAINER}: {'stopped' if not purge else 'removed (data volume purged)'}")
    return 0


def db_status(
    *,
    run: Runner = default_runner,
    say: Callable[[str], None] = print,
) -> int:
    """Backend line + DSN resolution step (masked) + hr-schema stats."""
    rc = 0
    if shutil.which("docker") is not None and compose_file_path().is_file():
        res = run(compose_argv(db_env_file(), ["ps", "-a"]), db_compose._DOCKER_TIMEOUT_S)
        for line in (res.stdout or res.stderr).splitlines():
            if line.strip():
                say(f"compose: {line}")
        rc = rc or res.rc
        say(f"backend: compose ({CONTAINER}: {container_status(run) or 'absent'})")
    else:
        say("compose: docker or the AIHR manifest not found — container state unknown")
        embedded_root = pg_launcher.pg_home()
        d = aihr_home_dir()
        if embedded_root is not None and db_embedded.installed(d, root=embedded_root):
            db_embedded.status_lines(d, root=embedded_root, say=say)
        else:
            say("backend: none (no compose container, no embedded bundle)")
    try:
        dsn, source = config.db_dsn_with_source()
    except RuntimeError as exc:
        say(f"dsn: {exc}")
        return 1
    say(f"dsn: {mask_dsn(dsn)}")
    say(f"resolved-by: {source}")
    probe_ok = False
    try:
        conn = db.connect()
    except Exception as exc:  # noqa: BLE001 — CLI boundary: report, never traceback
        say(f"sql-probe: FAILED ({exc})")
        return 1
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            probe_ok = cur.fetchone() == (1,)
        tables, rows = db.schema_stats(conn)
        say("sql-probe: ok" if probe_ok else "sql-probe: FAILED")
        say(f"hr-schema: {tables} tables, {rows} total rows")
    except Exception as exc:  # noqa: BLE001 — CLI boundary: report, never traceback
        say(f"sql-probe: FAILED ({exc})")
        probe_ok = False
    finally:
        conn.close()
    return rc or (0 if probe_ok else 1)
