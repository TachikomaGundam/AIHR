"""Hermetic tests for the AIHR turnkey database layer (hr.db_admin / hr.cli_db).

No docker, no live DB: every subprocess goes through an injected fake Runner,
every filesystem effect lands in tmp_path (HR_HOME/HOME sealed), and every
password in a fixture is a fake value.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest
import yaml
from psycopg2 import sql

from hr import db_admin
from hr.db_admin import CommandResult
from hr.db_admin import (
    compose_argv,
    container_status,
    db_down,
    db_status,
    db_up,
    ensure_db_env_file,
    generate_db_password,
    is_port_bound,
    mask_dsn,
    read_db_env_file,
    render_db_env,
    wait_for_ready,
    write_db_env_file,
)
from hr.db import schema_stats

def _seed_env(monkeypatch, tmp_path: Path) -> Path:
    """HR_HOME=tmp (no hr.toml, no ambient db env file); returns docker dir."""
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    monkeypatch.delenv("HR_DSN", raising=False)
    monkeypatch.delenv("HR_DB_PASSWORD", raising=False)
    monkeypatch.delenv("HR_COMPOSE_FILE", raising=False)
    monkeypatch.delenv("HR_DB_NAME", raising=False)
    monkeypatch.delenv("HR_DB_HOST", raising=False)
    monkeypatch.delenv("HR_DB_PORT", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return tmp_path / "docker"


class _StatsCursor:
    def __init__(self):
        self.executed: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def execute(self, stmt, params=None) -> None:  # noqa: ARG002
        self.executed.append(stmt)

    def fetchall(self):
        return [("model",), ("seat",)]

    def fetchone(self):
        return (1,) if str(self.executed[-1]) == "SELECT 1" else (3,)


class _StatsConn:
    def __init__(self):
        self.cursor_obj = _StatsCursor()
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self) -> None:
        self.closed = True


def _fake_pg(monkeypatch, conn: _StatsConn) -> list:
    inits: list[int] = []

    def fake_init_schema() -> None:
        inits.append(1)

    monkeypatch.setattr(db_admin.db, "init_schema", fake_init_schema)
    monkeypatch.setattr(db_admin.db, "connect", lambda *a, **k: conn)
    return inits


def _runner(state: str | None = "running", *, up_rc: int = 0, ready_rc: int = 0):
    calls: list[list[str]] = []

    def run(argv: list[str], timeout: int) -> CommandResult:  # noqa: ARG001
        calls.append(argv)
        joined = " ".join(argv)
        if "inspect" in joined:
            if state is None:
                return CommandResult(1, "", "No such object")
            return CommandResult(0, state + "\n", "")
        if "pg_isready" in joined:
            return CommandResult(ready_rc, "", "")
        if argv[-2:] == ["up", "-d"]:
            return CommandResult(up_rc, "", "" if up_rc == 0 else "port is already allocated")
        return CommandResult(0, "", "")

    return run, calls


# ---------------------------------------------------------------------------
# shipped compose manifest (contract a)
# ---------------------------------------------------------------------------


def test_shipped_compose_manifest_matches_the_turnkey_contract() -> None:
    # Given: the docker-compose.yml the wheel ships at share/aihr/docker/.
    repo_root = Path(db_admin.__file__).resolve().parents[1]
    manifest = yaml.safe_load((repo_root / "docker" / "docker-compose.yml").read_text())
    service = manifest["services"]["aihr-db"]
    # Then: pinned image/name/restart/volume/port/env, and NO schema bootstrap SQL.
    assert service["image"] == "postgres:16-alpine"
    assert service["container_name"] == "aihr-db"
    assert service["restart"] == "unless-stopped"
    assert service["volumes"] == ["aihr-db-data:/var/lib/postgresql/data"]
    assert service["ports"] == ["127.0.0.1:${AIHR_DB_PORT:-5433}:5432"]
    assert service["environment"] == {
        "POSTGRES_DB": "aihr",
        "POSTGRES_USER": "aihr",
        "POSTGRES_PASSWORD": "${AIHR_DB_PASSWORD}",
    }
    assert "docker-entrypoint-initdb.d" not in yaml.safe_dump(manifest)
    assert manifest["volumes"] == {"aihr-db-data": None}


# ---------------------------------------------------------------------------
# db env file: generate / persist / permissions / discovery / fallback
# ---------------------------------------------------------------------------


def test_generate_db_password_is_random_and_urlsafe() -> None:
    first, second = generate_db_password(), generate_db_password()
    assert first != second
    assert len(first) >= 32
    assert all(c.isalnum() or c in "-_" for c in first)


def test_render_and_parse_env_file_roundtrip(tmp_path) -> None:
    text = render_db_env("fake-pw-1", 5441)
    path = tmp_path / "db.env"
    path.write_text(text, encoding="utf-8")
    assert read_db_env_file(path) == {"AIHR_DB_PASSWORD": "fake-pw-1", "AIHR_DB_PORT": "5441"}


def test_read_db_env_file_skips_comments_and_strips_quotes(tmp_path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        '# generated\n\nAIHR_DB_PASSWORD="quoted-pw"\nbroken line without equals\n',
        encoding="utf-8",
    )
    assert read_db_env_file(path) == {"AIHR_DB_PASSWORD": "quoted-pw"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_write_db_env_file_is_mode_0600(tmp_path) -> None:
    path = tmp_path / "sub" / ".env"
    write_db_env_file(path, "fake-pw-2", 5433)
    assert path.read_text(encoding="utf-8").startswith("# AIHR database credentials")
    assert path.stat().st_mode & 0o777 == 0o600


def test_ensure_db_env_file_creates_once_and_keeps_password(monkeypatch, tmp_path) -> None:
    docker = _seed_env(monkeypatch, tmp_path)
    path, port, created = ensure_db_env_file(None)
    assert path == docker / ".env" and port == 5433 and created
    first = read_db_env_file(path)["AIHR_DB_PASSWORD"]
    path2, port2, created2 = ensure_db_env_file(None)
    assert (path2, port2, created2) == (path, 5433, False)
    assert read_db_env_file(path2)["AIHR_DB_PASSWORD"] == first  # idempotent


def test_ensure_db_env_file_port_change_keeps_password(monkeypatch, tmp_path) -> None:
    _seed_env(monkeypatch, tmp_path)
    path, _port, _ = ensure_db_env_file(5433)
    password = read_db_env_file(path)["AIHR_DB_PASSWORD"]
    path2, port2, created = ensure_db_env_file(5440)
    values = read_db_env_file(path2)
    assert (port2, created) == (5440, True)
    assert values["AIHR_DB_PASSWORD"] == password
    assert values["AIHR_DB_PORT"] == "5440"


def test_ensure_db_env_file_falls_back_to_data_dir_when_readonly(
    monkeypatch, tmp_path
) -> None:
    docker = _seed_env(monkeypatch, tmp_path)
    docker.mkdir()
    docker.chmod(0o555)
    home = tmp_path / "fake-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    try:
        path, _port, created = ensure_db_env_file(None)
        expected = home / ".local" / "share" / "aihr" / "db.env"
        assert path == expected and created
        assert read_db_env_file(path)["AIHR_DB_PASSWORD"]
    finally:
        docker.chmod(0o755)


def test_db_env_file_discovery_prefers_existing_docker_env(monkeypatch, tmp_path) -> None:
    from hr import config

    _seed_env(monkeypatch, tmp_path)
    assert config.db_env_file() is None
    write_db_env_file(tmp_path / "docker" / ".env", "primary-pw", 5433)
    assert config.db_env_file() == tmp_path / "docker" / ".env"


# ---------------------------------------------------------------------------
# argv assembly / port probe / container probes / readiness poll
# ---------------------------------------------------------------------------


def test_compose_argv_pins_project_directory_and_env_file(monkeypatch, tmp_path) -> None:
    _seed_env(monkeypatch, tmp_path)
    env_file = tmp_path / "aihr-data" / "db.env"
    argv = compose_argv(env_file, ["up", "-d"])
    assert argv[:3] == ["docker", "compose", "-p"]
    assert argv[3] == "aihr"
    assert argv[4:6] == ["--project-directory", str(tmp_path / "docker")]
    assert argv[6:8] == ["--env-file", str(env_file)]
    assert argv[8:] == ["up", "-d"]
    assert "--env-file" not in compose_argv(None, ["stop"])


def test_is_port_bound_true_only_while_the_port_listens() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        assert is_port_bound(port) is True
    assert is_port_bound(port) is False


def test_container_status_maps_inspect_state() -> None:
    run, _calls = _runner(state="running")
    assert container_status(run) == "running"
    absent, _ = _runner(state=None)
    assert container_status(absent) == ""


def test_wait_for_ready_polls_until_success_then_times_out() -> None:
    attempts: list[float] = []
    replies = iter([CommandResult(1, "", ""), CommandResult(0, "", "")])

    def flaky(argv, timeout):  # noqa: ARG001
        return next(replies)

    assert wait_for_ready(flaky, timeout_s=10, interval_s=1, sleep=attempts.append) is True
    assert attempts == [1]

    stuck, _ = _runner(ready_rc=1)
    assert wait_for_ready(stuck, timeout_s=0.02, interval_s=0.01, sleep=lambda _s: None) is False


# ---------------------------------------------------------------------------
# mask / stats
# ---------------------------------------------------------------------------


def test_mask_dsn_hides_password_but_keeps_routing() -> None:
    masked = mask_dsn("postgresql://aihr:sup3r-secret-pw@localhost:5433/aihr")
    assert masked == "postgresql://aihr:***@localhost:5433/aihr"
    assert "sup3r" not in masked
    assert mask_dsn("postgresql://localhost/db") == "postgresql://localhost/db"


def test_schema_stats_counts_tables_and_rows() -> None:
    conn = _StatsConn()
    tables, rows = schema_stats(conn)
    assert (tables, rows) == (2, 6)
    listing, *counts = conn.cursor_obj.executed
    assert "table_schema = 'hr'" in str(listing)
    assert all("count(*)" in str(stmt) for stmt in counts)
    identifiers = [
        tuple(part._wrapped)
        for stmt in counts
        for part in stmt._wrapped
        if isinstance(part, sql.Identifier)
    ]
    assert identifiers == [("hr", "model"), ("hr", "seat")]


# ---------------------------------------------------------------------------
# db_up — branches and argv, all hermetic
# ---------------------------------------------------------------------------


def _prepare_db_up(monkeypatch, tmp_path) -> tuple[list, _StatsConn]:
    docker = _seed_env(monkeypatch, tmp_path)
    docker.mkdir(parents=True, exist_ok=True)
    (docker / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(db_admin.shutil, "which", lambda name: f"/usr/bin/{name}")
    conn = _StatsConn()
    inits = _fake_pg(monkeypatch, conn)
    return inits, conn


def test_db_up_requires_docker(monkeypatch, tmp_path) -> None:
    _seed_env(monkeypatch, tmp_path)
    monkeypatch.setattr(db_admin.shutil, "which", lambda _name: None)
    lines: list[str] = []
    assert db_up(say=lines.append) == 1
    assert any("docker not found" in line for line in lines)


def test_db_up_happy_path_generates_env_and_initializes_schema(monkeypatch, tmp_path) -> None:
    inits, conn = _prepare_db_up(monkeypatch, tmp_path)
    run, calls = _runner()
    lines: list[str] = []
    assert db_up(run=run, sleep=lambda _s: None, say=lines.append) == 0
    env_file = tmp_path / "docker" / ".env"
    assert env_file.is_file()
    password = read_db_env_file(env_file)["AIHR_DB_PASSWORD"]
    assert ["up", "-d"] in [argv[-2:] for argv in calls if argv[:2] == ["docker", "compose"]]
    assert any("pg_isready" in argv for argv in calls)
    assert inits == [1] and conn.closed
    out = "\n".join(lines)
    assert "container: running" in out
    assert "port: 127.0.0.1:5433" in out
    assert "schema: hr — 2 tables" in out
    assert password not in out and "AIHR_DB_PASSWORD" not in out


def test_db_up_refuses_foreign_port_with_hint(monkeypatch, tmp_path) -> None:
    _prepare_db_up(monkeypatch, tmp_path)
    run, calls = _runner(state=None)
    monkeypatch.setattr(db_admin, "is_port_bound", lambda *_a, **_k: True)
    lines: list[str] = []
    assert db_up(run=run, sleep=lambda _s: None, say=lines.append) == 1
    assert any("--port" in line for line in lines)
    assert not any(argv[-2:] == ["up", "-d"] for argv in calls if argv[:2] == ["docker", "compose"])


def test_db_up_proceeds_when_our_container_holds_the_port(monkeypatch, tmp_path) -> None:
    _prepare_db_up(monkeypatch, tmp_path)
    run, calls = _runner(state="running")
    monkeypatch.setattr(db_admin, "is_port_bound", lambda *_a, **_k: True)
    lines: list[str] = []
    assert db_up(run=run, sleep=lambda _s: None, say=lines.append) == 0
    assert any(argv[-2:] == ["up", "-d"] for argv in calls)


def test_db_up_propagates_compose_failure(monkeypatch, tmp_path) -> None:
    _prepare_db_up(monkeypatch, tmp_path)
    run, _calls = _runner(up_rc=17)
    lines: list[str] = []
    assert db_up(run=run, sleep=lambda _s: None, say=lines.append) == 17
    assert any("rc=17" in line for line in lines)


def test_db_up_times_out_when_never_ready(monkeypatch, tmp_path) -> None:
    _prepare_db_up(monkeypatch, tmp_path)
    run, _calls = _runner(ready_rc=1)
    monkeypatch.setattr(db_admin, "READY_TIMEOUT_S", 0.02)
    monkeypatch.setattr(db_admin, "READY_POLL_INTERVAL_S", 0.01)
    lines: list[str] = []
    assert db_up(run=run, sleep=lambda _s: None, say=lines.append) == 1
    assert any("not ready" in line for line in lines)


# --- foreign-takeover guard (one shared aihr-db per host) -------------------


def _guard_runner(
    state: str | None = "running", *, label: str = "", label_rc: int = 0
):
    """Runner that answers the config_files label inspect distinctly from the
    state inspect; ``label`` is what the container's compose label reports."""
    calls: list[list[str]] = []

    def run(argv: list[str], timeout: int) -> CommandResult:  # noqa: ARG001
        calls.append(argv)
        joined = " ".join(argv)
        if "config_files" in joined:
            if label_rc != 0:
                return CommandResult(label_rc, "", "template parse error")
            return CommandResult(0, label + "\n", "")
        if "inspect" in joined:
            if state is None:
                return CommandResult(1, "", "No such object")
            return CommandResult(0, state + "\n", "")
        if "pg_isready" in joined:
            return CommandResult(0, "", "")
        if argv[-2:] == ["up", "-d"]:
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")

    return run, calls


