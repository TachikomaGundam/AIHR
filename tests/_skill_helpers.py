"""Shared helpers for ``tests/test_skill_*.py`` (the ``hr skill`` factory).

Everything derives from ``tmp_path``: the authority, the copy roots, and the
install destinations. The real ``~/.config`` is never a fixture input or
output (conftest seals HOME anyway; the seams below make the isolation
explicit and readable per test).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Optional

from pytest import MonkeyPatch
from typer.testing import CliRunner

runner = CliRunner()

#: minimal frontmatter that passes ``hr skill validate``
VALID_SKILL_MD = (
    "---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n\nBody.\n"
)


def make_skill(
    root: Path,
    skill_id: str,
    *,
    name: Optional[str] = None,
    description: str = "Use when a test needs this skill.",
    extra_files: Optional[dict[str, str]] = None,
) -> Path:
    """Write a valid ``<root>/<skill_id>/SKILL.md`` (V2 directory form)."""
    target_dir = root / skill_id
    target_dir.mkdir(parents=True, exist_ok=True)
    skill_md = target_dir / "SKILL.md"
    skill_md.write_text(
        VALID_SKILL_MD.format(name=name or skill_id, desc=description),
        encoding="utf-8",
    )
    for rel, body in (extra_files or {}).items():
        extra = target_dir / rel
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text(body, encoding="utf-8")
    return skill_md


def seal_skill_roots(monkeypatch: MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """Point the factory at tmp roots: returns (authority, copies).

    ``HR_SKILL_SOURCE`` is the authority seam; ``HR_SKILL_COPIES`` registers
    the controllable copy root for ``doctor``. The engine-facing default
    install dir additionally falls inside the conftest-sealed HOME.
    """
    authority = tmp_path / "authority-skills"
    authority.mkdir()
    copies = tmp_path / "copies"
    monkeypatch.setenv("HR_SKILL_SOURCE", str(authority))
    monkeypatch.setenv("HR_SKILL_COPIES", str(copies))
    return authority, copies


def engine_rows(monkeypatch: MonkeyPatch, rows: list[dict[str, Any]]) -> None:
    """Fake the engine probe: ``engine_skills`` answers with ``rows``."""
    monkeypatch.setattr(
        "hr.cli_skill.engine_skills",
        lambda directory, **_kw: list(rows),
    )


def engine_unreachable(monkeypatch: MonkeyPatch) -> None:
    """Fake a downed engine probe: any call raises RuntimeError."""

    def _boom(_directory, **_kw):
        raise RuntimeError("opencode binary not found on PATH")

    monkeypatch.setattr("hr.cli_skill.engine_skills", _boom)


def snapshot_tree(root: Path) -> list[tuple[str, str, int]]:
    """(relpath, sha256, mtime_ns) per file — byte AND timestamp identity,
    the zero-write proof for install idempotence."""
    if not root.is_dir():
        return []
    return [
        (
            str(p.relative_to(root)),
            hashlib.sha256(p.read_bytes()).hexdigest(),
            p.stat().st_mtime_ns,
        )
        for p in sorted(item for item in root.rglob("*") if item.is_file())
    ]


def fake_subprocess(
    monkeypatch: MonkeyPatch,
    responses: list[Any],
) -> list[list[str]]:
    """Patch ``hr.cli_skill.subprocess.run``; return the argv recorder.

    ``responses`` entries are consumed one per call (the last one repeats);
    an Exception instance is raised, anything else is returned (shape it
    like ``CompletedProcess`` with ``types.SimpleNamespace``).
    """
    calls: list[list[str]] = []

    def _run(argv, **_kwargs):
        calls.append(list(argv))
        response = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr("hr.cli_skill.subprocess.run", _run)
    monkeypatch.setattr("hr.cli_skill.time.sleep", lambda _s: None)
    return calls


def no_npm_runner() -> Callable[..., Any]:
    """Runner stub for ``cli_setup.run_uninstall`` with no subprocess calls."""
    from hr.cli_setup import CommandResult

    def _runner(_argv: list[str], _timeout: int) -> Any:
        return CommandResult(0, "", "")

    return _runner
