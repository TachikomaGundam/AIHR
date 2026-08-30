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
