"""``hr skill new`` contract tests — the scaffold must be valid the moment
it is born (the template is fail-closed: it cannot produce a skill the
engine would not advertise), and bad or duplicate IDs are refused."""

from __future__ import annotations

from pathlib import Path

import pytest

from hr.cli import app
from hr.cli_skill import check_skill, discover_skills
from tests._skill_helpers import runner, seal_skill_roots


def test_scaffold_is_born_valid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    result = runner.invoke(app, ["skill", "new", "release-notes", "--dir", str(authority)])
    assert result.exit_code == 0
    skill_md = authority / "release-notes" / "SKILL.md"
    assert skill_md.is_file()
    assert "release-notes" in result.output
    skills = discover_skills(authority)
    assert [s.skill_id for s in skills] == ["release-notes"]
    assert check_skill(skills[0]) == []  # template passes validate immediately


def test_scaffold_lands_in_authority_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    result = runner.invoke(app, ["skill", "new", "default-root-skill"])
    assert result.exit_code == 0
    assert (authority / "default-root-skill" / "SKILL.md").is_file()


@pytest.mark.parametrize("bad_id", ["UPPER", "under_score", "-lead", "trail-", "a--b", "x" * 65, ""])
def test_invalid_ids_refused(bad_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    # "--" before the name so dash-leading IDs reach the validator
    # instead of click's option parser.
    result = runner.invoke(app, ["skill", "new", "--dir", str(authority), "--", bad_id])
    assert result.exit_code == 1
    assert "not a valid skill ID" in result.stderr
    assert list(authority.iterdir()) == []  # refusal wrote nothing


def test_existing_directory_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    first = runner.invoke(app, ["skill", "new", "dup", "--dir", str(authority)])
    assert first.exit_code == 0
    second = runner.invoke(app, ["skill", "new", "dup", "--dir", str(authority)])
    assert second.exit_code == 1
    assert "already exists" in second.stderr


def test_existing_flat_legacy_file_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy flat ``<id>.md`` shadows the same path-derived ID — the
    scaffold must not silently create a second definition of it."""
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    (authority / "shadowed.md").write_text("# legacy\n", encoding="utf-8")
    result = runner.invoke(app, ["skill", "new", "shadowed", "--dir", str(authority)])
    assert result.exit_code == 1
    assert "already exists" in result.stderr
    assert not (authority / "shadowed").exists()


def test_scaffold_creates_missing_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seal_skill_roots(monkeypatch, tmp_path)
    fresh = tmp_path / "deep" / "new-root"
    result = runner.invoke(app, ["skill", "new", "first-skill", "--dir", str(fresh)])
    assert result.exit_code == 0
    assert (fresh / "first-skill" / "SKILL.md").is_file()
