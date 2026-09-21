"""Backend decision-tree tests for hr.db_admin (compose lane frozen, embedded lane live).

All hermetic: docker is a monkeypatched ``shutil.which`` plus a scripted
Runner, the embedded bundle is a staged fake ``D`` with a recording
StubPgRunner, and schema init runs against a fake connection.
"""

from __future__ import annotations

import os


from hr import config_resources, db_admin, db_embedded
from hr.db_admin import db_down, db_status, db_up
from hr.db_compose import CommandResult

from tests._bundle_helpers import StubPgRunner, stage_bundle


class _StatsCursor:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def execute(self, stmt, params=None) -> None:  # noqa: ARG002
        self.executed.append(str(stmt))

    def fetchall(self):
        return [("model",), ("seat",)]

    def fetchone(self):
        return (3,)


class _StatsConn:
    def __init__(self) -> None:
        self.cursor_obj = _StatsCursor()
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self) -> None:
        self.closed = True


_NO_RUN = lambda argv, timeout: CommandResult(1, "", "")  # noqa: E731 — docker lane must stay dark


def _fake_schema(monkeypatch, conn: _StatsConn) -> list[int]:
    inits: list[int] = []
    monkeypatch.setattr(db_embedded.db, "init_schema", lambda: inits.append(1))
    monkeypatch.setattr(db_embedded.db, "connect", lambda *a, **k: conn)
    return inits


