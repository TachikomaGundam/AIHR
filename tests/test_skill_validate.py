"""``hr skill validate`` contract tests — the fail-closed frontmatter gate.

G1 regression class: a skill without frontmatter (the shipped D1 defect)
must FAIL with the missing fields named, never pass with a warning. Also
proves the migrated authoritative skill in this repository stays valid
(G2 as a standing regression — invariant C).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hr.cli import app
from tests._skill_helpers import make_skill, runner, seal_skill_roots


def test_no_frontmatter_fails_naming_description(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """G1: the OLD authoritative shape (plain Markdown, no frontmatter)."""
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    stale = authority / "hr-workflow.md"
    stale.write_text("# HR Workflow\n\nbody without any frontmatter\n", encoding="utf-8")
    result = runner.invoke(app, ["skill", "validate", str(stale)])
    assert result.exit_code == 1
    assert "FAIL" in result.output
    assert "missing frontmatter field: description" in result.output
    assert "missing frontmatter field: name" in result.output


def test_valid_dir_form_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    skill_md = make_skill(authority, "review-notes")
    result = runner.invoke(app, ["skill", "validate", str(skill_md.parent)])
    assert result.exit_code == 0
    assert "OK" in result.output


def test_skill_md_file_argument_derives_id_from_parent_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: handing validate the SKILL.md path directly must derive the
    ID from the parent directory (engine rule), not from the filename."""
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    skill_md = make_skill(authority, "hr-workflow")
    result = runner.invoke(app, ["skill", "validate", str(skill_md)])
    assert result.exit_code == 0
    assert "(id: hr-workflow)" in result.output


def test_missing_description_only_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    skill = authority / "bare-name"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: bare-name\n---\n\nbody\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["skill", "validate", str(skill / "SKILL.md")])
    assert result.exit_code == 1
    assert "missing frontmatter field: description" in result.output


@pytest.mark.parametrize(
    ("raw", "expect"),
    [
        # YAML folds a value-less or whitespace-only scalar into None -> the
        # validator reports the field as missing; an explicitly empty string
        # hits the non-empty-string defect. Both paths FAIL CLOSED either way.
        ("description:", "missing frontmatter field: description"),
        ("description:    ", "missing frontmatter field: description"),
        ('description: ""', "non-empty string"),
    ],
)
def test_blank_description_fails(
    raw: str, expect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    skill = authority / "blank-desc"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        f"---\nname: blank-desc\n{raw}\n---\n\nbody\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["skill", "validate", str(skill / "SKILL.md")])
    assert result.exit_code == 1
    assert expect in result.output


def test_name_must_match_path_derived_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    make_skill(authority, "real-id", name="other-name")
    result = runner.invoke(app, ["skill", "validate", str(authority / "real-id")])
    assert result.exit_code == 1
    assert "does not match the path-derived skill ID" in result.output


def test_name_must_be_kebab_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    make_skill(authority, "good-dir", name="Not_Kebab")
    result = runner.invoke(app, ["skill", "validate", str(authority / "good-dir")])
    assert result.exit_code == 1
    assert "portable skill ID rule" in result.output


def test_disable_model_invocation_must_be_bool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    skill = authority / "flag-kind"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: flag-kind\ndescription: d\ndisable-model-invocation: notabool\n---\n\nbody\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["skill", "validate", str(skill / "SKILL.md")])
    assert result.exit_code == 1
    assert "disable-model-invocation must be a boolean" in result.output


def test_invalid_yaml_frontmatter_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    skill = authority / "bad-yaml"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: [unclosed\n---\n\nbody\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["skill", "validate", str(skill / "SKILL.md")])
    assert result.exit_code == 1
    assert "not valid YAML" in result.output


def test_unterminated_frontmatter_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    skill = authority / "unterminated"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: unterminated\n\nnever closed\n", encoding="utf-8")
    result = runner.invoke(app, ["skill", "validate", str(skill / "SKILL.md")])
    assert result.exit_code == 1
    assert "never terminated" in result.output


def test_all_validates_every_skill_and_names_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    make_skill(authority, "good-one")
    bad = authority / "bad-two"
    bad.mkdir()
    (bad / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")
    result = runner.invoke(app, ["skill", "validate", "--all"])
    assert result.exit_code == 1
    assert "OK" in result.output and "FAIL" in result.output
    assert "bad-two" in result.output


def test_all_on_missing_root_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HR_SKILL_SOURCE", str(tmp_path / "nowhere"))
    result = runner.invoke(app, ["skill", "validate", "--all"])
    assert result.exit_code == 1
    assert "authoritative skills root not found" in result.stderr


def test_requires_path_or_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seal_skill_roots(monkeypatch, tmp_path)
    missing = runner.invoke(app, ["skill", "validate"])
    assert missing.exit_code == 1
    assert "pass a PATH or --all" in missing.stderr
    both = runner.invoke(app, ["skill", "validate", str(tmp_path), "--all"])
    assert both.exit_code == 1
    assert "not both" in both.stderr


def test_non_markdown_argument_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seal_skill_roots(monkeypatch, tmp_path)
    not_md = tmp_path / "notes.txt"
    not_md.write_text("x\n", encoding="utf-8")
    result = runner.invoke(app, ["skill", "validate", str(not_md)])
    assert result.exit_code == 1
    assert "not a Markdown skill file" in result.stderr


def test_shipped_authority_validates_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """G2 as a standing gate: the repository's authoritative skills surface
    (opencode-config/skills/, directory layout) must ALWAYS validate at
    zero — a re-introduced D1-class defect turns this red."""
    monkeypatch.delenv("HR_SKILL_SOURCE", raising=False)
    monkeypatch.delenv("HR_HOME", raising=False)  # resolve through the checkout
    result = runner.invoke(app, ["skill", "validate", "--all"])
    assert result.exit_code == 0, result.output
    assert "hr-workflow" in result.output