def _no_compose_up(calls: list[list[str]]) -> bool:
    return not any(
        argv[:2] == ["docker", "compose"] and argv[-2:] == ["up", "-d"] for argv in calls
    )


@pytest.mark.parametrize("label", ["", "<no value>"])
def test_db_up_refuses_foreign_container_without_compose_label(
    monkeypatch, tmp_path, label
) -> None:
    _prepare_db_up(monkeypatch, tmp_path)
    run, calls = _guard_runner(state="running", label=label)
    lines: list[str] = []
    assert db_up(run=run, sleep=lambda _s: None, say=lines.append) == 1
    out = "\n".join(lines)
    assert "foreign" in out and "--force" in out
    assert _no_compose_up(calls)


def test_db_up_refuses_when_existing_container_label_unreadable(
    monkeypatch, tmp_path
) -> None:
    _prepare_db_up(monkeypatch, tmp_path)
    run, calls = _guard_runner(state="exited", label_rc=1)
    lines: list[str] = []
    assert db_up(run=run, sleep=lambda _s: None, say=lines.append) == 1
    out = "\n".join(lines)
    assert "foreign" in out and "--force" in out
    assert _no_compose_up(calls)


def test_db_up_refuses_container_managed_by_other_compose_file(
    monkeypatch, tmp_path
) -> None:
    _prepare_db_up(monkeypatch, tmp_path)
    foreign = "/opt/other-aihr-install/docker/docker-compose.yml"
    run, calls = _guard_runner(state="running", label=foreign)
    lines: list[str] = []
    assert db_up(run=run, sleep=lambda _s: None, say=lines.append) == 1
    out = "\n".join(lines)
    ours = str((tmp_path / "docker" / "docker-compose.yml").resolve())
    assert foreign in out and ours in out  # error names BOTH paths
    assert "db-down" in out and "one install" in out  # the advice
    assert _no_compose_up(calls)


