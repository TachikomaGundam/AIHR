from __future__ import annotations

import os
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


def bundled_python_home() -> Path | None:
    """The turnkey bundle's vendored CPython (``D/py``), or None on the host
    / developer channel.

    Resolution order: ``AIHR_PY_HOME`` env override (tests & power users),
    then the bundle layout next to the frozen binary (``app/hr`` ->
    ``../py/bin/python3``). PBS is relocatable: mounting its prefix and
    executing bin/python3 needs no PYTHONHOME games.
    """
    override = os.environ.get("AIHR_PY_HOME")
    if override:
        home = Path(override)
        return home if (home / "bin" / "python3").exists() else None
    if not getattr(sys, "frozen", False):
        return None
    home = Path(sys.executable).resolve().parent.parent / "py"
    return home if (home / "bin" / "python3").exists() else None


def sandbox_available() -> str | None:
    """None when the code-gen sandbox can run here; otherwise the reason."""
    if shutil.which("bwrap") is None:
        return "bwrap not installed"
    if getattr(sys, "frozen", False) and bundled_python_home() is None:
        return "frozen-binary: bundle ships no vendored interpreter (py/ missing)"
    return None


def run_sandboxed(
    workdir: Path,
    python_args: list[str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    reason = sandbox_available()
    if reason is not None:
        raise SandboxUnavailableError(reason)
    bubblewrap = shutil.which("bwrap")
    if bubblewrap is None:
        raise SandboxUnavailableError("bwrap not installed")

    bundled = bundled_python_home()
    if bundled is not None:
        # PBS locates its stdlib by the EXECUTABLE's path: the whole home
        # must stay intact in one mount (bin/+lib/ siblings), so the
        # interpreter runs as /runtime/bin/python3 — no split /python-bin
        # mount (pilot rc=1 'platform independent libraries' proved it).
        runtime = bundled.resolve()
        interpreter = runtime / "bin" / "python3"
        interp_mount = "/runtime/bin/python3"
    else:
        runtime = Path(sys.base_prefix).resolve()
        interpreter = Path(sys.executable).resolve()
        interp_mount = None
    command = [
        bubblewrap,
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--ro-bind",
        str(runtime),
        "/runtime",
        *(
            []
            if interp_mount
            else ["--ro-bind", str(interpreter.parent), "/python-bin"]
        ),
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
        "/runtime/bin:/usr/bin" if interp_mount else "/python-bin:/runtime/bin:/usr/bin",
        "--setenv",
        "PYTHONHASHSEED",
        "0",
        "--setenv",
        "PYTHONNOUSERSITE",
        "1",
    ]

    dependency_paths: list[str] = []
    # With the vendored interpreter the graded code runs in a clean room:
    # host site-packages (our own libs!) must NOT leak into its sys.path.
    for path_entry in [] if bundled is not None else sys.path:
        path = Path(path_entry or ".").resolve()
        if not path.is_dir() or "site-packages" not in path.parts:
            continue
        destination = f"/deps/{len(dependency_paths)}"
        command.extend(["--ro-bind", str(path), destination])
        dependency_paths.append(destination)
    if dependency_paths:
        command.extend(["--setenv", "PYTHONPATH", ":".join(dependency_paths)])

    command.extend([interp_mount or f"/python-bin/{interpreter.name}", *python_args])
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


__all__ = ["SandboxUnavailableError", "run_sandboxed"]
