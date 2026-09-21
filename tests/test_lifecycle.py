"""Hermetic tests for the turnkey bundle lifecycle (hr.lifecycle / hr.lifecycle_uninstall).

Everything lands in tmp_path: staged fake ``D``, a redirected HOME, and an
OPENCODE_CONFIG_DIR sandbox — no real rc files, no bun caches, no npm, no
server, no docker.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hr import lifecycle, setup_env
from hr.lifecycle import install_post, receipt_path
from hr.lifecycle_uninstall import self_uninstall
from hr.opencfg import strip_jsonc_comments

from tests._bundle_helpers import StubPgRunner, stage_bundle

PRE_EXISTING_RC = "# existing bashrc\n"
PRE_EXISTING_JSONC = (
    '{\n  // keep me\n  "model": "frontier/x",\n  "plugin": ["user-owned@9"] // mine\n}\n'
)


def _sandbox(monkeypatch, tmp_path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bashrc").write_text(PRE_EXISTING_RC, encoding="utf-8")
    config_dir = tmp_path / "opencode"
    config_dir.mkdir()
    (config_dir / "opencode.jsonc").write_text(PRE_EXISTING_JSONC, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(config_dir))
    d = stage_bundle(monkeypatch, tmp_path)
    return d, home, config_dir


def _parse(path: Path) -> dict:
    return json.loads(strip_jsonc_comments(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# install-post
# ---------------------------------------------------------------------------


def test_install_post_requires_staged_bundle(monkeypatch, tmp_path) -> None:
    d, _home, _cfg = _sandbox(monkeypatch, tmp_path)
    (d / "app").rmdir()
    lines: list[str] = []
    assert install_post(say=lines.append) == 1
    assert any("not staged" in line and "app" in line for line in lines)
    assert not (d / "bin").exists()  # nothing placed on a failed precheck


def test_install_post_places_everything_and_records_the_receipt(monkeypatch, tmp_path) -> None:
    d, home, cfg = _sandbox(monkeypatch, tmp_path)
    lines: list[str] = []
    assert install_post(say=lines.append) == 0
    shim = d / "bin" / "hr"
    assert shim.is_symlink() and shim.readlink() == Path("../app/hr")
    assert (d / "db.env").is_file()
    rc_text = (home / ".bashrc").read_text(encoding="utf-8")
    assert rc_text.startswith(PRE_EXISTING_RC) and setup_env.MARK_BEGIN in rc_text
    assert str(d / "bin") in rc_text
    assert _parse(cfg / "opencode.jsonc")["plugin"] == [
        lifecycle.HR_AGENT_PLUGIN, "user-owned@9",
    ]
    assert _parse(cfg / "tui.json")["plugin"] == [lifecycle.FASTDRAW_PLUGIN]
    receipt = json.loads(receipt_path(d).read_text(encoding="utf-8"))
    assert set(receipt) == {"version", "ts", "entries"}
    kinds = [e["kind"] for e in receipt["entries"]]
    assert kinds == ["file", "dir", "file", "path-block", "config-entry", "config-entry"]
    assert receipt["entries"][0]["path"] == str(d / "db.env")
    assert receipt["entries"][-1]["value"] == lifecycle.FASTDRAW_PLUGIN
    assert "prior_sha256" not in receipt["entries"][-1]  # tui.json did not exist yet
    assert receipt["entries"][-2]["prior_sha256"]  # opencode.jsonc did → hash recorded


def test_install_post_is_idempotent(monkeypatch, tmp_path) -> None:
    d, home, cfg = _sandbox(monkeypatch, tmp_path)
    say = (lambda _line: None)
    assert install_post(say=say) == 0
    first = json.loads(receipt_path(d).read_text(encoding="utf-8"))["entries"]
    password_line = next(
        ln for ln in (d / "db.env").read_text(encoding="utf-8").splitlines()
        if ln.startswith("AIHR_DB_PASSWORD=")
    )
    assert install_post(port=5440, say=say) == 0
    assert "AIHR_DB_PORT=5440" in (d / "db.env").read_text(encoding="utf-8")
    assert password_line in (d / "db.env").read_text(encoding="utf-8")  # never regenerated
    second = json.loads(receipt_path(d).read_text(encoding="utf-8"))["entries"]
    assert len(first) == len(second) == 6  # no duplicate receipt entries
    assert _parse(cfg / "opencode.jsonc")["plugin"].count(lifecycle.HR_AGENT_PLUGIN) == 1
    assert (home / ".bashrc").read_text(encoding="utf-8").count(setup_env.MARK_BEGIN) == 1


def test_install_post_port_seed_and_password_source(monkeypatch, tmp_path) -> None:
    from hr import db_admin

    monkeypatch.setattr(db_admin, "generate_db_password", lambda: "fake-gen-pw")
    d, _home, _cfg = _sandbox(monkeypatch, tmp_path)
    assert install_post(port=5441, say=lambda _l: None) == 0
    text = (d / "db.env").read_text(encoding="utf-8")
    assert "fake-gen-pw" in text and "AIHR_DB_PORT=5441" in text  # reuses db_admin's generator
    if os.name != "posix":
        pytest.skip("POSIX file modes")
    assert (d / "db.env").stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# self-uninstall
# ---------------------------------------------------------------------------


def test_self_uninstall_requires_yes(monkeypatch, tmp_path) -> None:
    d, _home, _cfg = _sandbox(monkeypatch, tmp_path)
    install_post(say=lambda _l: None)
    lines: list[str] = []
    assert self_uninstall(yes=False, say=lines.append) == 1
    assert any("--yes" in line for line in lines)
    assert d.exists() and (d / "bin" / "hr").is_symlink()


def test_self_uninstall_without_receipt_refuses_and_prints_manual_steps(monkeypatch, tmp_path) -> None:
    d, _home, cfg = _sandbox(monkeypatch, tmp_path)
    install_post(say=lambda _l: None)
    receipt_path(d).unlink()
    lines: list[str] = []
    assert self_uninstall(yes=True, say=lines.append) == 1
    out = "\n".join(lines)
    assert "refusing to delete anything blindly" in out
    assert "pg_ctl" in out and str(d) in out and "PATH" in out
    assert lifecycle.HR_AGENT_PLUGIN in out  # names both plugin entries to drop by hand
    assert lifecycle.HR_AGENT_PLUGIN in (cfg / "opencode.jsonc").read_text(encoding="utf-8")
    assert (d / "bin" / "hr").is_symlink()  # untouched: the refusal deleted nothing


def test_self_uninstall_reverses_everything_and_reports_residue(monkeypatch, tmp_path) -> None:
    d, home, cfg = _sandbox(monkeypatch, tmp_path)
    install_post(say=lambda _l: None)
    # seed residue the bundle does NOT own: bun plugin caches + a pip console script
    cache = home / ".cache" / "opencode" / "packages"
    (cache / "opencode-hr-agent@0.9.9").mkdir(parents=True)
    (cache / "opencode-fastdraw@1.4.2").mkdir(parents=True)
    user_bin = home / ".local" / "bin"
    user_bin.mkdir(parents=True)
    (user_bin / "hr").write_text("#!/bin/sh\n", encoding="utf-8")
    lines: list[str] = []
    assert self_uninstall(yes=True, say=lines.append) == 0
    out = "\n".join(lines)
    assert not d.exists()  # D deleted last, after the replay
    assert (home / ".bashrc").read_text(encoding="utf-8") == PRE_EXISTING_RC  # PATH block gone
    assert (cfg / "opencode.jsonc").read_text(encoding="utf-8") == PRE_EXISTING_JSONC
    assert not (cfg / "tui.json").exists()  # we created it → it goes
    for needle in ("opencode-hr-agent@0.9.9", "opencode-fastdraw@1.4.2", str(user_bin / "hr")):
        assert needle in out
    if os.name != "posix":
        pytest.skip("POSIX removal commands")
    assert "rm -rf" in out


def test_self_uninstall_stops_a_running_cluster_first(monkeypatch, tmp_path) -> None:
    d, _home, _cfg = _sandbox(monkeypatch, tmp_path)
    install_post(say=lambda _l: None)
    pgdata = d / "data" / "pgdata"
    pgdata.mkdir(parents=True)
    (pgdata / "PG_VERSION").write_text("18\n", encoding="utf-8")
    (pgdata / "postmaster.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    stub = StubPgRunner()
    lines: list[str] = []
    assert self_uninstall(yes=True, pg_run=stub, say=lines.append) == 0
    assert stub.find("pg_ctl")[0][-2:] == ["fast", "stop"]
    assert any("stopping server" in line for line in lines)
    assert not d.exists()


def test_self_uninstall_keeps_user_edited_config_entry(monkeypatch, tmp_path) -> None:
    d, _home, cfg = _sandbox(monkeypatch, tmp_path)
    install_post(say=lambda _l: None)
    # the user rephrased/relocated the entry: exact recorded string is gone
    (cfg / "opencode.jsonc").write_text('{"plugin": ["somethingelse@1"]}\n', encoding="utf-8")
    lines: list[str] = []
    assert self_uninstall(yes=True, say=lines.append) == 0
    assert "not present" in "\n".join(lines)
    assert json.loads((cfg / "opencode.jsonc").read_text(encoding="utf-8"))["plugin"] == [
        "somethingelse@1"
    ]


# ---------------------------------------------------------------------------
# CLI surface +2
# ---------------------------------------------------------------------------


def test_cli_lifecycle_commands_registered_on_app() -> None:
    from typer.main import get_command

    from hr.cli import app

    commands = get_command(app).commands
    assert {"install-post", "self-uninstall"} <= set(commands)


def test_cli_install_post_and_self_uninstall_help_flags() -> None:
    from typer.testing import CliRunner

    from hr.cli import app

    runner = CliRunner()
    assert "--port" in runner.invoke(app, ["install-post", "--help"]).output
    assert "--yes" in runner.invoke(app, ["self-uninstall", "--help"]).output


def test_place_shim_windows_stub_is_byte_exact(monkeypatch, tmp_path) -> None:
    d, _home, _cfg = _sandbox(monkeypatch, tmp_path)
    shim, value, created = lifecycle.place_shim(d, platform="win32")
    assert created and shim.name == "hr.cmd"
    assert shim.read_bytes() == value.encode("utf-8") == lifecycle.SHIM_CMD_TEXT.encode("utf-8")
    again = lifecycle.place_shim(d, platform="win32")
    assert again[2] is False  # idempotent: byte-identical stub not rewritten
