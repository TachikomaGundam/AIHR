"""``hr support-bundle`` — customer-side diagnostics artifact, key-redacted.

When a customer's box misbehaves we cannot walk over there; the machine
should be able to mail itself to us. Collects the facts that made every bug
in the 2026-10 incident chain diagnosable (receipt/version, dialect
contracts, incident rows with verbatim details, measurement excerpts, config
echo, log tails) into one tar.gz — with every credential-bearing value
redacted before it leaves memory. Court rows are included as TSV of the
diagnostic columns only; response_text is truncated (verbatim tails rarely
outrank size limits when a customer is on a metered channel).
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from .dialect_contract import CONTRACT_STATUS_SQL

_REDACT = re.compile(
    r"""(?i)([A-Za-z0-9_-]*?(?:api[_-]?key|passwd|password|secret|auth[_-]?token|token|authorization)"""
    r"""[A-Za-z0-9_-]*)(["']?\s*[:=]\s*["']?)(\S+)"""
)


def redact(text: str) -> str:
    return _REDACT.sub(r"\1\2***REDACTED***", text)


_SWEEP_COUNT_SQL = "SELECT count(*) FROM hr.sweep"
_RUN_SQL = (
    "SELECT run_id, sweep_id, model_id, battery_id, status, "
    "coalesce(failure_reason,'-'), coalesce(contract_id,'-'), started_at "
    "FROM hr.run ORDER BY started_at DESC LIMIT 60"
)
_INCIDENT_SQL = (
    "SELECT incident_id, kind, details_json, recorded_at "
    "FROM hr.infra_incident ORDER BY recorded_at DESC LIMIT 40"
)
_MEAS_SQL = (
    "SELECT m.run_id, m.item_id, m.repetition, m.score, m.tokens_in, m.tokens_out, "
    "m.latency_ms, coalesce(m.flags,'-'), left(coalesce(m.response_text,''),300) "
    "FROM hr.measurement m ORDER BY m.created_at DESC LIMIT 40"
)

_TEXT_FILES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("db.env", (".aihr", "db.env")),
    ("log-pg.tail.txt", (".aihr", "log", "pg.log")),
    ("receipt.json", (".aihr", "receipt.json")),
)


def _tsv(rows: list[tuple[object, ...]] | None) -> str:
    return "\n".join("\t".join(str(c) for c in row) for row in rows or [])


def _tail(text: str, n: int) -> str:
    return "\n".join(text.splitlines()[-n:])


def _read_redacted(path: Path) -> str | None:
    try:
        return redact(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def collect(conn, *, home: Path, out_dir: Path, version: str) -> tuple[Path, str]:
    """Write the bundle; returns (path, sha256). conn is any DBAPI connection."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload: dict[str, str] = {
        "hr-version.txt": f"aihr {version}\ngenerated {stamp} (UTC)\n",
        "tables-sweeps.tsv": "sweep_count\n" + _tsv(_query(conn, _SWEEP_COUNT_SQL)),
        "tables-runs.tsv": _tsv(_query(conn, _RUN_SQL)),
        "tables-incidents.tsv": _tsv(_query(conn, _INCIDENT_SQL)),
        "tables-contracts.tsv": _tsv(_query(conn, CONTRACT_STATUS_SQL)),
        "tables-measurements.tsv": _tsv(_query(conn, _MEAS_SQL)),
    }
    for name, parts in _TEXT_FILES:
        body = _read_redacted(home.joinpath(*parts))
        if body is not None:
            payload[name] = _tail(body, 400) if name.endswith("tail.txt") else body
    if out_dir is not None:
        bundle = Path(out_dir) / f"aihr-support-{stamp}.tar.gz"
    else:  # pragma: no cover — defensive, callers pass explicit dirs
        bundle = Path(f"aihr-support-{stamp}.tar.gz")
    bundle.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(bundle, "w:gz") as tar:
        for name, body in payload.items():
            data = body.encode("utf-8")
            info = tarfile.TarInfo(f"support/{name}")
            info.size = len(data)
            info.mtime = int(datetime.now(timezone.utc).timestamp())
            tar.addfile(info, io.BytesIO(data))
    return bundle, hashlib.sha256(bundle.read_bytes()).hexdigest()


def _query(conn, sql: str) -> list[tuple[object, ...]]:
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return [tuple(redact(str(c)) for c in row) for row in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001 — one dead table must not kill the bundle
        return [("QUERY-ERROR", str(exc)[:200])]



from .cli_app import _with_conn  # noqa: E402


def support_bundle(
    out: Path = Path("."),
) -> None:
    """Write a redacted diagnostics tar.gz for shipping to the maintainers."""
    from . import __version__  # noqa: PLC0415

    home = Path(os.environ.get("HOME") or Path.home())

    def build(conn: object) -> str:
        path, digest = collect(conn, home=home, out_dir=out, version=__version__)
        return f"support bundle: {path}\nsha256: {digest}\nattach this file when reporting an issue"

    _with_conn(build)


try:  # worktree wiring (mirrors hr.cli_lifecycle): attach to the shipped typer app
    from .cli_app import app as _worktree_app  # noqa: PLC0415
except ModuleNotFoundError:  # pragma: no cover — fresh HEAD checkout
    _worktree_app = None

if _worktree_app is not None:
    _worktree_app.command(name="support-bundle")(support_bundle)

__all__ = ["collect", "redact", "support_bundle"]