def test_db_up_proceeds_when_label_matches_our_compose_file(
    monkeypatch, tmp_path
) -> None:
    _prepare_db_up(monkeypatch, tmp_path)
    ours = str((tmp_path / "docker" / "docker-compose.yml").resolve())
    run, calls = _guard_runner(state="running", label=f", {ours} ,")
    lines: list[str] = []
    assert db_up(run=run, sleep=lambda _s: None, say=lines.append) == 0
    assert any(argv[-2:] == ["up", "-d"] for argv in calls)  # same-install re-up


def test_db_up_proceeds_when_container_absent(monkeypatch, tmp_path) -> None:
    _prepare_db_up(monkeypatch, tmp_path)
    run, calls = _guard_runner(state=None)
    monkeypatch.setattr(db_admin, "is_port_bound", lambda *_a, **_k: False)
    lines: list[str] = []
    assert db_up(run=run, sleep=lambda _s: None, say=lines.append) == 0
    assert any(argv[-2:] == ["up", "-d"] for argv in calls)


def test_db_up_force_proceeds_despite_foreign_label(monkeypatch, tmp_path) -> None:
    _prepare_db_up(monkeypatch, tmp_path)
    run, calls = _guard_runner(state="running", label="/opt/other/docker-compose.yml")
    lines: list[str] = []
    assert db_up(force=True, run=run, sleep=lambda _s: None, say=lines.append) == 0
    assert any(argv[-2:] == ["up", "-d"] for argv in calls)
    # --force skips the ownership guard entirely
    assert not any("config_files" in " ".join(argv) for argv in calls)


