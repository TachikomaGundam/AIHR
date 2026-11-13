"""``hr skill install`` contract tests — validate-first (an invalid skill is
refused), idempotence with zero writes on the second run, --dry-run writes
nothing, differences fail without --force. Plus the uninstall-symmetry line
of ``hr/cli_setup.py`` (dir form AND legacy flat form both reversed).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from hr import cli_setup
from hr.cli import app
from tests._skill_helpers import make_skill, no_npm_runner, runner, seal_skill_roots, snapshot_tree


def test_install_copies_and_prints_hashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    skill_md = make_skill(authority, "shipped", extra_files={"references/policy.md": "p\n"})
    dest = tmp_path / "engine-skills"
    result = runner.invoke(app, ["skill", "install", "shipped", "--dest", str(dest)])
    assert result.exit_code == 0
    landed = dest / "shipped" / "SKILL.md"
    assert landed.is_file()
    assert (dest / "shipped" / "references" / "policy.md").is_file()  # whole dir moves
    src_hash = hashlib.sha256(skill_md.read_bytes()).hexdigest()
    assert src_hash in result.output
    assert result.output.count("sha256") >= 4  # src + dst receipts


def test_second_install_is_zero_write_zero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G4: run twice; the second run writes NOTHING (bytes AND mtimes) and
    exits 0."""
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    make_skill(authority, "twice", extra_files={"scripts/run.sh": "echo\n"})
    dest = tmp_path / "engine-skills"
    first = runner.invoke(app, ["skill", "install", "twice", "--dest", str(dest)])
    assert first.exit_code == 0
    before = snapshot_tree(dest)
    assert before  # sanity: something landed
    second = runner.invoke(app, ["skill", "install", "twice", "--dest", str(dest)])
    assert second.exit_code == 0
    assert "already installed, identical" in second.output
    assert snapshot_tree(dest) == before  # identical paths, hashes, mtimes


def test_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    make_skill(authority, "planned")
    dest = tmp_path / "engine-skills"
    result = runner.invoke(app, ["skill", "install", "planned", "--dest", str(dest), "--dry-run"])
    assert result.exit_code == 0
    assert "would install" in result.output
    assert snapshot_tree(dest) == []
    assert not dest.exists()  # not even an empty directory


def test_dry_run_on_stale_copy_reports_failure_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulation-faithful dry-run: reports what the real run would do
    (FAIL without --force) while writing nothing."""
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    make_skill(authority, "planned")
    dest = tmp_path / "engine-skills"
    assert runner.invoke(app, ["skill", "install", "planned", "--dest", str(dest)]).exit_code == 0
    stale = dest / "planned" / "SKILL.md"
    stale.write_text("tampered\n", encoding="utf-8")
    before = snapshot_tree(dest)
    result = runner.invoke(app, ["skill", "install", "planned", "--dest", str(dest), "--dry-run"])
    assert result.exit_code == 1
    assert "nothing written" in result.output
    assert "--force" in result.output
    assert snapshot_tree(dest) == before  # dry-run never writes, even a repair


def test_force_also_normalizes_legacy_flat_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    make_skill(authority, "flatty")
    dest = tmp_path / "engine-skills"
    dest.mkdir()
    (dest / "flatty.md").write_text("old flat layout copy\n", encoding="utf-8")
    result = runner.invoke(app, ["skill", "install", "flatty", "--dest", str(dest), "--force"])
    assert result.exit_code == 0
    assert (dest / "flatty" / "SKILL.md").is_file()
    assert not (dest / "flatty.md").exists()


def test_diverged_destination_fails_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    make_skill(authority, "drifted")
    dest = tmp_path / "engine-skills"
    assert runner.invoke(app, ["skill", "install", "drifted", "--dest", str(dest)]).exit_code == 0
    (dest / "drifted" / "SKILL.md").write_text("local edit\n", encoding="utf-8")
    result = runner.invoke(app, ["skill", "install", "drifted", "--dest", str(dest)])
    assert result.exit_code == 1
    assert "differs from the authority" in result.stderr
    assert "--force" in result.stderr


def test_force_replaces_diverged_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    skill_md = make_skill(authority, "forced")
    dest = tmp_path / "engine-skills"
    assert runner.invoke(app, ["skill", "install", "forced", "--dest", str(dest)]).exit_code == 0
    (dest / "forced" / "SKILL.md").write_text("local edit\n", encoding="utf-8")
    result = runner.invoke(
        app, ["skill", "install", "forced", "--dest", str(dest), "--force"]
    )
    assert result.exit_code == 0
    src_hash = hashlib.sha256(skill_md.read_bytes()).hexdigest()
    assert hashlib.sha256((dest / "forced" / "SKILL.md").read_bytes()).hexdigest() == src_hash


def test_invalid_skill_is_refused_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Design requirement 1 on the install path: validate-first, fail-closed."""
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    bad = authority / "no-description"
    bad.mkdir()
    (bad / "SKILL.md").write_text("# heading only, no frontmatter\n", encoding="utf-8")
    dest = tmp_path / "engine-skills"
    result = runner.invoke(app, ["skill", "install", "no-description", "--dest", str(dest)])
    assert result.exit_code == 1
    assert "refusing to install INVALID skill" in result.stderr
    assert "description" in result.stderr
    assert snapshot_tree(dest) == []


