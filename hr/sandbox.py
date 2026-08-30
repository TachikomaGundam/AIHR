from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SandboxUnavailableError(RuntimeError):
    executable: str

    def __str__(self) -> str:
        return f"required sandbox executable is unavailable: {self.executable}"


def run_sandboxed(
    workdir: Path,
    python_args: list[str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    bubblewrap = shutil.which("bwrap")
    if bubblewrap is None:
        raise SandboxUnavailableError("bwrap")

    runtime = Path(sys.base_prefix).resolve()
    interpreter = Path(sys.executable).resolve()
    command = [
        bubblewrap,
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--ro-bind",
        str(runtime),
        "/runtime",
        "--ro-bind",
        str(interpreter.parent),
        "/python-bin",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind",
        "/lib",
        "/lib",
        "--ro-bind",
        "/lib64",
        "/lib64",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--bind",
        str(workdir.resolve()),
        "/work",
        "--chdir",
        "/work",
        "--dir",
        "/deps",
        "--setenv",
        "HOME",
        "/nonexistent",
        "--setenv",
        "PATH",
        "/python-bin:/runtime/bin:/usr/bin",
        "--setenv",
        "PYTHONHASHSEED",
        "0",
        "--setenv",
        "PYTHONNOUSERSITE",
        "1",
    ]

    dependency_paths: list[str] = []
    for path_entry in sys.path:
        path = Path(path_entry or ".").resolve()
        if not path.is_dir() or "site-packages" not in path.parts:
            continue
        destination = f"/deps/{len(dependency_paths)}"
        command.extend(["--ro-bind", str(path), destination])
        dependency_paths.append(destination)
    if dependency_paths:
        command.extend(["--setenv", "PYTHONPATH", ":".join(dependency_paths)])

    command.extend([f"/python-bin/{interpreter.name}", *python_args])
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


__all__ = ["SandboxUnavailableError", "run_sandboxed"]