def test_db_up_force_does_not_override_the_port_guard(monkeypatch, tmp_path) -> None:
    _prepare_db_up(monkeypatch, tmp_path)
    run, calls = _guard_runner(state=None)
    monkeypatch.setattr(db_admin, "is_port_bound", lambda *_a, **_k: True)
    lines: list[str] = []
    assert db_up(force=True, run=run, sleep=lambda _s: None, say=lines.append) == 1
    assert any("--port" in line for line in lines)
    assert _no_compose_up(calls)


def test_db_up_reports_schema_init_failure_without_traceback(monkeypatch, tmp_path) -> None:
    docker = _seed_env(monkeypatch, tmp_path)
    docker.mkdir(parents=True, exist_ok=True)
    (docker / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(db_admin.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(db_admin.db, "init_schema", lambda: (_ for _ in ()).throw(RuntimeError("dsn unresolvable")))
    run, _calls = _runner()
    lines: list[str] = []
    assert db_up(run=run, sleep=lambda _s: None, say=lines.append) == 1
    assert any("schema initialization failed: dsn unresolvable" in line for line in lines)


# ---------------------------------------------------------------------------
# db_down
# ---------------------------------------------------------------------------


def test_db_down_defaults_to_stop(monkeypatch, tmp_path) -> None:
    _seed_env(monkeypatch, tmp_path)
    monkeypatch.setattr(db_admin.shutil, "which", lambda name: f"/usr/bin/{name}")
    run, calls = _runner()
    lines: list[str] = []
    assert db_down(run=run, say=lines.append) == 0
    assert calls[-1][-1] == "stop" and "down" not in calls[-1]


def test_db_down_purge_requires_yes_and_never_deletes_without_it(
    monkeypatch, tmp_path
) -> None:
    _seed_env(monkeypatch, tmp_path)
    monkeypatch.setattr(db_admin.shutil, "which", lambda name: f"/usr/bin/{name}")
    run, calls = _runner()
    lines: list[str] = []
    assert db_down(purge=True, run=run, say=lines.append) == 1
    assert calls == []
    assert any("--yes" in line for line in lines)
    assert db_down(purge=True, yes=True, run=run, say=lines.append) == 0
    assert calls[-1][-2:] == ["down", "-v"]


# ---------------------------------------------------------------------------
# db_status
# ---------------------------------------------------------------------------


def test_db_status_reports_resolution_step_masked(monkeypatch, tmp_path) -> None:
    docker = _seed_env(monkeypatch, tmp_path)
    docker.mkdir(parents=True, exist_ok=True)
    write_db_env_file(docker / ".env", "fake-status-pw", 5433)
    (docker / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(db_admin.shutil, "which", lambda name: f"/usr/bin/{name}")
    conn = _StatsConn()
    monkeypatch.setattr(db_admin.db, "connect", lambda *a, **k: conn)
    run, _calls = _runner()
    lines: list[str] = []
    assert db_status(run=run, say=lines.append) == 0
    out = "\n".join(lines)
    assert "resolved-by: AIHR db env file" in out
    assert "sql-probe: ok" in out
    assert "hr-schema: 2 tables, 6 total rows" in out
    assert "fake-status-pw" not in out
    assert "aihr:***@" in out


def test_db_status_fails_loud_when_dsn_unresolvable(monkeypatch, tmp_path) -> None:
    _seed_env(monkeypatch, tmp_path)
    monkeypatch.setattr(db_admin.shutil, "which", lambda _name: None)
    lines: list[str] = []
    assert db_status(run=lambda argv, timeout: CommandResult(0, "", ""), say=lines.append) == 1
    out = "\n".join(lines)
    assert "cannot resolve HR database DSN" in out
    assert "hr db-up" in out


def test_db_status_reports_sql_failure(monkeypatch, tmp_path) -> None:
    _seed_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HR_DSN", "postgresql://aihr:fake-pw@localhost:5433/aihr")
    monkeypatch.setattr(db_admin.shutil, "which", lambda _name: None)

    def boom(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr(db_admin.db, "connect", boom)
    lines: list[str] = []
    assert db_status(run=lambda argv, timeout: CommandResult(0, "", ""), say=lines.append) == 1
    out = "\n".join(lines)
    assert "resolved-by: HR_DSN env var" in out
    assert "sql-probe: FAILED" in out
    assert "fake-pw" not in out


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_db_commands_registered_on_app() -> None:
    from typer.main import get_command

    from hr.cli import app

    commands = get_command(app).commands
    assert {"db-up", "db-down", "db-status"} <= set(commands)


def test_cli_db_up_help_exposes_port_option() -> None:
    from typer.testing import CliRunner

    from hr.cli import app

    result = CliRunner().invoke(app, ["db-up", "--help"])
    assert result.exit_code == 0
    assert "--port" in result.output