def _compose_only(monkeypatch, tmp_path) -> None:
    """docker + compose manifest present; no embedded bundle anywhere."""
    docker = tmp_path / "docker"
    docker.mkdir()
    (docker / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    monkeypatch.delenv("AIHR_HOME_DIR", raising=False)
    monkeypatch.delenv("AIHR_PG_HOME", raising=False)
    monkeypatch.setattr(db_admin.shutil, "which", lambda name: f"/usr/bin/{name}")


def _embedded_only(monkeypatch, tmp_path):
    monkeypatch.setattr(db_admin.shutil, "which", lambda _name: None)
    monkeypatch.setattr(db_embedded, "is_port_bound", lambda *_a, **_k: False)
    return stage_bundle(monkeypatch, tmp_path)


def _runner(state: str | None = "running"):
    calls: list[list[str]] = []

    def run(argv: list[str], timeout: int) -> CommandResult:  # noqa: ARG001
        calls.append(argv)
        joined = " ".join(argv)
        if "inspect" in joined:
            if state is None:
                return CommandResult(1, "", "No such object")
            return CommandResult(0, state + "\n", "")
        if "pg_isready" in joined:
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")

    return run, calls


# ---------------------------------------------------------------------------
# (1) docker present → compose lane, argv frozen, pg_run never touched
# ---------------------------------------------------------------------------


def test_compose_lane_wins_when_container_present(monkeypatch, tmp_path) -> None:
    _compose_only(monkeypatch, tmp_path)
    stage_bundle(monkeypatch, tmp_path)  # embedded also staged — compose must still win
    _fake_schema(monkeypatch, _StatsConn())
    run, calls = _runner()
    pg_stub = StubPgRunner()
    lines: list[str] = []
    assert db_up(run=run, pg_run=pg_stub, sleep=lambda _s: None, say=lines.append) == 0
    assert pg_stub.calls == []  # the embedded binaries were never spawned
    assert any(argv[-2:] == ["up", "-d"] for argv in calls)
    assert not any("backend: embedded" in line for line in lines)


def test_compose_lane_argv_unchanged_with_container_absent(monkeypatch, tmp_path) -> None:
    _compose_only(monkeypatch, tmp_path)
    stage_bundle(monkeypatch, tmp_path)
    _fake_schema(monkeypatch, _StatsConn())
    monkeypatch.setattr(db_admin, "is_port_bound", lambda *_a, **_k: False)
    run, calls = _runner(state=None)
    lines: list[str] = []
    assert db_up(run=run, pg_run=StubPgRunner(), sleep=lambda _s: None, say=lines.append) == 0
    up = next(argv for argv in calls if argv[-2:] == ["up", "-d"])
    assert up[:2] == ["docker", "compose"] and up[2:4] == ["-p", "aihr"]
    assert any("pg_isready" in argv for argv in calls)


# ---------------------------------------------------------------------------
# (2) no docker + bundle staged → embedded lane, full sequence
# ---------------------------------------------------------------------------


def test_embedded_lane_sequence(monkeypatch, tmp_path) -> None:
    d = _embedded_only(monkeypatch, tmp_path)
    conn = _StatsConn()
    inits = _fake_schema(monkeypatch, conn)
    stub = StubPgRunner()
    lines: list[str] = []
    rc = db_up(
        run=_NO_RUN, pg_run=stub, sleep=lambda _s: None,
        connect=lambda _p: True, say=lines.append,
    )
    assert rc == 0 and inits == [1] and conn.closed
    joined = [" ".join(argv) for argv in stub.argvs]
    assert any("/bin/initdb" in line for line in joined)
    assert any("pg_ctl" in line and " start" in line for line in joined)
    assert any("/bin/psql" in line and "SELECT 1" in line for line in joined)
    out = "\n".join(lines)
    assert "backend: embedded" in out
    assert "port: 127.0.0.1:5433" in out
    assert "schema: hr — 2 tables, 6 rows" in out
    assert "AIHR database is up." in out
    password = config_resources.read_db_env_file(d / "db.env")["AIHR_DB_PASSWORD"]
    assert password and password not in out


def test_embedded_lane_refuses_foreign_port_holder(monkeypatch, tmp_path) -> None:
    _embedded_only(monkeypatch, tmp_path)
    monkeypatch.setattr(db_embedded, "is_port_bound", lambda *_a, **_k: True)
    stub = StubPgRunner()
    lines: list[str] = []
    rc = db_up(run=_NO_RUN, pg_run=stub, sleep=lambda _s: None,
               connect=lambda _p: True, say=lines.append)
    assert rc == 1
    assert any("is not the AIHR database" in line and "--port" in line for line in lines)
    assert stub.calls == []  # never spawned anything for a port we do not own


def test_embedded_lane_idempotent_when_already_running(monkeypatch, tmp_path) -> None:
    d = _embedded_only(monkeypatch, tmp_path)
    conn = _StatsConn()
    inits = _fake_schema(monkeypatch, conn)
    pgdata = d / "data" / "pgdata"
    pgdata.mkdir(parents=True)
    (pgdata / "postmaster.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (pgdata / "PG_VERSION").write_text("18\n", encoding="utf-8")
    stub = StubPgRunner()
    lines: list[str] = []
    rc = db_up(run=_NO_RUN, pg_run=stub, sleep=lambda _s: None,
               connect=lambda _p: True, say=lines.append)
    assert rc == 0 and inits == [1]
    assert stub.calls == []  # no initdb, no pg_ctl start — schema init only


# ---------------------------------------------------------------------------
# (3) neither backend → current error text + the turnkey hint line
# ---------------------------------------------------------------------------


def test_neither_backend_keeps_text_and_appends_hint(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(db_admin.shutil, "which", lambda _name: None)
    monkeypatch.setenv("AIHR_HOME_DIR", str(tmp_path / "empty"))
    monkeypatch.delenv("AIHR_PG_HOME", raising=False)
    lines: list[str] = []
    assert db_up(run=_NO_RUN, say=lines.append) == 1
    assert lines[0] == (
        "error: docker not found on PATH — install Docker Engine, then re-run: hr db-up"
    )
    assert lines[-1] == "or install the turnkey bundle (see README) / BYO-Postgres via HR_DSN"


# ---------------------------------------------------------------------------
# db-down / db-status on the embedded lane
# ---------------------------------------------------------------------------


def _fake_cluster(d) -> None:
    pgdata = d / "data" / "pgdata"
    pgdata.mkdir(parents=True)
    (pgdata / "PG_VERSION").write_text("18\n", encoding="utf-8")


def test_db_down_embedded_stops_and_purges_only_with_yes(monkeypatch, tmp_path) -> None:
    d = _embedded_only(monkeypatch, tmp_path)
    _fake_cluster(d)
    stub = StubPgRunner()
    lines: list[str] = []
    assert db_down(purge=True, run=_NO_RUN, pg_run=stub, say=lines.append) == 1
    assert any("--yes" in line for line in lines)
    assert stub.calls == [] and (d / "data").is_dir()
    assert db_down(purge=True, yes=True, run=_NO_RUN, pg_run=stub, say=lines.append) == 0
    assert stub.find("pg_ctl")[0][-2:] == ["fast", "stop"]
    assert not (d / "data").exists()


def test_db_status_prints_embedded_backend_line(monkeypatch, tmp_path) -> None:
    d = _embedded_only(monkeypatch, tmp_path)
    _fake_cluster(d)
    monkeypatch.setenv("HR_DSN", "postgresql://aihr:fake-pw@127.0.0.1:5433/aihr")

    def boom(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr(db_admin.db, "connect", boom)
    lines: list[str] = []
    assert db_status(run=_NO_RUN, say=lines.append) == 1
    out = "\n".join(lines)
    assert "backend: embedded (stopped" in out
    assert "fake-pw" not in out


def test_db_status_prints_compose_backend_line(monkeypatch, tmp_path) -> None:
    _compose_only(monkeypatch, tmp_path)
    monkeypatch.setenv("HR_DSN", "postgresql://aihr:fake-pw@127.0.0.1:5433/aihr")

    def boom(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr(db_admin.db, "connect", boom)
    run, _calls = _runner()
    lines: list[str] = []
    db_status(run=run, say=lines.append)
    assert any("backend: compose (aihr-db: running)" in line for line in lines)


# ---------------------------------------------------------------------------
# db.env discovery candidate order (docker/.env primary, bundled middle)
# ---------------------------------------------------------------------------


def test_db_env_candidate_order_docker_then_bundled_then_fallback(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HR_HOME", str(tmp_path / "hrhome"))
    monkeypatch.setenv("AIHR_HOME_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    primary = tmp_path / "hrhome" / "docker" / ".env"
    bundled = tmp_path / "d" / "db.env"
    fallback = tmp_path / "xdg" / "aihr" / "db.env"
    assert config_resources.db_env_file() is None

    def _seed(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("AIHR_DB_PASSWORD=x\n", encoding="utf-8")

    _seed(fallback)
    assert config_resources.db_env_file() == fallback
    _seed(bundled)
    assert config_resources.db_env_file() == bundled  # bundled beats the legacy fallback
    _seed(primary)
    assert config_resources.db_env_file() == primary  # docker/.env stays primary
    bundled.unlink()
    assert config_resources.db_env_file() == primary
    primary.unlink()
    assert config_resources.db_env_file() == fallback
