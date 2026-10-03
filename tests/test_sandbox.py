from __future__ import annotations

import subprocess
from pathlib import Path

from hr import sandbox


def test_sandbox_binds_the_actual_python_interpreter(
    monkeypatch, tmp_path: Path
) -> None:
    # Given: the interpreter is outside sys.base_prefix/bin.
    interpreter = tmp_path / "toolcache" / "python"
    interpreter.parent.mkdir()
    interpreter.touch()
    captured: list[str] = []

    def run(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        captured.extend(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(sandbox.sys, "executable", str(interpreter))
    monkeypatch.setattr(sandbox.sys, "base_prefix", str(tmp_path / "runtime"))
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox.subprocess, "run", run)

    # When: a sandbox command is assembled.
    sandbox.run_sandboxed(tmp_path, ["-V"], 1)

    # Then: the executable's real directory is mounted and invoked directly.
    bind_index = captured.index(str(interpreter.parent))
    assert captured[bind_index - 1 : bind_index + 2] == [
        "--ro-bind",
        str(interpreter.parent),
        "/python-bin",
    ]
    assert "/python-bin/python" in captured


def test_sandbox_available_flags_frozen_binary(monkeypatch) -> None:
    from hr import sandbox as sb
    monkeypatch.setattr(sb.sys, "frozen", True, raising=False)
    reason = sb.sandbox_available()
    assert reason is not None and "frozen-binary" in reason
    monkeypatch.delattr(sb.sys, "frozen", raising=False)
    monkeypatch.setattr(sb.shutil, "which", lambda _n: None)
    assert sb.sandbox_available() == "bwrap not installed"


def test_run_sandboxed_raises_with_probe_reason_under_frozen(monkeypatch) -> None:
    from hr import sandbox as sb
    from pathlib import Path
    monkeypatch.setattr(sb.sys, "frozen", True, raising=False)
    try:
        sb.run_sandboxed(Path("/tmp"), ["x.py"], 1)
        raise AssertionError("expected SandboxUnavailableError")
    except sb.SandboxUnavailableError as exc:
        assert "frozen-binary" in str(exc)


def test_code_gen_gates_to_not_applicable_when_sandbox_missing(monkeypatch) -> None:
    """191 lesson: frozen installs scored every code_gen item 0/13 via a dead
    sandbox; the battery must skip honestly instead of poisoning verdicts."""
    from hr.bench.engine_runners import EngineRunnersMixin
    from hr.bench.engine_results import _RunResult

    class _Engine(EngineRunnersMixin):
        _timeout_s = 1

        def _single_call(self, *a, **k):
            raise AssertionError("must not call model when sandbox gate trips")

    monkeypatch.setattr("hr.bench.engine_runners.sandbox_available",
                        lambda: "bwrap not installed")
    res = _Engine()._run_code_gen("m", adapter=None, caps=None)
    assert isinstance(res, _RunResult)
    assert res.outcome.status == "not_applicable"
    assert "bwrap" in res.outcome.raw_output
