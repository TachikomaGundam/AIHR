"""DB connection-layer contract tests (committed surface).

Covers hr.db: password resolution precedence, both connect() paths with
migration wiring, and the schema bootstrap entry points — all against
fakes so nothing touches a real database.
"""

from __future__ import annotations

import pytest

import hr.db as db_mod


class _FakeCursor:
    def __init__(self, rows: list[tuple] | None = None):
        self.executed: list[str] = []
        # Queue of fetchone() results; the LAST entry repeats for later probes.
        # Default = healthy sentinels so pre-existing connect tests keep the
        # fast path (no heal).
        self._rows: list[tuple] = (
            list(rows) if rows is not None else [("hr.run", "hr.model_contract")]
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def execute(self, sql: str, params=None) -> None:  # noqa: ARG002
        self.executed.append(sql)

    def fetchone(self) -> tuple:
        return self._rows.pop(0) if len(self._rows) > 1 else self._rows[0]


class _FakeConn:
    def __init__(self, rows: list[tuple] | None = None):
        self.cursor_obj = _FakeCursor(rows)
        self.committed = False
        self.closed = False
        self.call_kwargs: dict = {}

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        pass

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

# --------------------------------------------------------------------------
# Schema self-heal on connect (product invariant: zero manual db-up).
# --------------------------------------------------------------------------

_HEAL_MIGRATIONS = (
    "migrate_schema_namespace",
    "migrate_add_response_columns",
    "migrate_add_measurement_cap_column",
    "migrate_add_calibration_measurement_columns",
    "migrate_add_directional_separation",
    "migrate_run_status_columns",
    "migrate_measurement_scorer_columns",
    "migrate_dialect_contract_columns",
    "migrate_measurement_finish_reason",
)


def _patch_migrations_noop(monkeypatch) -> None:
    for name in _HEAL_MIGRATIONS:
        monkeypatch.setattr(db_mod, name, lambda c: None)


def _dsn_only(monkeypatch, conn: _FakeConn) -> None:
    monkeypatch.setattr(db_mod, "db_dsn", lambda: "postgresql://x")
    monkeypatch.setattr(db_mod.psycopg2, "connect", lambda *a, **k: conn)


def test_connect_heals_legacy_database(monkeypatch) -> None:
    """Sentinel missing -> full idempotent pipeline runs on the same conn."""
    conn = _FakeConn(rows=[(None, None), ("hr.run", "hr.model_contract")])
    _dsn_only(monkeypatch, conn)
    _patch_migrations_noop(monkeypatch)
    out = db_mod.connect()
    assert out is conn
    assert db_mod._SCHEMA_HEAL_PROBE in conn.cursor_obj.executed
    assert any(
        "CREATE TABLE IF NOT EXISTS hr.model_contract" in sql
        for sql in conn.cursor_obj.executed
    )
    assert conn.committed


def test_connect_healthy_database_skips_ddl(monkeypatch) -> None:
    conn = _FakeConn()
    _dsn_only(monkeypatch, conn)
    _patch_migrations_noop(monkeypatch)
    db_mod.connect()
    assert db_mod._SCHEMA_HEAL_PROBE in conn.cursor_obj.executed
    assert not any("CREATE" in sql for sql in conn.cursor_obj.executed)


def test_connect_escape_hatch_skips_probe(monkeypatch) -> None:
    monkeypatch.setenv("HR_NO_SCHEMA_HEAL", "1")
    conn = _FakeConn(rows=[(None, None)])
    _dsn_only(monkeypatch, conn)
    _patch_migrations_noop(monkeypatch)
    db_mod.connect()
    assert db_mod._SCHEMA_HEAL_PROBE not in conn.cursor_obj.executed
    assert not any("CREATE" in sql for sql in conn.cursor_obj.executed)


def test_connect_privilege_error_names_db_up(monkeypatch) -> None:
    import psycopg2.errors

    conn = _FakeConn(rows=[(None, None)])
    _dsn_only(monkeypatch, conn)
    _patch_migrations_noop(monkeypatch)

    def _deny(conn_, **_kw):
        raise psycopg2.errors.InsufficientPrivilege("permission denied")

    monkeypatch.setattr(db_mod, "init_schema", _deny)
    with pytest.raises(RuntimeError, match=r"hr db-up"):
        db_mod.connect()


def test_connect_duplicate_race_wins_after_reprobe(monkeypatch) -> None:
    """Concurrent healer created the tables first: duplicate error + landed
    sentinels is accepted, not surfaced."""
    import psycopg2.errors

    conn = _FakeConn(rows=[(None, None), ("hr.run", "hr.model_contract")])
    _dsn_only(monkeypatch, conn)
    _patch_migrations_noop(monkeypatch)

    def _lost_race(conn_, **_kw):
        raise psycopg2.errors.DuplicateTable('relation "model_contract" already exists')

    monkeypatch.setattr(db_mod, "init_schema", _lost_race)
    assert db_mod.connect() is conn


def test_connect_duplicate_race_without_landings_reraises(monkeypatch) -> None:
    import psycopg2.errors

    conn = _FakeConn(rows=[(None, None)])
    _dsn_only(monkeypatch, conn)
    _patch_migrations_noop(monkeypatch)

    def _lost_race(conn_, **_kw):
        raise psycopg2.errors.DuplicateTable("boom")

    monkeypatch.setattr(db_mod, "init_schema", _lost_race)
    with pytest.raises(psycopg2.errors.DuplicateTable):
        db_mod.connect()


@pytest.mark.db
@pytest.mark.integration
def test_connect_selfheals_dropped_schema(scratch_db) -> None:
    """Live-DB: after the schema is gone, connect() re-creates the whole
    table set (exact-restore) with no manual step."""
    _name, _dsn = scratch_db
    conn = db_mod.connect()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='hr'"
        )
        before = {row[0] for row in cur.fetchall()}
        cur.execute("DROP SCHEMA hr CASCADE")
    conn.commit()
    conn.close()

    healed = db_mod.connect()
    try:
        with healed.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='hr'"
            )
            after = {row[0] for row in cur.fetchall()}
        assert after == before
        assert "model_contract" in after
    finally:
        healed.close()


@pytest.mark.db
@pytest.mark.integration
def test_connect_selfheals_single_missing_table(scratch_db) -> None:
    """Live-DB legacy shape: only the newest table dropped — heal restores
    it and a second connect takes the fast path untouched."""
    _name, _dsn = scratch_db
    conn = db_mod.connect()
    with conn.cursor() as cur:
        cur.execute("DROP TABLE hr.model_contract")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('hr.model_contract')"
        )
        assert cur.fetchone()[0] is None
    conn.close()

    healed = db_mod.connect()
    try:
        with healed.cursor() as cur:
            cur.execute("SELECT to_regclass('hr.model_contract')")
            assert cur.fetchone()[0] is not None
            cur.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema='hr' AND table_name='model_contract'"
            )
            assert cur.fetchone()[0] > 0
    finally:
        healed.close()
