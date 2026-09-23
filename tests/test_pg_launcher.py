"""Hermetic tests for the embedded-Postgres layer (hr.pg_launcher / hr.pg_env).

No real binaries, no server, no docker: discovery runs against a staged fake
``D``, every spawn is a recording StubPgRunner, and the password in every
fixture is a fake value.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from hr import pg_env, pg_launcher
from hr.config_resources import read_db_env_file
from hr.pg_env import PG_HBA_TEXT
from hr.pg_launcher import ProcessResult

from tests._bundle_helpers import StubPgRunner, stage_bundle


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def test_pg_home_finds_the_staged_bundle(monkeypatch, tmp_path) -> None:
    d = stage_bundle(monkeypatch, tmp_path)
    assert pg_launcher.pg_home() == d / "pg"
    assert pg_launcher.pg_home(d) == d / "pg"


def test_pg_home_none_when_no_binaries(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AIHR_HOME_DIR", str(tmp_path / "empty"))
    monkeypatch.delenv("AIHR_PG_HOME", raising=False)
    assert pg_launcher.pg_home() is None


def test_pg_home_falls_back_to_aihr_pg_home(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AIHR_HOME_DIR", str(tmp_path / "empty"))
    alt = tmp_path / "elsewhere"
    (alt / "bin").mkdir(parents=True)
    (alt / "bin" / "initdb").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("AIHR_PG_HOME", str(alt))
    assert pg_launcher.pg_home() == alt


def test_bundled_mode_is_a_function_not_a_constant(monkeypatch, tmp_path) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    exe = tmp_path / "bundle" / "app" / "hr"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(exe))
    assert pg_launcher.bundled_mode() is False
    (tmp_path / "bundle" / "pg").mkdir()
    assert pg_launcher.bundled_mode() is True
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert pg_launcher.bundled_mode() is True


# ---------------------------------------------------------------------------
# runtime argv / env
# ---------------------------------------------------------------------------


def _expect_ld_path(env: dict[str, str], d: Path) -> None:
    if os.name != "posix":
        pytest.skip("LD_LIBRARY_PATH is POSIX-only")
    assert env["LD_LIBRARY_PATH"].startswith(str(d / "pg" / "lib"))


def test_start_argv_matches_the_pinned_shape(monkeypatch, tmp_path) -> None:
    d = stage_bundle(monkeypatch, tmp_path)
    stub = StubPgRunner()
    res = pg_launcher.start(d, 5433, root=d / "pg", pg_run=stub)
    assert res.rc == 0
    assert stub.argvs == [
        [
            str(d / "pg" / "bin" / "pg_ctl"),
            "-D", str(d / "data" / "pgdata"),
            "-l", str(d / "log" / "pg.log"),
            "-w", "-t", "60", "start",
            "-o", "-p 5433 -h 127.0.0.1",
        ]
    ]
    _expect_ld_path(stub.calls[0][2], d)


def test_start_creates_log_dir_before_pg_ctl(monkeypatch, tmp_path) -> None:
    d = stage_bundle(monkeypatch, tmp_path)
    assert not (d / "log").exists()
    pg_launcher.start(d, 5433, root=d / "pg", pg_run=StubPgRunner())
    assert (d / "log").is_dir()


def test_stop_uses_fast_mode(monkeypatch, tmp_path) -> None:
    d = stage_bundle(monkeypatch, tmp_path)
    stub = StubPgRunner()
    pg_launcher.stop(d, root=d / "pg", pg_run=stub)
    assert stub.argvs[0] == [
        str(d / "pg" / "bin" / "pg_ctl"),
        "-D", str(d / "data" / "pgdata"),
        "-m", "fast", "stop",
    ]


def test_ready_probe_psql_dsn_carries_pgpassword_env(monkeypatch, tmp_path) -> None:
    d = stage_bundle(monkeypatch, tmp_path)
    pg_env.ensure_env(d, None)
    password = read_db_env_file(d / "db.env")["AIHR_DB_PASSWORD"]
    stub = StubPgRunner()
    assert pg_launcher.ready_probe(d, 5433, root=d / "pg", pg_run=stub, connect=lambda _p: True)
    argv, _timeout, env = stub.calls[0]
    assert argv == [
        str(d / "pg" / "bin" / "psql"),
        "postgresql://aihr@127.0.0.1:5433/postgres?sslmode=disable",
        "-c", "SELECT 1",
    ]
    assert env["PGPASSWORD"] == password
    assert password not in " ".join(argv)


def test_ready_probe_skips_psql_when_tcp_closed(monkeypatch, tmp_path) -> None:
    d = stage_bundle(monkeypatch, tmp_path)
    stub = StubPgRunner()
    ok = pg_launcher.ready_probe(
        d, 5433, root=d / "pg", pg_run=stub, connect=lambda _p: False
    )
    assert ok is False and stub.calls == []


def test_wait_until_ready_polls_until_deadline(monkeypatch, tmp_path) -> None:
    d = stage_bundle(monkeypatch, tmp_path)
    sleeps: list[float] = []
    ok = pg_launcher.wait_until_ready(
        d, 5433, root=d / "pg", pg_run=StubPgRunner(), connect=lambda _p: False,
        sleep=sleeps.append, timeout_s=4, interval_s=2,
    )
    assert ok is False and sleeps == [2, 2]


def test_embedded_pid_follows_the_pid_file(monkeypatch, tmp_path) -> None:
    d = stage_bundle(monkeypatch, tmp_path)
    assert pg_launcher.embedded_pid(d) is None
    pidfile = d / "data" / "pgdata" / "postmaster.pid"
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text(f"{os.getpid()}\n", encoding="utf-8")
    assert pg_launcher.embedded_pid(d) == os.getpid()


# ---------------------------------------------------------------------------
# provisioning: db.env / initdb / conf files
# ---------------------------------------------------------------------------


def test_ensure_env_creates_once_and_never_regenerates(monkeypatch, tmp_path) -> None:
    d = stage_bundle(monkeypatch, tmp_path)
    path, port, created = pg_env.ensure_env(d, None)
    assert path == d / "db.env" and port == 5433 and created
    first = read_db_env_file(path)["AIHR_DB_PASSWORD"]
    assert pg_env.ensure_env(d, None) == (path, 5433, False)
    assert read_db_env_file(path)["AIHR_DB_PASSWORD"] == first
    if os.name != "posix":
        pytest.skip("POSIX file modes")
    assert path.stat().st_mode & 0o777 == 0o600


def test_ensure_env_reuses_db_admin_password_generator(monkeypatch, tmp_path) -> None:
    from hr import db_admin

    monkeypatch.setattr(db_admin, "generate_db_password", lambda: "fake-gen-pw")
    d = stage_bundle(monkeypatch, tmp_path)
    path, port, created = pg_env.ensure_env(d, None)
    assert created and port == 5433
    assert read_db_env_file(path)["AIHR_DB_PASSWORD"] == "fake-gen-pw"


def test_ensure_env_port_change_keeps_password(monkeypatch, tmp_path) -> None:
    d = stage_bundle(monkeypatch, tmp_path)
    path, _port, _ = pg_env.ensure_env(d, None)
    password = read_db_env_file(path)["AIHR_DB_PASSWORD"]
    _path2, port2, created = pg_env.ensure_env(d, 5440)
    assert (port2, created) == (5440, True)
    values = read_db_env_file(path)
    assert values["AIHR_DB_PASSWORD"] == password and values["AIHR_DB_PORT"] == "5440"


def test_initdb_argv_pinned_pwfile_wiped_password_never_in_argv(
    monkeypatch, tmp_path
) -> None:
    d = stage_bundle(monkeypatch, tmp_path)
    _path, port, _ = pg_env.ensure_env(d, None)
    assert port == 5433
    password = read_db_env_file(d / "db.env")["AIHR_DB_PASSWORD"]
    seen: dict[str, object] = {}

    def hook(argv: list[str], env: dict[str, str]) -> None:
        pwfile = Path(next(a for a in argv if a.startswith("--pwfile="))[len("--pwfile=") :])
        seen["exists"] = pwfile.is_file()
        seen["content"] = pwfile.read_text(encoding="utf-8") if pwfile.is_file() else ""
        seen["argv"] = list(argv)
        seen["env"] = dict(env)

    stub = StubPgRunner()
    stub.hook = hook
    assert pg_env.ensure_pgdata(d, 5433, root=d / "pg", pg_run=stub) is True
    initdb_call = stub.find("initdb")[0]
    pwfile = Path(next(a for a in initdb_call if a.startswith("--pwfile="))[len("--pwfile="):])
    assert initdb_call == [
        str(d / "pg" / "bin" / "initdb"),
        "-D", str(d / "data" / "pgdata"),
        "-U", "aihr",
        "--auth=scram-sha-256",
        f"--pwfile={pwfile}",
        "--encoding=UTF8",
    ]
    assert seen["exists"] is True and seen["content"] == password + "\n"
    assert not pwfile.exists()  # wiped after initdb returned
    assert password not in " ".join(" ".join(argv) for argv in stub.argvs)
    _expect_ld_path(dict(seen["env"]), d)  # type: ignore[arg-type]


def test_ensure_pgdata_conf_pins_are_deterministic(monkeypatch, tmp_path) -> None:
    d = stage_bundle(monkeypatch, tmp_path)
    pg_env.ensure_env(d, None)
    stub = StubPgRunner()
    assert pg_env.ensure_pgdata(d, 5439, root=d / "pg", pg_run=stub) is True
    data = d / "data" / "pgdata"
    conf = (data / "postgresql.conf").read_text(encoding="utf-8")
    assert conf.startswith("# original initdb conf")  # append-only, never rewritten
    assert pg_env.CONF_MARKER in conf
    assert "listen_addresses = '127.0.0.1'" in conf and "port = 5439" in conf
    assert (data / "pg_hba.conf").read_text(encoding="utf-8") == PG_HBA_TEXT
    assert "scram-sha-256" in PG_HBA_TEXT and "127.0.0.1/32" in PG_HBA_TEXT
    # second call: cluster exists — no initdb, conf untouched (single marker)
    assert pg_env.ensure_pgdata(d, 5439, root=d / "pg", pg_run=stub) is False
    assert len(stub.find("initdb")) == 1
    assert (data / "postgresql.conf").read_text(encoding="utf-8").count(pg_env.CONF_MARKER) == 1


def test_ensure_pgdata_failure_wipes_pwfile_and_raises(monkeypatch, tmp_path) -> None:
    d = stage_bundle(monkeypatch, tmp_path)
    pg_env.ensure_env(d, None)
    stub = StubPgRunner(initdb_rc=1)
    with pytest.raises(RuntimeError, match="initdb failed"):
        pg_env.ensure_pgdata(d, 5433, root=d / "pg", pg_run=stub)
    for argv in stub.argvs:  # every pwfile handed to initdb is gone again
        for arg in argv:
            if arg.startswith("--pwfile="):
                assert not Path(arg[len("--pwfile=") :]).exists()
    assert not (d / "data" / "pgdata" / "PG_VERSION").exists()


def test_ensure_pgdata_requires_existing_password(monkeypatch, tmp_path) -> None:
    d = stage_bundle(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="AIHR_DB_PASSWORD"):
        pg_env.ensure_pgdata(d, 5433, root=d / "pg", pg_run=StubPgRunner())


def test_ready_probe_rc_nonzero_is_not_ready(monkeypatch, tmp_path) -> None:
    d = stage_bundle(monkeypatch, tmp_path)
    pg_env.ensure_env(d, None)

    def failing(argv, timeout, env):  # noqa: ARG001
        return ProcessResult(1, "", "auth failed")

    ok = pg_launcher.ready_probe(
        d, 5433, root=d / "pg", pg_run=failing, connect=lambda _p: True
    )
    assert ok is False


def test_ensure_database_argv_targets_maintenance_db(monkeypatch, tmp_path) -> None:
    d = stage_bundle(monkeypatch, tmp_path)
    (d / "db.env").write_text(
        "AIHR_DB_PASSWORD=pw1\nAIHR_DB_PORT=5433\n", encoding="utf-8"
    )
    stub = StubPgRunner()
    res = pg_launcher.ensure_database(d, 5433, root=d / "pg", pg_run=stub)
    assert res.rc == 0
    argv = stub.argvs[-1]
    assert argv[0].endswith("createdb") or argv[0] == str(d / "pg" / "bin" / "createdb")
    # Real createdb CLI (verified against the vendored PG18.6 binary):
    # -D is TABLESPACE, the maintenance DB is --maintenance-db, dbname is the
    # trailing positional. -w keeps a bad password from hanging on a prompt.
    assert "--maintenance-db" in argv and "-D" not in argv and "-d" not in argv
    assert argv[argv.index("--maintenance-db") + 1] == "postgres"
    assert "-w" in argv
    assert argv[-1] == "aihr"


def test_ensure_database_already_exists_is_success(monkeypatch, tmp_path) -> None:
    d = stage_bundle(monkeypatch, tmp_path)
    (d / "db.env").write_text("AIHR_DB_PASSWORD=pw1\n", encoding="utf-8")

    class Exists(StubPgRunner):
        def __call__(self, argv, timeout, env):  # type: ignore[no-untyped-def]
            self.calls.append((list(argv), timeout, dict(env)))
            if "createdb" in " ".join(argv):
                return ProcessResult(1, "", 'createdb: error: database creation failed: ERROR:  database "aihr" already exists')
            return ProcessResult(0, "", "")

    assert pg_launcher.ensure_database(d, 5433, root=d / "pg", pg_run=Exists()).rc == 0


def test_ensure_database_real_failure_propagates(monkeypatch, tmp_path) -> None:
    d = stage_bundle(monkeypatch, tmp_path)
    (d / "db.env").write_text("AIHR_DB_PASSWORD=pw1\n", encoding="utf-8")

    class Boom(StubPgRunner):
        def __call__(self, argv, timeout, env):  # type: ignore[no-untyped-def]
            self.calls.append((list(argv), timeout, dict(env)))
            if "createdb" in " ".join(argv):
                return ProcessResult(1, "", "createdb: error: connection failed")
            return ProcessResult(0, "", "")

    assert pg_launcher.ensure_database(d, 5433, root=d / "pg", pg_run=Boom()).rc == 1


def test_pg_home_windows_suffix_probe(monkeypatch, tmp_path) -> None:
    """Win bundles carry initdb.exe — the bare-name probe returned None on the
    0.4.0 windows smoke (run 35832366570), silently disabling the embedded lane."""
    monkeypatch.setattr(pg_launcher, "TOOL_SUFFIX", ".exe")
    d = tmp_path / "aihr"
    (d / "pg" / "bin").mkdir(parents=True)
    (d / "pg" / "bin" / "initdb.exe").write_text("", encoding="utf-8")
    assert pg_launcher.pg_home(d) == d / "pg"
    e = tmp_path / "bare"  # a POSIX-style name must NOT satisfy the win probe
    (e / "pg" / "bin").mkdir(parents=True)
    (e / "pg" / "bin" / "initdb").write_text("", encoding="utf-8")
    monkeypatch.setenv("AIHR_PG_HOME", "")
    assert pg_launcher.pg_home(e) is None


def test_pg_bin_dir_windows_suffix(monkeypatch) -> None:
    monkeypatch.setattr(pg_launcher, "TOOL_SUFFIX", ".exe")
    assert pg_launcher.pg_bin_dir(Path("/x"), "pg_ctl") == "/x/bin/pg_ctl.exe"
