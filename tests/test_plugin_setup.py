"""Unit tests for hr.cli_setup — the ``hr setup`` plugin bootstrap.

The subprocess runner is injected everywhere, so nothing touches npm, the
network, or the real user config.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from hr.cli_setup import (
    CommandResult,
    plugin_specs,
    run_setup,
)

_OK = CommandResult(0, "", "")


class FakeRunner:
    def __init__(self, results: dict[tuple[str, ...], CommandResult], default: CommandResult = _OK) -> None:
        self.results = results
        self.default = default
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: int) -> CommandResult:  # noqa: ARG002
        self.calls.append(list(argv))
        for key, res in self.results.items():
            if tuple(argv)[: len(key)] == key:
                return res
        return self.default


def _registrar_runner(tmp_path: Path, extra: dict | None = None) -> FakeRunner:
    bin_dir = tmp_path / "npm-global" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "opencode-hr").write_text("#!/bin/sh\nexit 0\n")
    results: dict = {("npm", "prefix", "-g"): CommandResult(0, str(tmp_path / "npm-global"), "")}
    if extra:
        results.update(extra)
    return FakeRunner(results)


def test_plugin_specs_float_at_latest() -> None:
    assert plugin_specs() == [
        "npm", "install", "-g", "--include-workspace-root", "false",
        "opencode-hr-agent@latest", "opencode-fastdraw@latest",
    ]
    # non-npm package managers must not receive the npm-only flag
    assert plugin_specs("pnpm") == [
        "pnpm", "install", "-g", "opencode-hr-agent@latest", "opencode-fastdraw@latest",
    ]


def test_workspace_flag_survives_real_npm(tmp_path: Path) -> None:
    """Integration guard (regression for the npm-11 no-lockfile workspace
    default): real npm must accept the argv AND must not sweep a root
    package.json in the CWD into a -g install."""
    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("npm not on PATH")
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "hostile-root-probe", "version": "9.9.9"})
    )
    proc = subprocess.run(
        plugin_specs(npm) + ["--dry-run"],
        cwd=tmp_path, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    assert "hostile-root-probe" not in proc.stdout + proc.stderr


def test_happy_path_sequence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/npm" if name == "npm" else None)
    runner = _registrar_runner(tmp_path)
    assert run_setup(runner=runner) == 0
    joined = [" ".join(c) for c in runner.calls]
    assert joined[0] == "npm install -g --include-workspace-root false opencode-hr-agent@latest opencode-fastdraw@latest"
    assert joined[1] == "npm ls -g --depth=0 opencode-hr-agent opencode-fastdraw"
    assert joined[2] == "npm prefix -g"
    assert joined[3].endswith("bin/opencode-hr install")
    assert joined[4].endswith("bin/opencode-hr status")
    out = capsys.readouterr().out
    assert "done" in out and "Restart opencode" in out


def test_resolved_versions_are_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/npm" if name == "npm" else None)
    # npm-ls stdout is parsed for name@ver only; derive the prefix from
    # tmp_path because check (b) of the universality gate bans literal
    # home-directory paths in tracked files.
    npm_ls = CommandResult(
        0,
        f"{tmp_path}/.npm-global\n├── opencode-hr-agent@0.2.3\n└── opencode-fastdraw@1.1.2\n",
        "",
    )
    runner = _registrar_runner(tmp_path, {("npm", "ls"): npm_ls})
    assert run_setup(runner=runner) == 0
    out = capsys.readouterr().out
    assert "resolved: opencode-hr-agent@0.2.3" in out
    assert "resolved: opencode-fastdraw@1.1.2" in out


def test_resolved_version_report_never_fails_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/npm" if name == "npm" else None)
    runner = _registrar_runner(tmp_path, {("npm", "ls"): CommandResult(1, "", "npm ERR!")})
    assert run_setup(runner=runner) == 0  # reporting is informational only


def test_no_npm_skips_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/npm" if name == "npm" else None)
    runner = _registrar_runner(tmp_path)
    assert run_setup(runner=runner, install_npm=False) == 0
    assert not any(c[1] == "install" for c in runner.calls[:1])
    assert all("install -g" not in " ".join(c) for c in runner.calls)


def test_missing_npm_guidance(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    assert run_setup(runner=FakeRunner({})) == 1
    assert "Node.js" in capsys.readouterr().out


def test_eacces_points_at_user_prefix_never_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/npm" if name == "npm" else None)
    runner = _registrar_runner(
        tmp_path,
        {("npm", "install"): CommandResult(1, "", "Error: EACCES: permission denied /usr/lib/node_modules")},
    )
    assert run_setup(runner=runner) == 1
    out = capsys.readouterr().out
    assert "npm config set prefix" in out
    assert "sudo" not in out.lower()


def test_registrar_missing_fallback_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/npm" if name == "npm" else None)
    runner = FakeRunner({("npm", "prefix", "-g"): CommandResult(0, str(tmp_path / "nonexistent"), "")})
    assert run_setup(runner=runner) == 1
    assert "--no-npm" in capsys.readouterr().out


def test_status_miss_propagates_rc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/npm" if name == "npm" else None)
    runner = _registrar_runner(
        tmp_path,
        {(str(tmp_path / "npm-global" / "bin" / "opencode-hr"), "status"): CommandResult(1, "MISS tui.json missing: opencode-fastdraw", "")},
    )
    assert run_setup(runner=runner) == 1


def test_timeout_becomes_clean_rc124_not_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import subprocess

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/npm" if name == "npm" else None)

    class HangRunner(FakeRunner):
        def __call__(self, argv, timeout):
            self.calls.append(list(argv))
            raise subprocess.TimeoutExpired(argv, timeout)

    assert run_setup(runner=HangRunner({})) == 1
    out = capsys.readouterr().out
    assert "timed out" in out and "Traceback" not in out
