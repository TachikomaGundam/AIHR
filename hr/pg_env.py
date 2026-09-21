"""Embedded-Postgres provisioning: credentials, cluster bootstrap, conf files.

Split out of :mod:`hr.pg_launcher` for the 250-LOC module ceiling: this half
writes the on-disk state (``D/db.env``, ``D/data/pgdata`` with its
``postgresql.conf`` / ``pg_hba.conf``); the launcher half drives the running
server (start/stop/ready). ``initdb`` receives the password ONLY through a
zeroed-then-deleted ``--pwfile`` temp file — never argv, never a log line.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

from hr.config_resources import DB_DEFAULT_PORT, read_db_env_file
from hr.pg_launcher import (
    PG_HOST,
    PG_USER,
    PgRunner,
    default_pg_runner,
    pg_bin_dir,
    pg_data_dir,
    process_env,
)

CONF_MARKER = "# --- AIHR embedded Postgres settings (written by hr db-up) ---"

#: Deterministic client authentication: loopback only, scram-sha-256 only.
PG_HBA_TEXT = f"""# AIHR embedded Postgres client authentication — managed by hr db-up.
# TYPE  DATABASE  USER  ADDRESS             METHOD
local   all       all                       scram-sha-256
host    all       all       {PG_HOST}/32    scram-sha-256
"""


def db_env_path(d: Path) -> Path:
    return d / "db.env"


def ensure_env(d: Path, port: Optional[int] = None) -> tuple[Path, int, bool]:
    """Resolve ``D/db.env`` (generate when missing); (path, port, wrote).

    Mirrors :func:`hr.db_admin.ensure_db_env_file` semantics on the bundle's
    own file: an existing password is never regenerated; an explicit ``port``
    rewrites only the port line. Password generation reuses the same
    :func:`hr.db_admin.generate_db_password` the compose path uses.
    """
    from hr import db_admin  # noqa: PLC0415 — lazy: db_admin imports pg_launcher at top level

    path = db_env_path(d)
    values = read_db_env_file(path)
    password = values.get("AIHR_DB_PASSWORD", "")
    file_port_raw = values.get("AIHR_DB_PORT", "")
    file_port = int(file_port_raw) if file_port_raw.isdigit() else None
    effective = port if port is not None else (file_port or DB_DEFAULT_PORT)
    if password and (port is None or port == file_port):
        return path, effective, False
    db_admin.write_db_env_file(path, password or db_admin.generate_db_password(), effective)
    return path, effective, True


def initdb_argv(root: Path, d: Path, pwfile: Path) -> list[str]:
    """Pinned ``initdb`` argv: scram auth, aihr superuser, UTF8, pwfile only."""
    return [
        pg_bin_dir(root, "initdb"),
        "-D", str(pg_data_dir(d)),
        "-U", PG_USER,
        "--auth=scram-sha-256",
        f"--pwfile={pwfile}",
        "--encoding=UTF8",
    ]


def conf_block(port: int) -> str:
    """The ``postgresql.conf`` override block (later settings win in PG)."""
    return f"{CONF_MARKER}\nlisten_addresses = '{PG_HOST}'\nport = {port}\n"


def apply_conf(data_dir: Path, port: int) -> None:
    """Pin loopback + port in ``postgresql.conf``; rewrite ``pg_hba.conf``.

    The conf append is idempotent (marker-guarded); the HBA is the
    deterministic :data:`PG_HBA_TEXT`, replaced wholesale on every bootstrap
    so the shipped authentication policy never drifts from the tested text.
    """
    conf = data_dir / "postgresql.conf"
    text = conf.read_text(encoding="utf-8") if conf.is_file() else ""
    if CONF_MARKER not in text:
        prefix = "" if text.endswith("\n") or not text else "\n"
        conf.write_text(text + prefix + conf_block(port), encoding="utf-8")
    (data_dir / "pg_hba.conf").write_text(PG_HBA_TEXT, encoding="utf-8")


def _write_pwfile(path: Path, password: str) -> None:
    path.write_text(password + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # best-effort on platforms without POSIX modes


def _wipe(path: Path) -> None:
    """Overwrite the pwfile with zeros before unlinking (initdb never deletes it)."""
    try:
        size = path.stat().st_size
        with path.open("wb") as fh:
            fh.write(b"\0" * size)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass
    path.unlink(missing_ok=True)


def ensure_pgdata(
    d: Path,
    port: int,
    *,
    root: Path,
    pg_run: PgRunner = default_pg_runner,
) -> bool:
    """Create the cluster on first ``db-up``; True when ``initdb`` ran.

    An existing cluster (``PG_VERSION`` present) is left untouched — data
    lives across restarts, exactly like the compose volume does.
    """
    data = pg_data_dir(d)
    if (data / "PG_VERSION").is_file():
        return False
    password = read_db_env_file(db_env_path(d)).get("AIHR_DB_PASSWORD", "")
    if not password:
        raise RuntimeError("embedded db env file has no AIHR_DB_PASSWORD (run ensure_env first)")
    data.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix="aihr-pwfile-", dir=data.parent)
    pwfile = Path(raw)
    os.close(fd)
    try:
        _write_pwfile(pwfile, password)
        res = pg_run(initdb_argv(root, d, pwfile), 120, process_env(root))
        if res.rc != 0:
            raise RuntimeError(
                f"initdb failed (rc={res.rc}): {_tail(res.stderr or res.stdout)}"
            )
    finally:
        _wipe(pwfile)
    apply_conf(data, port)
    return True


def _tail(text: str, limit: int = 3) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return " | ".join(lines[-limit:]) if lines else "(no output)"


def cluster_exists(d: Path) -> bool:
    return (pg_data_dir(d) / "PG_VERSION").is_file()
