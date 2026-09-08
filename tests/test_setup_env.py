"""Unit tests for hr.setup_env + the PATH/uninstall arms of hr.cli_setup.

Every filesystem touch happens under ``tmp_path`` (injected ``home=``);
the Windows registry is a captured fake module — no real winreg, no real
npm, no network, nothing outside the sandbox.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from hr import cli_setup, setup_env
from hr.cli_setup import CommandResult, run_uninstall
from hr.setup_env import MARK_BEGIN, MARK_END

# ---------------------------------------------------------------------------
# POSIX rc blocks
# ---------------------------------------------------------------------------


def _home(tmp_path: Path, rc_names: tuple[str, ...] = ()) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True)
    for name in rc_names:
        (home / name).write_text("# existing\n", encoding="utf-8")
    return home


def test_persist_posix_creates_profile_when_no_rc_exists(tmp_path: Path) -> None:
    home = _home(tmp_path)
    target = tmp_path / "scripts"
    changed, message = setup_env.persist_posix(target, home)
    assert changed and ".profile" in message
    text = (home / ".profile").read_text(encoding="utf-8")
    assert MARK_BEGIN in text and MARK_END in text
    assert f'export PATH="{target}:$PATH"' in text


def test_persist_posix_idempotent_and_appends_only(tmp_path: Path) -> None:
    home = _home(tmp_path, (".bashrc",))
    target = tmp_path / "bin"
    assert setup_env.persist_posix(target, home)[0] is True
    changed, message = setup_env.persist_posix(target, home)
    assert changed is False and "already present" in message
    text = (home / ".bashrc").read_text(encoding="utf-8")
    assert text.startswith("# existing")  # user content untouched
    assert text.count(MARK_BEGIN) == 1


def test_remove_posix_roundtrip_deletes_empty_created_profile(tmp_path: Path) -> None:
    home = _home(tmp_path)
    target = tmp_path / "scripts"
    setup_env.persist_posix(target, home)
    changed, message = setup_env.remove_posix(target, home)
    assert changed and ".profile (empty, deleted)" in message
    assert not (home / ".profile").exists()
    # second removal is a clean no-op (no residue either way)
    assert setup_env.remove_posix(target, home)[0] is False


def test_remove_posix_keeps_user_content_and_nonempty_profile(tmp_path: Path) -> None:
    home = _home(tmp_path, (".zshrc",))
    target = tmp_path / "scripts"
    setup_env.persist_posix(target, home)
    setup_env.remove_posix(target, home)
    assert (home / ".zshrc").read_text(encoding="utf-8") == "# existing\n"
    assert MARK_BEGIN not in (home / ".zshrc").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# PATH inspection helpers
# ---------------------------------------------------------------------------


def test_on_path_case_insensitive_only_on_windows(tmp_path: Path) -> None:
    target = tmp_path / "Scripts"
    env = {"PATH": f"/else;{str(target).upper()}"}
    assert setup_env.on_path(target, environ=env, platform="win32") is True
    assert setup_env.on_path(target, environ=env, platform="linux") is False
    assert setup_env.on_path(target, environ={"PATH": str(target)}, platform="linux") is True


def test_console_script_dir_finds_hr_entry_point(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scripts = tmp_path / "user-scripts"
    scripts.mkdir()
    (scripts / "hr").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(setup_env, "scripts_dir_candidates", lambda platform=None: [scripts])
    assert setup_env.console_script_dir() == scripts


def test_console_script_dir_none_when_nothing_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup_env, "scripts_dir_candidates", lambda platform=None: [tmp_path / "absent"])
    assert setup_env.console_script_dir() is None


# ---------------------------------------------------------------------------
# Windows HKCU\Environment fake
# ---------------------------------------------------------------------------


class FakeReg:
    """Duck-typed winreg module capturing every write."""

    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1
    REG_EXPAND_SZ = 2
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2

    def __init__(self, value: str | None = None, value_type: int = REG_EXPAND_SZ) -> None:
        self.store: dict[str, tuple[str, int]] = {} if value is None else {"Path": (value, value_type)}
        self.writes: list[tuple[str, int, str]] = []
        self.closed = 0

    def OpenKey(self, root: object, sub: str, reserved: int, flags: int) -> str:  # noqa: N802, ARG002
        assert root == self.HKEY_CURRENT_USER and sub == "Environment"
        return "envkey"

    def CloseKey(self, key: str) -> None:  # noqa: N802, ARG002
        self.closed += 1

    def QueryValueEx(self, key: str, name: str) -> tuple[str, int]:  # noqa: N802, ARG002
        if name not in self.store:
            raise FileNotFoundError
        return self.store[name]

    def SetValueEx(self, key: str, name: str, reserved: int, typ: int, value: str) -> None:  # noqa: N802, ARG002
        self.writes.append((name, typ, value))
        self.store[name] = (value, typ)


def test_persist_win_appends_only_target_entry_preserving_type_and_others() -> None:
    reg = FakeReg(value=r"C:\Windows;%SystemRoot%\x;D:\keepme")
    changed, message = setup_env.persist_win(Path(r"C:\py\Scripts"), reg_mod=reg)
    assert changed and "HKCU" in message
    value, typ = reg.store["Path"]
    assert value.split(";") == [r"C:\Windows", r"%SystemRoot%\x", "D:\\keepme", r"C:\py\Scripts"]
    assert typ == FakeReg.REG_EXPAND_SZ  # %VAR% entries survive
    assert reg.writes and reg.closed == 2


def test_persist_win_idempotent_and_creates_missing_value_as_expand_sz() -> None:
    reg = FakeReg(value=r"C:\other;C:\py\Scripts")
    assert setup_env.persist_win(Path(r"c:\py\scripts\\"), reg_mod=reg)[0] is False  # case/trailing-sep tolerant
    fresh = FakeReg(value=None)
    assert setup_env.persist_win(Path(r"C:\py\Scripts"), reg_mod=fresh)[0] is True
    assert fresh.store["Path"] == (r"C:\py\Scripts", FakeReg.REG_EXPAND_SZ)


def test_remove_win_takes_only_our_entry_and_broadcaster_runs() -> None:
    reg = FakeReg(value=r"C:\Windows;C:\py\Scripts;D:\keepme")
    calls: list[int] = []
    changed, message = setup_env.remove_win(Path(r"C:\py\Scripts"), reg_mod=reg, broadcaster=lambda: calls.append(1))
    assert changed and "untouched" in message
    assert reg.store["Path"][0] == r"C:\Windows;D:\keepme"
    assert calls == [1]
    # absent target: no write, no broadcast, no residue
    before = reg.writes.copy()
    assert setup_env.remove_win(Path(r"C:\py\Scripts"), reg_mod=reg, broadcaster=lambda: calls.append(2))[0] is False
    assert reg.writes == before and calls == [1]


def test_windows_scripts_candidates_use_scheme_and_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        setup_env.sysconfig,
        "get_path",
        lambda *a, **kw: str(tmp_path / a[1]) if len(a) > 1 and a[1].endswith("_user") else "",
    )
    cands = setup_env.scripts_dir_candidates(platform="win32")
    assert str(tmp_path / "nt_user") in [str(c) for c in cands]


# ---------------------------------------------------------------------------
# facade dispatch
# ---------------------------------------------------------------------------


def test_facade_dispatches_by_platform() -> None:
    reg = FakeReg(value="C:\\base")
    changed, _ = setup_env.persist_path(Path(r"C:\py\Scripts"), platform="win32", reg_mod=reg)
    assert changed and "Path" in reg.store
    # posix leg covered by the injected-home tests above; win32 leg must
    # never touch rc files even when no home is supplied.


def test_facade_remove_after_persist_win_roundtrip() -> None:
    reg = FakeReg(value="C:\\base")
    setup_env.persist_path(Path(r"C:\py\Scripts"), platform="win32", reg_mod=reg)
    changed, _ = setup_env.remove_path(Path(r"C:\py\Scripts"), platform="win32", reg_mod=reg)
    assert changed
    assert reg.store["Path"][0] == "C:\\base"  # exactly restored — zero residue


# ---------------------------------------------------------------------------
# cli_setup: registrar lookup, guidance, uninstall orchestration
# ---------------------------------------------------------------------------


def test_find_registrar_windows_prefix_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = tmp_path / "npm-global"
    prefix.mkdir()
    (prefix / "opencode-hr.cmd").write_text("@echo off\r\n", encoding="utf-8")
    fake = _Runner({("npm", "prefix", "-g"): CommandResult(0, str(prefix), "")})
    monkeypatch.setattr(cli_setup, "os", types.SimpleNamespace(name="nt"))
    found = cli_setup._find_registrar(fake)
    assert found == str(prefix / "opencode-hr.cmd")


class _Runner:
    def __init__(self, results: dict[tuple[str, ...], CommandResult], default: CommandResult | None = None) -> None:
        self.results = results
        self.default = default or CommandResult(0, "", "")
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: int) -> CommandResult:  # noqa: ARG002
        self.calls.append(list(argv))
        for key, res in self.results.items():
            if tuple(argv)[: len(key)] == key:
                return res
        return self.default


def test_run_uninstall_full_sequence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # registrar + npm via fake runner; caches/config/PATH via env + tmp dirs
    prefix = tmp_path / "npm-global"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "opencode-hr").write_text("#!/bin/sh\n", encoding="utf-8")
    fake = _Runner({("npm", "prefix", "-g"): CommandResult(0, str(prefix), "")})
    monkeypatch.setattr(cli_setup.shutil, "which", lambda name: "/usr/bin/npm" if name == "npm" else None)
    cache = tmp_path / "cache" / "opencode" / "packages"
    ours = cache / "opencode-hr-agent@0.2.1"
    theirs = cache / "some-other-plugin@1.0.0"
    ours.mkdir(parents=True)
    theirs.mkdir(parents=True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = tmp_path / "config" / "opencode"
    (cfg / "skills").mkdir(parents=True)
    (cfg / "agents").mkdir(parents=True)
    skill = cfg / "skills" / "hr-workflow.md"
    agent = cfg / "agents" / "hr.md"
    skill.write_text("x\n", encoding="utf-8")
    agent.write_text("y\n", encoding="utf-8")
    monkeypatch.setattr(cli_setup, "_opencode_config_dir", lambda: cfg)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    monkeypatch.setattr(setup_env, "scripts_dir_candidates", lambda platform=None: [scripts])
    home = _home(tmp_path / "posix", (".bashrc",))
    setup_env.persist_posix(scripts, home)
    monkeypatch.setattr(Path, "home", lambda: home)

    assert run_uninstall(fake) == 0
    out = capsys.readouterr().out
    reg_i = next(i for i, c in enumerate(fake.calls) if c[-1] == "uninstall" and "opencode-hr" in c[0])
    npm_i = next(i for i, c in enumerate(fake.calls) if c[:3] == ["npm", "uninstall", "-g"])
    assert reg_i < npm_i  # configs unregistered BEFORE the registrar disappears
    assert "removed npm globals" in out
    assert not ours.exists() and theirs.exists()  # cache purge is ours-only
    assert not skill.exists() and not agent.exists()
    assert MARK_BEGIN not in (home / ".bashrc").read_text(encoding="utf-8")  # PATH residue gone
    assert "pip uninstall aihr" in out  # engine removal handed to the user


def test_run_uninstall_without_npm_still_purges_and_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake = _Runner({})
    monkeypatch.setattr(cli_setup.shutil, "which", lambda name: None)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    monkeypatch.setattr(setup_env, "scripts_dir_candidates", lambda platform=None: [scripts])
    home = _home(tmp_path, (".profile",))
    setup_env.persist_posix(scripts, home)
    monkeypatch.setattr(Path, "home", lambda: home)
    assert run_uninstall(fake) == 0
    out = capsys.readouterr().out
    assert "SKIP npm steps" in out and "PATH:" in out
    assert MARK_BEGIN not in (home / ".profile").read_text(encoding="utf-8")
    assert fake.calls == []  # nothing subprocess-y attempted without npm


def test_ensure_script_on_path_branches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lines: list[str] = []
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    monkeypatch.setattr(setup_env, "console_script_dir", lambda: scripts)
    # already on PATH → quiet confirmation only
    monkeypatch.setattr(setup_env, "on_path", lambda p, **kw: True)
    cli_setup.ensure_script_on_path(lines.append, persist=True)
    assert lines[-1].startswith("hr is already on PATH")
    # --no-path → guidance, zero writes
    monkeypatch.setattr(setup_env, "on_path", lambda p, **kw: False)
    cli_setup.ensure_script_on_path(lines.append, persist=False)
    assert any("--no-path" in ln or "Add it to PATH yourself" in ln for ln in lines)
    # persist → exactly one persist_path call with the scripts dir
    seen: list[Path] = []
    monkeypatch.setattr(setup_env, "persist_path", lambda target, **kw: (seen.append(target), (True, "ok"))[1])
    cli_setup.ensure_script_on_path(lines.append, persist=True)
    assert seen == [scripts] and "PATH: ok" in lines[-1]


def test_ensure_script_on_path_absent_scripts_dir_is_note_only(monkeypatch: pytest.MonkeyPatch) -> None:
    lines: list[str] = []
    monkeypatch.setattr(setup_env, "console_script_dir", lambda: None)
    cli_setup.ensure_script_on_path(lines.append, persist=True)
    assert len(lines) == 1 and "venv/pipx" in lines[0]
