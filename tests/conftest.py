"""Shared pytest config + fixtures.

Staging-workspace mechanism (contract, not convention):

* ``hr_sandbox`` — ONE central staging fixture. Every test that touches the
  filesystem must derive ALL paths from it (or from ``tmp_path`` directly):
  it seals ``HOME``/``OPENCODE_CONFIG_DIR``/``HR_HOME``/``HR_ITEMREPO``/
  ``HR_OUTPUT_DIR`` into a per-test tmp dir and chdirs into an empty project
  dir. A test that needs the real machine config (opencode.jsonc, overlays)
  must opt in EXPLICITLY and be marked/documented — none do today.

* ``_sealed_home`` — session-scoped autouse HOME redirection. ``Path.home()``
  resolves into session tmp for the whole run, so no test can ever touch the
  real ``~/.config``/``~/.local`` by accident, even one that forgets to
  monkeypatch.

* ``pytest_sessionfinish`` cleanliness guard — snapshots
  ``git status --porcelain`` at session start and asserts it is IDENTICAL at
  session end, failing the run with the exact paths any test left behind
  inside the repo. Together with the HOME seal this makes "repo dirty after
  tests" a hard failure instead of a cleanup chore.

  Sanctioned volatility (invisible to the guard because gitignored):
  ``__pycache__/``, ``.pytest_cache/``, ``.local.yaml`` overlays, the package
  ``hr.toml`` runtime copy.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_NAMES = (
    "seats.yaml",
    "fleet.yaml",
    "deployable.yaml",
    "models.yaml",
    "thresholds.yaml",
    "knowledge.yaml",
    "hr.toml.example",
)
_DB_ENVS = ("HR_DSN", "HR_DB_PASSWORD", "HR_DB_USER", "HR_COMPOSE_FILE")

# Coverage artifacts are excluded from the guard's comparison set: pytest-cov
# erases `.coverage` BEFORE pytest_sessionstart's snapshot and writes it back
# BEFORE the after-snapshot, so it is always absent from the before set and
# present in the after one (flipping the guard on every --cov run). `.coverage`
# and the CI `coverage.xml` report are test-infra byproducts, not test leaks.
_COVERAGE_ARTIFACTS = {".coverage", "coverage.xml"}


@pytest.fixture(scope="session", autouse=True)
def _sealed_home(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Redirect HOME into session tmp for the whole suite (restored after)."""
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path_factory.mktemp("sealed-home"))
    yield
    if saved is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = saved


@pytest.fixture
def hr_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Central staging workspace: all envs sealed into ``tmp_path``.

    Returns anchors: ``tmp_path``, ``home``, ``config_dir`` (opencode),
    ``hr_home``, ``configs`` (``hr_home/configs``), ``itemrepo``, ``project``
    (the chdir'd cwd). No tracked config is materialized — tests write their
    own fixtures; use :func:`materialize_templates` when production shapes
    are needed.
    """
    home = tmp_path / "home"
    home.mkdir()
    config_dir = tmp_path / "opencode"
    config_dir.mkdir()
    hr_home = tmp_path / "hr"
    configs = hr_home / "configs"
    configs.mkdir(parents=True)
    itemrepo = hr_home / "itemrepo"
    itemrepo.mkdir()
    project = tmp_path / "proj"
    project.mkdir()

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HR_HOME", str(hr_home))
    monkeypatch.setenv("HR_ITEMREPO", str(itemrepo))
    monkeypatch.setenv("HR_OUTPUT_DIR", str(tmp_path / "out"))
    for var in _DB_ENVS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(project)

    return {
        "tmp_path": tmp_path,
        "home": home,
        "config_dir": config_dir,
        "hr_home": hr_home,
        "configs": configs,
        "itemrepo": itemrepo,
        "project": project,
    }


def materialize_templates(sandbox: dict[str, Path]) -> None:
    """Copy the tracked ``configs/*.yaml`` templates into the sandbox.

    Call from a fixture/test when the production YAML shapes (seats,
    thresholds, knowledge, …) are required; tests that assert on malformed or
    custom configs must write their own files instead.
    """
    repo_configs = _REPO_ROOT / "configs"
    for name in _CONFIG_NAMES:
        src = repo_configs / name
        if src.is_file():
            shutil.copy(src, sandbox["configs"] / name)


def _repo_status() -> str:
    proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    lines = []
    for line in proc.stdout.splitlines():
        path = line[3:].strip() if len(line) > 3 else line
        if path in _COVERAGE_ARTIFACTS:
            continue
        lines.append(line)
    return "\n".join(lines)


_cleanliness_before: str | None = None


def pytest_sessionstart(session: pytest.Session) -> None:
    global _cleanliness_before
    _cleanliness_before = _repo_status()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Cleanliness guard: the repo must be EXACTLY as clean as it started.

    Any file a test created/modified inside the repo (a stray write outside
    the staging workspace) fails the run and names the offending paths.
    """
    after = _repo_status()
    if after == _cleanliness_before:
        return
    lines = [l for l in after.splitlines() if l.strip()]
    print("\n" + "=" * 72, flush=True)
    print("CLEANLINESS GUARD: repository is dirty after the test session", flush=True)
    print("(a test wrote outside the staging workspace — see paths below):", flush=True)
    for line in lines:
        print(f"  {line}", flush=True)
    print("=" * 72, flush=True)
    session.exitstatus = max(int(exitstatus), 1)