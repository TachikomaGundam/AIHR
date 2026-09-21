"""Shared hermetic fixtures for the turnkey-bundle tests (no docker, no PG).

Stages a fake owned dir ``D`` (empty marker files under ``pg/bin`` so real
discovery logic finds the bundle) and a recording ``PgRunner`` stub — every
subprocess is captured, never spawned.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hr.pg_launcher import ProcessResult

PG_TOOLS = ("initdb", "pg_ctl", "psql", "createdb", "postgres")


def stage_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Create ``D`` with app/pg/share staged + AIHR_HOME_DIR redirected."""
    d = tmp_path / "aihr-home"
    (d / "app").mkdir(parents=True)
    (d / "share" / "aihr" / "configs").mkdir(parents=True)
    bin_dir = d / "pg" / "bin"
    bin_dir.mkdir(parents=True)
    for tool in PG_TOOLS:
        (bin_dir / tool).write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("AIHR_HOME_DIR", str(d))
    monkeypatch.delenv("AIHR_PG_HOME", raising=False)
    return d


class StubPgRunner:
    """Recording injectable PgRunner; emulates a successful initdb cluster."""

    def __init__(self, initdb_rc: int = 0) -> None:
        self.calls: list[tuple[list[str], int, dict[str, str]]] = []
        self.hook: object | None = None  # callable(argv, env) — inspection at spawn time
        self.initdb_rc = initdb_rc

    def __call__(self, argv: list[str], timeout: int, env: dict[str, str]) -> ProcessResult:
        self.calls.append((list(argv), timeout, dict(env)))
        if self.hook is not None:
            self.hook(argv, env)  # type: ignore[operator]
        if "initdb" in " ".join(argv):
            if self.initdb_rc != 0:
                return ProcessResult(self.initdb_rc, "", "initdb: failed")
            data_dir = Path(argv[argv.index("-D") + 1])
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "PG_VERSION").write_text("18\n", encoding="utf-8")
            (data_dir / "postgresql.conf").write_text(
                "# original initdb conf\n#port = 5432\n", encoding="utf-8"
            )
            (data_dir / "pg_hba.conf").write_text("# original initdb hba\n", encoding="utf-8")
        return ProcessResult(0, "", "")

    @property
    def argvs(self) -> list[list[str]]:
        return [argv for argv, _timeout, _env in self.calls]

    def find(self, needle: str) -> list[list[str]]:
        return [argv for argv in self.argvs if needle in " ".join(argv)]
