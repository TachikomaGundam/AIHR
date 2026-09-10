"""Tests for the unified config layer (offline; no DB, no mocking).

db_dsn() only ever builds a connection string — nothing here connects.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hr import config
from hr import config_resources

_MONOREPO_ROOT = Path(__file__).resolve().parents[1]


def _clear_dsn_envs(monkeypatch) -> None:
    monkeypatch.delenv("HR_DSN", raising=False)
    monkeypatch.delenv("HR_DB_PASSWORD", raising=False)
    monkeypatch.delenv("HR_COMPOSE_FILE", raising=False)
    monkeypatch.delenv("HR_DB_HOST", raising=False)
    monkeypatch.delenv("HR_DB_PORT", raising=False)
    monkeypatch.delenv("HR_DB_NAME", raising=False)
    monkeypatch.delenv("HR_DB_USER", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)


def test_hr_home_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    assert config.hr_home() == tmp_path


def test_hr_home_auto_detect_is_monorepo_root():
    assert config.hr_home() == _MONOREPO_ROOT


def test_hr_home_uses_installed_share_when_source_configs_are_absent(
    tmp_path, monkeypatch
):
    # Given: a wheel-style install with configs under the interpreter prefix.
    package_file = tmp_path / "site-packages" / "hr" / "config.py"
    installed_home = tmp_path / "share" / "aihr"
    (installed_home / "configs").mkdir(parents=True)
    monkeypatch.delenv("HR_HOME", raising=False)
    monkeypatch.setattr(config_resources, "__file__", str(package_file))
    monkeypatch.setattr(config_resources.sys, "prefix", str(tmp_path))

    # When/Then: resource resolution chooses installed data, not site-packages.
    assert config.hr_home() == installed_home


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


def test_db_dsn_encodes_reserved_credential_characters(tmp_path, monkeypatch):
    # Given: database credentials containing URI delimiters.
    (tmp_path / "hr.toml").write_text(
        'db_name = "hr data"\n'
        'db_user = "alice@example.com"\n',
        encoding="utf-8",
    )
    _clear_dsn_envs(monkeypatch)
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    monkeypatch.setenv("HR_DB_PASSWORD", "p@ss:/word")

    # When / Then: each credential component remains within its URI field,
    # and an hr.toml without db_port lands on the new aihr default port.
    assert config.db_dsn() == (
        "postgresql://alice%40example.com:p%40ss%3A%2Fword@localhost:5433/hr%20data"
    )


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
    assert "hr db-up" in msg
    assert "AIHR db env file" in msg


# ---------------------------------------------------------------------------
# AIHR turnkey db env file (chain step 3)
# ---------------------------------------------------------------------------


def _write_env_file(tmp_path, body: str):
    docker = tmp_path / "docker"
    docker.mkdir(parents=True, exist_ok=True)
    (docker / ".env").write_text(body, encoding="utf-8")


def test_db_dsn_env_file_resolves_aihr_stack(tmp_path, monkeypatch):
    """No hr.toml, no HR_DB_PASSWORD, no compose: the generated docker/.env
    alone resolves the full DSN on the new aihr defaults."""
    _clear_dsn_envs(monkeypatch)
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    _write_env_file(tmp_path, "AIHR_DB_PASSWORD=turnkey-pw\n")
    dsn, source = config.db_dsn_with_source()
    assert dsn == "postgresql://aihr:turnkey-pw@localhost:5433/aihr"
    assert "AIHR db env file" in source


def test_db_dsn_env_file_port_override(tmp_path, monkeypatch):
    _clear_dsn_envs(monkeypatch)
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    _write_env_file(
        tmp_path, "AIHR_DB_PASSWORD=turnkey-pw\nAIHR_DB_PORT=5441\n"
    )
    assert config.db_dsn() == "postgresql://aihr:turnkey-pw@localhost:5441/aihr"


def test_db_dsn_env_file_uses_toml_fields_when_present(tmp_path, monkeypatch):
    """Chain contract: fields from hr.toml when present, else aihr defaults;
    the env file supplies the password."""
    _clear_dsn_envs(monkeypatch)
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    (tmp_path / "hr.toml").write_text(
        'db_host = "db.example"\ndb_name = "aihr2"\ndb_user = "ops"\n',
        encoding="utf-8",
    )
    _write_env_file(tmp_path, "AIHR_DB_PASSWORD=env-file-pw\nAIHR_DB_PORT=5442\n")
    assert config.db_dsn() == "postgresql://ops:env-file-pw@db.example:5442/aihr2"


def test_db_dsn_precedence_env_password_beats_env_file(tmp_path, monkeypatch):
    """Chain ordering: HR_DSN > hr.toml+HR_DB_PASSWORD > docker/.env >
    legacy compose — with the lower steps also resolvable, step 2 wins."""
    _clear_dsn_envs(monkeypatch)
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    (tmp_path / "hr.toml").write_text('db_user = "alice"\n', encoding="utf-8")
    _write_env_file(tmp_path, "AIHR_DB_PASSWORD=lower-step-pw\n")
    monkeypatch.setenv("HR_DB_PASSWORD", "higher-step-pw")
    assert config.db_dsn() == "postgresql://alice:higher-step-pw@localhost:5433/aihr"


def test_db_dsn_env_file_data_dir_fallback_discovered(tmp_path, monkeypatch):
    """Read discovery finds <HOME>/.local/share/aihr/db.env when the
    repo-local docker/.env is absent."""
    _clear_dsn_envs(monkeypatch)
    monkeypatch.setenv("HR_HOME", str(tmp_path / "hr"))
    monkeypatch.setenv("HOME", str(tmp_path))
    data = tmp_path / ".local" / "share" / "aihr"
    data.mkdir(parents=True)
    (data / "db.env").write_text(
        "AIHR_DB_PASSWORD=fallback-pw\nAIHR_DB_PORT=5443\n", encoding="utf-8"
    )
    assert config.db_dsn() == "postgresql://aihr:fallback-pw@localhost:5443/aihr"


# ---------------------------------------------------------------------------
# legacy HR_COMPOSE_FILE path (chain step 4) — user/db/password resolution
# ---------------------------------------------------------------------------


def test_db_dsn_compose_fallback_resolves_services_db(tmp_path, monkeypatch):
    """services.db.environment POSTGRES_* (list style) define user/db."""
    _clear_dsn_envs(monkeypatch)
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    (tmp_path / "compose.yml").write_text(
        "services:\n"
        "  db:\n"
        "    environment:\n"
        "      - POSTGRES_USER=dbuser\n"
        "      - POSTGRES_DB=dbnam\n"
        "      - POSTGRES_PASSWORD=dbpw\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HR_COMPOSE_FILE", str(tmp_path / "compose.yml"))
    assert config.db_dsn() == "postgresql://dbuser:dbpw@localhost:5432/dbnam"


def test_db_dsn_compose_fallback_wiki_overlays_db_service(tmp_path, monkeypatch):
    """The consumer block (services.wiki DB_*) overlays services.db POSTGRES_*."""
    _clear_dsn_envs(monkeypatch)
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    (tmp_path / "compose.yml").write_text(
        "services:\n"
        "  db:\n"
        "    environment:\n"
        "      POSTGRES_USER: baseuser\n"
        "      POSTGRES_DB: basenam\n"
        "      POSTGRES_PASSWORD: basepw\n"
        "  wiki:\n"
        "    environment:\n"
        "      DB_USER: overlayuser\n"
        "      DB_NAME: overlaynam\n"
        "      DB_PASS: overlaypw\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HR_COMPOSE_FILE", str(tmp_path / "compose.yml"))
    assert config.db_dsn() == "postgresql://overlayuser:overlaypw@localhost:5432/overlaynam"


def test_db_dsn_compose_env_vars_keep_highest_precedence(tmp_path, monkeypatch):
    """HR_DB_* env overrides beat every compose-declared field."""
    _clear_dsn_envs(monkeypatch)
    monkeypatch.setenv("HR_HOME", str(tmp_path))
    (tmp_path / "compose.yml").write_text(
        "services:\n"
        "  wiki:\n"
        "    environment:\n"
        "      DB_USER: wikijs\n"
        "      DB_PASS: wikipw\n"
        "      DB_NAME: wiki\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HR_COMPOSE_FILE", str(tmp_path / "compose.yml"))
    monkeypatch.setenv("HR_DB_USER", "forced")
    monkeypatch.setenv("HR_DB_NAME", "forced_db")
    monkeypatch.setenv("HR_DB_PORT", "5599")
    assert config.db_dsn() == "postgresql://forced:wikipw@localhost:5599/forced_db"


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


def test_home_dir_redirects_with_home_env(tmp_path, monkeypatch):
    """Hermeticity contract: a HOME env redirect moves home_dir() at CALL time
    (never frozen at import), and opencode_data_dir() follows it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config_resources.home_dir() == tmp_path
    assert config_resources.opencode_data_dir() == (
        tmp_path / ".local" / "share" / "opencode"
    )


