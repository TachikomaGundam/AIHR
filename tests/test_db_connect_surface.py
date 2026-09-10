"""DB connection-layer contract tests (committed surface).

Covers hr.db: password resolution precedence, both connect() paths with
migration wiring, and the schema bootstrap entry points — all against
fakes so nothing touches a real database.
"""

from __future__ import annotations

import pytest

import hr.db as db_mod


class _FakeCursor:
    def __init__(self):
        self.executed: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def execute(self, sql: str, params=None) -> None:  # noqa: ARG002
        self.executed.append(sql)


class _FakeConn:
    def __init__(self):
        self.cursor_obj = _FakeCursor()
        self.committed = False
        self.closed = False
        self.call_kwargs: dict = {}

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True


def _seal_db_env(monkeypatch, tmp_path) -> None:
    """Seal the password chain: no hr.toml, no env file, no compose."""
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    monkeypatch.delenv("HR_DSN", raising=False)
    monkeypatch.delenv("HR_DB_PASSWORD", raising=False)
    monkeypatch.delenv("HR_COMPOSE_FILE", raising=False)
    monkeypatch.delenv("HR_DB_NAME", raising=False)
    monkeypatch.delenv("HR_DB_HOST", raising=False)
    monkeypatch.delenv("HR_DB_PORT", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)


def test_load_db_password_from_env(monkeypatch, tmp_path) -> None:
    _seal_db_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HR_DB_PASSWORD", "sekrit")
    assert db_mod._load_db_password() == "sekrit"


def test_load_db_password_from_env_file(monkeypatch, tmp_path) -> None:
    """Chain step 2: the docker/.env written by `hr db-up`."""
    _seal_db_env(monkeypatch, tmp_path)
    docker = tmp_path / "docker"
    docker.mkdir()
    (docker / ".env").write_text(
        "AIHR_DB_PASSWORD=file-pass\nAIHR_DB_PORT=5433\n", encoding="utf-8"
    )
    assert db_mod._load_db_password() == "file-pass"


def test_load_db_password_env_var_beats_env_file(monkeypatch, tmp_path) -> None:
    _seal_db_env(monkeypatch, tmp_path)
    docker = tmp_path / "docker"
    docker.mkdir()
    (docker / ".env").write_text("AIHR_DB_PASSWORD=file-pass\n", encoding="utf-8")
    monkeypatch.setenv("HR_DB_PASSWORD", "env-pass")
    assert db_mod._load_db_password() == "env-pass"


def test_load_db_password_from_compose(monkeypatch, tmp_path) -> None:
    _seal_db_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HR_COMPOSE_FILE", str(tmp_path / "compose.yml"))
    monkeypatch.setattr(
        db_mod, "compose_db_password", lambda path: "compose-pass"
    )
    assert db_mod._load_db_password() == "compose-pass"


def test_load_db_password_fails_loud(monkeypatch, tmp_path) -> None:
    """Chain exhausted: the error names every attempted step."""
    _seal_db_env(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="cannot resolve DB password") as exc:
        db_mod._load_db_password()
    msg = str(exc.value)
    assert "HR_DB_PASSWORD" in msg
    assert "hr db-up" in msg
    assert "HR_COMPOSE_FILE" in msg


