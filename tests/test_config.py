"""Tests for the unified config layer (offline; no DB, no mocking).

db_dsn() only ever builds a connection string — nothing here connects.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hr import config

_MONOREPO_ROOT = Path(__file__).resolve().parents[1]


def _clear_dsn_envs(monkeypatch) -> None:
    monkeypatch.delenv("HR_DSN", raising=False)
    monkeypatch.delenv("HR_DB_PASSWORD", raising=False)
    monkeypatch.delenv("HR_COMPOSE_FILE", raising=False)
    monkeypatch.delenv("HR_DB_HOST", raising=False)
    monkeypatch.delenv("HR_DB_PORT", raising=False)
    monkeypatch.delenv("HR_DB_NAME", raising=False)
    monkeypatch.delenv("HR_DB_USER", raising=False)


def test_hr_home_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    assert config.hr_home() == tmp_path


def test_hr_home_auto_detect_is_monorepo_root():
    assert config.hr_home() == _MONOREPO_ROOT


def test_config_path_lives_under_hr_home_configs(tmp_path, monkeypatch):
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    assert config.config_path("fleet.yaml") == tmp_path / "configs" / "fleet.yaml"


def test_load_yaml_resolves_from_hr_home(tmp_path, monkeypatch):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "seats.yaml").write_text("seats: []\n", encoding="utf-8")
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    assert config.load_yaml("seats.yaml") == {"seats": []}


def test_load_yaml_missing_raises_with_resolved_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    with pytest.raises(FileNotFoundError) as exc:
        config.load_yaml("seats.yaml")
    assert str(tmp_path / "configs" / "seats.yaml") in str(exc.value)


def test_db_dsn_returns_hr_dsn_verbatim(monkeypatch):
    monkeypatch.setenv("HR_DSN", "postgresql://x")
    assert config.db_dsn() == "postgresql://x"


def test_db_dsn_precedence_hr_dsn_beats_toml_and_compose(tmp_path, monkeypatch):
    """Ordering contract: with ALL sources resolvable, HR_DSN wins verbatim
    (resolution chain: HR_DSN > hr.toml+HR_DB_PASSWORD > compose fallback)."""
    (tmp_path / "hr.toml").write_text(
        'db_host = "toml.example"\n' 'db_user = "alice"\n', encoding="utf-8"
    )
    (tmp_path / "compose.yml").write_text(
        "services:\n  wiki:\n    environment:\n      DB_PASS: compose-pass\n",
        encoding="utf-8",
    )
    _clear_dsn_envs(monkeypatch)
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    monkeypatch.setenv("HR_DSN", "postgresql://env-only")
    monkeypatch.setenv("HR_DB_PASSWORD", "toml-pass")
    monkeypatch.setenv("HR_COMPOSE_FILE", str(tmp_path / "compose.yml"))
    assert config.db_dsn() == "postgresql://env-only"


def test_db_dsn_precedence_toml_beats_compose(tmp_path, monkeypatch):
    """Ordering contract: HR_DSN unset, but hr.toml + HR_DB_PASSWORD AND a
    compose fallback both resolvable -> the toml+password path wins (its
    host/user/port are taken; the compose password is NOT used)."""
    (tmp_path / "hr.toml").write_text(
        'db_host = "toml.example"\n'
        "db_port = 5433\n"
        'db_name = "hr"\n'
        'db_user = "alice"\n',
        encoding="utf-8",
    )
    (tmp_path / "compose.yml").write_text(
        "services:\n  wiki:\n    environment:\n      DB_PASS: compose-pass\n",
        encoding="utf-8",
    )
    _clear_dsn_envs(monkeypatch)
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    monkeypatch.setenv("HR_DB_PASSWORD", "toml-pass")
    monkeypatch.setenv("HR_COMPOSE_FILE", str(tmp_path / "compose.yml"))
    assert config.db_dsn() == "postgresql://alice:toml-pass@toml.example:5433/hr"


def test_db_dsn_precedence_toml_fields_with_compose_password(tmp_path, monkeypatch):
    """Ordering contract: HR_DSN unset, hr.toml present but no HR_DB_PASSWORD
    -> the opt-in compose fallback supplies the password while the toml
    fields keep their overrides (host/user/port/name)."""
    (tmp_path / "hr.toml").write_text(
        'db_host = "toml.example"\n'
        "db_port = 5434\n"
        'db_name = "hr"\n'
        'db_user = "bob"\n',
        encoding="utf-8",
    )
    (tmp_path / "compose.yml").write_text(
        "services:\n  wiki:\n    environment:\n      DB_PASS: compose-pass\n",
        encoding="utf-8",
    )
    _clear_dsn_envs(monkeypatch)
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    monkeypatch.setenv("HR_COMPOSE_FILE", str(tmp_path / "compose.yml"))
    assert config.db_dsn() == "postgresql://bob:compose-pass@toml.example:5434/hr"


def test_db_dsn_toml_plus_password_env(tmp_path, monkeypatch):
    (tmp_path / "hr.toml").write_text(
        'db_host = "db.example"\n'
        "db_port = 5433\n"
        'db_name = "hr"\n'
        'db_user = "alice"\n',
        encoding="utf-8",
    )
    _clear_dsn_envs(monkeypatch)
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    monkeypatch.setenv("HR_DB_PASSWORD", "s3cret")
    assert config.db_dsn() == "postgresql://alice:s3cret@db.example:5433/hr"


def test_db_dsn_toml_defaults_when_no_password(tmp_path, monkeypatch):
    """(a) toml-less error path: nothing resolvable -> error listing steps."""
    _clear_dsn_envs(monkeypatch)
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "seats.yaml").write_text("seats: []\n", encoding="utf-8")
    with pytest.raises(RuntimeError) as exc:
        config.db_dsn()
    msg = str(exc.value)
    assert "HR_DSN" in msg
    assert "hr.toml" in msg
    assert "HR_DB_PASSWORD" in msg
    assert "HR_COMPOSE_FILE" in msg


def test_db_dsn_compose_fallback_builds_dsn(tmp_path, monkeypatch):
    """(e) HR_COMPOSE_FILE + wiki.DB_PASS (mapping form) -> DSN with password."""
    (tmp_path / "compose.yml").write_text(
        "services:\n  wiki:\n    environment:\n      DB_PASS: compose-pass-1\n",
        encoding="utf-8",
    )
    _clear_dsn_envs(monkeypatch)
    monkeypatch.setenv("HR_HOME", str(tmp_path))  # no hr.toml: default fields
    monkeypatch.setenv("HR_COMPOSE_FILE", str(tmp_path / "compose.yml"))
    assert config.db_dsn() == (
        "postgresql://wikijs:compose-pass-1@localhost:5432/wiki"
    )


def test_db_dsn_compose_fallback_postgres_password(tmp_path, monkeypatch):
    """POSTGRES_PASSWORD is a valid secondary key in the wiki env block."""
    (tmp_path / "compose.yml").write_text(
        "services:\n  wiki:\n    environment:\n      POSTGRES_PASSWORD: pg-pass-2\n",
        encoding="utf-8",
    )
    _clear_dsn_envs(monkeypatch)
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    monkeypatch.setenv("HR_COMPOSE_FILE", str(tmp_path / "compose.yml"))
    assert config.db_dsn() == "postgresql://wikijs:pg-pass-2@localhost:5432/wiki"


def test_db_dsn_compose_fallback_list_form(tmp_path, monkeypatch):
    """List-form environment (\"- KEY=VALUE\") is supported too."""
    (tmp_path / "compose.yml").write_text(
        "services:\n  wiki:\n    environment:\n      - DB_PASS=list-pass-3\n",
        encoding="utf-8",
    )
    _clear_dsn_envs(monkeypatch)
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    monkeypatch.setenv("HR_COMPOSE_FILE", str(tmp_path / "compose.yml"))
    assert config.db_dsn() == "postgresql://wikijs:list-pass-3@localhost:5432/wiki"


def test_db_dsn_compose_missing_password_lists_step(tmp_path, monkeypatch):
    (tmp_path / "compose.yml").write_text(
        "services:\n  wiki:\n    environment:\n      DB_TYPE: postgres\n",
        encoding="utf-8",
    )
    _clear_dsn_envs(monkeypatch)
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    monkeypatch.setenv("HR_COMPOSE_FILE", str(tmp_path / "compose.yml"))
    with pytest.raises(RuntimeError) as exc:
        config.db_dsn()
    assert "DB_PASS/POSTGRES_PASSWORD" in str(exc.value)


def test_opencode_config_dir_override(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "custom-config"))
    assert config.opencode_config_dir() == (tmp_path / "custom-config").resolve()


def test_opencode_config_dir_default_under_home(monkeypatch):
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    assert config.opencode_config_dir() == Path.home() / ".config" / "opencode"


def test_load_settings_backward_compat(tmp_path, monkeypatch):
    """v1 contract: load_settings() -> Settings with env-driven fields."""
    _clear_dsn_envs(monkeypatch)
    s = config.load_settings()
    assert s.dsn == ""
    assert s.db_host == "localhost"
    monkeypatch.setenv("HR_DSN", "postgresql://env-dsn")
    assert config.load_settings().dsn == "postgresql://env-dsn"
    monkeypatch.setenv("HR_DB_PASSWORD", "hunter2")
    assert config.load_settings().db_password == "hunter2"