def test_get_provider_config_resolves_key_from_auth_v2_only(hr_sandbox):
    """A key placed ONLY in auth-v2.json must resolve through
    get_provider_config (the legacy reader consults auth.json only, so
    this was RED before the auth-v2-first rewire)."""
    sandbox = hr_sandbox
    (sandbox["configs"] / "fleet.yaml").write_text(
        "gateway_urls:\n  provX: https://example.invalid/gw\n",
        encoding="utf-8",
    )
    data_dir = sandbox["home"] / ".local" / "share" / "opencode"
    data_dir.mkdir(parents=True)
    (data_dir / "auth-v2.json").write_text(
        json.dumps({"accounts": {"provX": {"type": "api", "key": " sk-v2-only \n"}}}),
        encoding="utf-8",
    )

    cfg = config.get_provider_config("provX")

    assert cfg.base_url == "https://example.invalid/gw"
    assert cfg.api_key == "sk-v2-only"


def test_oauth_token_present_but_never_an_api_key(hr_sandbox):
    """An auth-v2 oauth-only entry marks the provider PRESENT while
    provider_api_key() stays None and get_provider_config raises a
    ValueError naming BOTH auth files."""
    from hr.opencode_auth import provider_api_key, providers_with_credentials

    sandbox = hr_sandbox
    (sandbox["configs"] / "fleet.yaml").write_text(
        "gateway_urls:\n  provX: https://example.invalid/gw\n",
        encoding="utf-8",
    )
    data_dir = sandbox["home"] / ".local" / "share" / "opencode"
    data_dir.mkdir(parents=True)
    (data_dir / "auth-v2.json").write_text(
        json.dumps({"accounts": {"provX": {"type": "oauth", "token": "tk-oauth"}}}),
        encoding="utf-8",
    )

    assert "provX" in providers_with_credentials()
    assert provider_api_key("provX") is None

    with pytest.raises(ValueError) as exc:
        config.get_provider_config("provX")
    msg = str(exc.value)
    assert "auth-v2.json" in msg
    assert "auth.json" in msg
