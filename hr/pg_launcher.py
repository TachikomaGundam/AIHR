"""Embedded Postgres launcher: the vendored-binary half of the turnkey database.

Pure functions plus an injectable subprocess ``PgRunner`` (argv lists only,
never a shell), mirroring :mod:`hr.db_admin`. Every spawn of a vendored
binary carries the bundle's private library dir on ``LD_LIBRARY_PATH``
(POSIX): the shipped postgres builds resolve their shared libraries there.
Windows relies on sibling-DLL default loading (``D/pg/bin`` next to ``D/pg/lib``
is searched by the loader; documented no-op, no PATH surgery).

Bundle layout (``D`` = :func:`hr.config_resources.aihr_home_dir`):

    D/pg/bin/{initdb,pg_ctl,psql,postgres}   vendored server binaries
    D/pg/lib                                 their shared libraries
    D/data/pgdata                            cluster data dir (initdb owns it)
    D/log/pg.log                             server log (``pg_ctl -l``)
    D/db.env                                 AIHR_DB_PASSWORD / AIHR_DB_PORT
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hr.config_resources import (
    DB_DEFAULT_PORT,
    aihr_home_dir,
    bundled_db_env_file,
    read_db_env_file,
)

PG_USER = "aihr"
PG_DATABASE = "aihr"
PG_MAINTENANCE_DB = "postgres"
PG_HOST = "127.0.0.1"
PG_START_WAIT_S = 60
READY_TIMEOUT_S = 60.0
READY_POLL_INTERVAL_S = 2.0
_PGCTL_TIMEOUT_S = 90
_PSQL_TIMEOUT_S = 15


@dataclass(frozen=True)
class ProcessResult:
    rc: int
    stdout: str
    stderr: str


PgRunner = Callable[[list[str], int, Mapping[str, str]], ProcessResult]


def default_pg_runner(argv: list[str], timeout: int, env: Mapping[str, str]) -> ProcessResult:
    proc = subprocess.run(  # noqa: S603 — argv list, no shell, argv/env fully controlled above
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=dict(env),
        check=False,
    )
    return ProcessResult(proc.returncode, proc.stdout, proc.stderr)


# ---------------------------------------------------------------------------
# discovery: is the embedded backend available, and where do its pieces live
# ---------------------------------------------------------------------------


def bundled_mode() -> bool:
    """True when running from the turnkey bundle layout.

    A function (never an import-time constant) so both branches stay
    testable: PyInstaller sets ``sys.frozen``; a source checkout running
    from an env whose grandparent dir carries a vendored ``pg/`` also
    counts (development against a staged bundle).
    """
    if getattr(sys, "frozen", False):
        return True
    return (Path(sys.executable).resolve().parent.parent / "pg").exists()


# Windows bundles ship .exe-suffixed tools; the bare-name probe returned None
# on the 0.4.0 windows smoke (run 35832366570) and silently disabled the
# embedded lane. Read at call time so tests can monkeypatch the win shape.
TOOL_SUFFIX = ".exe" if os.name == "nt" else ""


def pg_home(d: Optional[Path] = None) -> Optional[Path]:
    """Vendored Postgres root: ``D/pg`` when complete, else ``$AIHR_PG_HOME``
    when that points at a usable tree, else ``None`` (embedded unavailable)."""
    root = (aihr_home_dir() if d is None else d) / "pg"
    if (root / "bin" / ("initdb" + TOOL_SUFFIX)).is_file():
        return root
    env = os.environ.get("AIHR_PG_HOME")
    if env:
        alt = Path(env).expanduser()
        if (alt / "bin" / ("initdb" + TOOL_SUFFIX)).is_file():
            return alt
    return None


def pg_bin_dir(root: Path, tool: str) -> str:
    return str(root / "bin" / (tool + TOOL_SUFFIX))


def pg_data_dir(d: Path) -> Path:
    return d / "data" / "pgdata"


def pg_log_file(d: Path) -> Path:
    return d / "log" / "pg.log"


def pid_file(d: Path) -> Path:
    return pg_data_dir(d) / "postmaster.pid"


def embedded_port(d: Optional[Path] = None) -> int:
    """Port from ``D/db.env`` (AIHR_DB_PORT), defaulting to DB_DEFAULT_PORT."""
    env_path = bundled_db_env_file() if d is None else d / "db.env"
    raw = read_db_env_file(env_path).get("AIHR_DB_PORT", "")
    return int(raw) if raw.isdigit() else DB_DEFAULT_PORT


def process_env(
    root: Optional[Path], extra: Optional[Mapping[str, str]] = None
) -> dict[str, str]:
    """Complete subprocess environment for one vendored-binary spawn."""
    env = dict(os.environ)
    if extra:
        env.update(extra)
    if root is not None and os.name == "posix":
        lib = str(root / "lib")
        prior = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{lib}{os.pathsep}{prior}" if prior else lib
    return env


# ---------------------------------------------------------------------------
# server lifecycle
# ---------------------------------------------------------------------------


def start(
    d: Path, port: int, *, root: Path, pg_run: PgRunner = default_pg_runner
) -> ProcessResult:
    """``pg_ctl start`` bound to loopback on ``port``; waits (``-w``) up to 60s.

    ``root`` is the gated :func:`pg_home` result — callers dispatch on the
    backend decision tree (see :func:`hr.db_admin.db_up`) and pass it here.
    """
    pg_log_file(d).parent.mkdir(parents=True, exist_ok=True)
    argv = [
        pg_bin_dir(root, "pg_ctl"),
        "-D", str(pg_data_dir(d)),
        "-l", str(pg_log_file(d)),
        "-w", "-t", str(PG_START_WAIT_S), "start",
        "-o", f"-p {port} -h {PG_HOST}",
    ]
    return pg_run(argv, _PGCTL_TIMEOUT_S, process_env(root))


def stop(d: Path, *, root: Path, pg_run: PgRunner = default_pg_runner) -> ProcessResult:
    """``pg_ctl stop -m fast`` — smart shutdown, never immediate."""
    argv = [
        pg_bin_dir(root, "pg_ctl"),
        "-D", str(pg_data_dir(d)),
        "-m", "fast", "stop",
    ]
    return pg_run(argv, _PGCTL_TIMEOUT_S, process_env(root))


def embedded_pid(d: Path) -> Optional[int]:
    """PID from ``postmaster.pid`` when that process is alive, else ``None``.

    A stale pid file (crash residue) reads as not-running; ``pg_ctl start``
    recovers from it itself, so no cleanup happens here.
    """
    try:
        raw = pid_file(d).read_text(encoding="utf-8").splitlines()[0]
        pid = int(raw.strip())
    except (OSError, ValueError, IndexError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


# ---------------------------------------------------------------------------
# readiness probing: TCP first, then one authenticated SQL round-trip
# ---------------------------------------------------------------------------


def tcp_reachable(port: int, host: str = PG_HOST, timeout_s: float = 2.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout_s)
        return probe.connect_ex((host, port)) == 0


def ready_probe(
    d: Path,
    port: int,
    *,
    root: Path,
    pg_run: PgRunner = default_pg_runner,
    connect: Callable[[int], bool] = tcp_reachable,
) -> bool:
    """TCP connect to 127.0.0.1:port, then ``psql -c "SELECT 1"`` on the
    maintenance DB (``initdb`` only guarantees postgres/template*; the
    target DB is created by :func:`ensure_database` once this probe passes)."""
    if not connect(port):
        return False
    password = read_db_env_file(d / "db.env").get("AIHR_DB_PASSWORD", "")
    dsn = f"postgresql://{PG_USER}@{PG_HOST}:{port}/{PG_MAINTENANCE_DB}?sslmode=disable"
    res = pg_run(
        [pg_bin_dir(root, "psql"), dsn, "-c", "SELECT 1"],
        _PSQL_TIMEOUT_S,
        process_env(root, {"PGPASSWORD": password}),
    )
    return res.rc == 0


def ensure_database(
    d: Path,
    port: int,
    *,
    root: Path,
    pg_run: PgRunner = default_pg_runner,
) -> ProcessResult:
    """Create the target database after cluster start.

    PG18 initdb only creates postgres/template0/template1 — the compose lane
    got its target DB from POSTGRES_DB in the image entrypoint, so the
    embedded lane must create it explicitly. An ``already exists`` verdict is
    success (idempotent re-up).
    """
    password = read_db_env_file(d / "db.env").get("AIHR_DB_PASSWORD", "")
    res = pg_run(
        [
            pg_bin_dir(root, "createdb"),
            "-h", PG_HOST, "-p", str(port), "-U", PG_USER,
            "-w", "--maintenance-db", PG_MAINTENANCE_DB, PG_DATABASE,
        ],
        _PSQL_TIMEOUT_S,
        process_env(root, {"PGPASSWORD": password}),
    )
    if res.rc != 0 and "already exists" not in (res.stdout + res.stderr):
        return res
    return ProcessResult(0, res.stdout, res.stderr)


def wait_until_ready(
    d: Path,
    port: int,
    *,
    root: Path,
    pg_run: PgRunner = default_pg_runner,
    connect: Callable[[int], bool] = tcp_reachable,
    sleep: Callable[[float], None] = time.sleep,
    timeout_s: Optional[float] = None,
    interval_s: Optional[float] = None,
) -> bool:
    """Poll :func:`ready_probe` until it passes or the budget runs out."""
    interval = READY_POLL_INTERVAL_S if interval_s is None else interval_s
    timeout = READY_TIMEOUT_S if timeout_s is None else timeout_s
    attempts = max(1, int(timeout / interval))
    for _attempt in range(attempts):
        if ready_probe(d, port, root=root, pg_run=pg_run, connect=connect):
            return True
        sleep(interval)
    return False
