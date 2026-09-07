"""Unit tests for hr.cli_setup — the ``hr setup`` plugin bootstrap.

The subprocess runner is injected everywhere, so nothing touches npm, the
network, or the real user config. Pins drift-guard runs against the actual
package.json files in this checkout (CI) — its whole point.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hr.cli_setup import (
    PLUGIN_PINS,
    CommandResult,
    check_pins_against_repo,
    plugin_specs,
    run_setup,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
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


def test_plugin_specs_pinned() -> None:
    assert plugin_specs() == ["opencode-hr-agent@0.2.2", "opencode-fastdraw@1.1.1"]


def test_pins_match_repo_package_jsons() -> None:
    assert check_pins_against_repo(REPO_ROOT) == []


def test_drift_guard_detects_mismatch(tmp_path: Path) -> None:
    pkg = tmp_path / "opencode_plugin"
    pkg.mkdir()
    (pkg / "package.json").write_text('{"version": "9.9.9"}')
    problems = check_pins_against_repo(tmp_path)
    assert problems == [f"opencode-hr-agent: pinned {PLUGIN_PINS['opencode-hr-agent']} != package.json 9.9.9"]


def test_happy_path_sequence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/npm" if name == "npm" else None)
    runner = _registrar_runner(tmp_path)
    assert run_setup(runner=runner) == 0
    joined = [" ".join(c) for c in runner.calls]
    assert joined[0] == "npm install -g opencode-hr-agent@0.2.2 opencode-fastdraw@1.1.1"
    assert joined[1] == "npm prefix -g"
    assert joined[2].endswith("bin/opencode-hr install")
    assert joined[3].endswith("bin/opencode-hr status")
    out = capsys.readouterr().out
    assert "done" in out and "Restart opencode" in out


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