def test_unknown_skill_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seal_skill_roots(monkeypatch, tmp_path)
    result = runner.invoke(
        app, ["skill", "install", "ghost", "--dest", str(tmp_path / "dest")]
    )
    assert result.exit_code == 1
    assert "not found in the authoritative root" in result.stderr


def test_legacy_flat_source_installs_as_directory_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex/OpenCode cross-tool portability: install normalizes the legacy
    flat ``<id>.md`` authority into the ``<id>/SKILL.md`` layout unchanged."""
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    flat = authority / "legacy-skill.md"
    flat.write_text(
        "---\nname: legacy-skill\ndescription: d\n---\n\n# legacy-skill\n\nbody\n",
        encoding="utf-8",
    )
    dest = tmp_path / "engine-skills"
    result = runner.invoke(app, ["skill", "install", "legacy-skill", "--dest", str(dest)])
    assert result.exit_code == 0
    landed = dest / "legacy-skill" / "SKILL.md"
    assert landed.is_file()
    assert landed.read_bytes() == flat.read_bytes()


# --- uninstall symmetry (hr/cli_setup.py skill-path lines) ------------------


def _make_cfg(tmp_path: Path) -> Path:
    cfg = tmp_path / "config"
    skill_dir = cfg / "skills" / "hr-workflow"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("installed\n", encoding="utf-8")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "note.md").write_text("n\n", encoding="utf-8")
    (cfg / "skills" / "hr-workflow.md").write_text("legacy flat residue\n", encoding="utf-8")
    (cfg / "agents").mkdir()
    (cfg / "agents" / "hr.md").write_text("agent\n", encoding="utf-8")
    return cfg


def test_uninstall_removes_dir_form_and_legacy_flat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(cli_setup, "_opencode_config_dir", lambda: cfg)
    monkeypatch.setattr(cli_setup.shutil, "which", lambda name: None)  # SKIP npm steps
    monkeypatch.setattr(cli_setup.setup_env, "scripts_dir_candidates", lambda platform=None: [])
    assert cli_setup.run_uninstall(no_npm_runner()) == 0
    out = capsys.readouterr().out
    assert not (cfg / "skills" / "hr-workflow").exists()  # V2 directory form gone
    assert not (cfg / "skills" / "hr-workflow.md").exists()  # legacy flat gone
    assert not (cfg / "agents" / "hr.md").exists()
    assert "DEL" in out


def test_uninstall_tolerates_absent_skill_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setattr(cli_setup, "_opencode_config_dir", lambda: cfg)
    monkeypatch.setattr(cli_setup.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli_setup.setup_env, "scripts_dir_candidates", lambda platform=None: [])
    assert cli_setup.run_uninstall(no_npm_runner()) == 0
    assert "DEL" not in capsys.readouterr().out  # nothing claimed, nothing removed