def test_connect_via_db_dsn(monkeypatch) -> None:
    conn = _FakeConn()
    monkeypatch.setattr(db_mod, "db_dsn", lambda: "postgresql://x")
    monkeypatch.setattr(db_mod.psycopg2, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(db_mod, "migrate_schema_namespace", lambda c: c.cursor_obj.executed.append("migrate"))
    out = db_mod.connect()
    assert out is conn
    assert "migrate" in conn.cursor_obj.executed


def test_connect_falls_back_to_env_password(monkeypatch, tmp_path) -> None:
    """No hardcoded wiki/wikijs/5432: the fallback path routes dbname/user/
    host/port through hr.config (sealed here to the fresh aihr defaults)."""
    conn = _FakeConn()
    calls: dict = {}
    _seal_db_env(monkeypatch, tmp_path)

    def fake_pg_connect(**kwargs):
        calls["kwargs"] = kwargs
        return conn

    def _dsn_boom():
        raise RuntimeError("no dsn")

    monkeypatch.setattr(db_mod, "db_dsn", _dsn_boom)
    monkeypatch.setattr(db_mod, "_load_db_password", lambda: "env-pass")
    monkeypatch.setattr(db_mod.psycopg2, "connect", fake_pg_connect)
    monkeypatch.setattr(db_mod, "migrate_schema_namespace", lambda c: None)
    out = db_mod.connect()
    assert out is conn
    assert calls["kwargs"]["password"] == "env-pass"
    assert calls["kwargs"]["user"] == "aihr"
    assert calls["kwargs"]["dbname"] == "aihr"
    assert calls["kwargs"]["host"] == "localhost"
    assert calls["kwargs"]["port"] == 5433


def test_connect_explicit_fields_win(monkeypatch, tmp_path) -> None:
    conn = _FakeConn()
    calls: dict = {}
    _seal_db_env(monkeypatch, tmp_path)
    monkeypatch.setattr(db_mod, "migrate_schema_namespace", lambda c: None)
    monkeypatch.setattr(
        db_mod.psycopg2, "connect", lambda **kwargs: calls.update(kwargs) or conn
    )
    out = db_mod.connect(
        dbname="other", user="ops", host="db.example", port=6000, password="pw"
    )
    assert out is conn
    assert calls == {
        "dbname": "other",
        "user": "ops",
        "host": "db.example",
        "port": 6000,
        "password": "pw",
    }


def test_connect_explicit_password(monkeypatch) -> None:
    conn = _FakeConn()
    monkeypatch.setattr(db_mod.psycopg2, "connect", lambda **kwargs: conn)
    monkeypatch.setattr(db_mod, "migrate_schema_namespace", lambda c: None)
    out = db_mod.connect(password="direct")
    assert out is conn


def test_migrations_run_in_order(monkeypatch) -> None:
    conn = _FakeConn()
    order: list[str] = []
    for name in (
        "migrate_schema_namespace",
        "migrate_add_response_columns",
        "migrate_add_measurement_cap_column",
        "migrate_add_calibration_measurement_columns",
        "migrate_add_directional_separation",
        "migrate_run_status_columns",
        "migrate_measurement_scorer_columns",
    ):
        monkeypatch.setattr(db_mod, name, lambda c, _n=name: order.append(_n))
    db_mod._run_migrations(conn)
    assert order == [
        "migrate_schema_namespace",
        "migrate_add_response_columns",
        "migrate_add_measurement_cap_column",
        "migrate_add_calibration_measurement_columns",
        "migrate_add_directional_separation",
        "migrate_run_status_columns",
        "migrate_measurement_scorer_columns",
    ]


def test_ddl_exposes_schema() -> None:
    assert "CREATE SCHEMA" in db_mod.ddl() or "CREATE TABLE" in db_mod.ddl()


def test_connect_migrate_closes_own_connection(monkeypatch) -> None:
    conn = _FakeConn()
    monkeypatch.setattr(db_mod, "connect", lambda: conn)
    monkeypatch.setattr(db_mod, "migrate_schema_namespace", lambda c: None)
    monkeypatch.setattr(db_mod, "migrate_add_response_columns", lambda c: None)
    monkeypatch.setattr(db_mod, "migrate_add_measurement_cap_column", lambda c: None)
    monkeypatch.setattr(db_mod, "migrate_add_calibration_measurement_columns", lambda c: None)
    monkeypatch.setattr(db_mod, "migrate_add_directional_separation", lambda c: None)
    monkeypatch.setattr(db_mod, "migrate_run_status_columns", lambda c: None)
    monkeypatch.setattr(db_mod, "migrate_measurement_scorer_columns", lambda c: None)
    db_mod.migrate()
    assert conn.closed


def test_init_schema_executes_ddl_and_migrations(monkeypatch) -> None:
    conn = _FakeConn()
    monkeypatch.setattr(db_mod, "migrate_schema_namespace", lambda c: None)
    monkeypatch.setattr(db_mod, "migrate_add_response_columns", lambda c: None)
    monkeypatch.setattr(db_mod, "migrate_add_measurement_cap_column", lambda c: None)
    monkeypatch.setattr(db_mod, "migrate_add_calibration_measurement_columns", lambda c: None)
    monkeypatch.setattr(db_mod, "migrate_add_directional_separation", lambda c: None)
    monkeypatch.setattr(db_mod, "migrate_run_status_columns", lambda c: None)
    monkeypatch.setattr(db_mod, "migrate_measurement_scorer_columns", lambda c: None)
    db_mod.init_schema(conn, own_connection=False)
    assert conn.committed
    assert any("CREATE" in sql for sql in conn.cursor_obj.executed)
    assert conn.closed is False


def test_init_schema_owns_and_closes_connection(monkeypatch) -> None:
    conn = _FakeConn()
    monkeypatch.setattr(db_mod, "connect", lambda: conn)
    for name in (
        "migrate_schema_namespace",
        "migrate_add_response_columns",
        "migrate_add_measurement_cap_column",
        "migrate_add_calibration_measurement_columns",
        "migrate_add_directional_separation",
        "migrate_run_status_columns",
        "migrate_measurement_scorer_columns",
    ):
        monkeypatch.setattr(db_mod, name, lambda c: None)
    db_mod.init_schema()
    assert conn.closed