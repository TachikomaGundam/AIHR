"""``hr skill doctor`` contract tests — the hash-comparison table between the
single authority and every copy root. G5 pattern: mutate one byte of a copy
→ non-zero; restore → zero. Orphan copies (no authority behind them) are
reported loudly but are not our drift."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hr.cli import app
from tests._skill_helpers import make_skill, runner, seal_skill_roots


def _install_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_id: str = "synced"):
    """Authority + a matching copy under the sealed copy root."""
    authority, copies = seal_skill_roots(monkeypatch, tmp_path)
    skill_md = make_skill(authority, skill_id)
    dest = copies / skill_id
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_bytes(skill_md.read_bytes())
    return authority, copies


def test_in_sync_copies_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_pair(tmp_path, monkeypatch)
    result = runner.invoke(app, ["skill", "doctor"])
    assert result.exit_code == 0
    assert "synced" in result.output
    assert "in sync" in result.output
    assert "no drift" in result.output


def test_one_byte_drift_fails_and_restore_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G5: byte-level mutation of a copy → non-zero; restoring it → zero."""
    authority, copies = _install_pair(tmp_path, monkeypatch)
    victim = copies / "synced" / "SKILL.md"
    original = victim.read_bytes()
    victim.write_bytes(original + b"mutated\n")
    drifted = runner.invoke(app, ["skill", "doctor"])
    assert drifted.exit_code == 1
    assert "DRIFT" in drifted.output
    assert "doctor: FAIL" in drifted.output
    victim.write_bytes(original)
    restored = runner.invoke(app, ["skill", "doctor"])
    assert restored.exit_code == 0
    assert authority.is_dir()  # doctor never touches the authority


def test_drift_detected_through_support_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Directory identity covers support files, not just SKILL.md."""
    authority, copies = seal_skill_roots(monkeypatch, tmp_path)
    make_skill(authority, "withrefs", extra_files={"references/policy.md": "original\n"})
    dest = copies / "withrefs"
    (dest / "references").mkdir(parents=True)
    (dest / "SKILL.md").write_bytes((authority / "withrefs" / "SKILL.md").read_bytes())
    (dest / "references" / "policy.md").write_text("original\n", encoding="utf-8")
    assert runner.invoke(app, ["skill", "doctor"]).exit_code == 0
    (dest / "references" / "policy.md").write_text("rewritten\n", encoding="utf-8")
    assert runner.invoke(app, ["skill", "doctor"]).exit_code == 1


def test_missing_copy_is_not_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    make_skill(authority, "uninstalled-skill")
    result = runner.invoke(app, ["skill", "doctor"])
    assert result.exit_code == 0
    assert "not installed" in result.output


def test_orphan_copy_reported_but_not_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A third-party skill installed beside ours has its own authority
    elsewhere — loud ORPHAN line, exit stays ours-clean."""
    _authority, copies = seal_skill_roots(monkeypatch, tmp_path)
    make_skill(copies, "foreign-guest")
    result = runner.invoke(app, ["skill", "doctor"])
    assert result.exit_code == 0
    assert "ORPHAN" in result.output


def test_cross_layout_copy_matches_by_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flat copy of a directory-form authority (pure file move across tools):
    identical SKILL.md bytes = in sync, no false drift."""
    authority, copies = seal_skill_roots(monkeypatch, tmp_path)
    skill_md = make_skill(authority, "portable")
    flat_root = copies.parent / "flat-copies"
    flat_root.mkdir(exist_ok=True)
    monkeypatch.setenv("HR_SKILL_COPIES", str(flat_root))
    (flat_root / "portable.md").write_bytes(skill_md.read_bytes())
    result = runner.invoke(app, ["skill", "doctor"])
    assert result.exit_code == 0
    assert "in sync" in result.output


def test_extra_copy_root_option(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    skill_md = make_skill(authority, "audited")
    extra = tmp_path / "codex-side" / "skills"
    (extra / "audited").mkdir(parents=True)
    (extra / "audited" / "SKILL.md").write_bytes(skill_md.read_bytes())
    result = runner.invoke(app, ["skill", "doctor", "--copy", str(extra)])
    assert result.exit_code == 0
    assert "in sync" in result.output
    # mutate that root → drift found through --copy
    (extra / "audited" / "SKILL.md").write_text("edited\n", encoding="utf-8")
    assert runner.invoke(app, ["skill", "doctor", "--copy", str(extra)]).exit_code == 1


def test_json_surface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_pair(tmp_path, monkeypatch, "json-doc")
    ok = runner.invoke(app, ["skill", "doctor", "--json"])
    assert ok.exit_code == 0
    payload = json.loads(ok.output)
    assert payload["drift"] is False
    row = next(s for s in payload["skills"] if s["id"] == "json-doc")
    assert row["status"] == "in sync"
    assert row["authority"]["valid"] is True
    assert "SKILL.md" in row["authority"]["content"]  # per-file hashes visible


def test_json_surface_drift_keeps_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, copies = _install_pair(tmp_path, monkeypatch, "json-doc")
    (copies / "json-doc" / "SKILL.md").write_text("drifted\n", encoding="utf-8")
    result = runner.invoke(app, ["skill", "doctor", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["drift"] is True
    row = next(s for s in payload["skills"] if s["id"] == "json-doc")
    assert row["status"].startswith("DRIFT")


def test_invalid_authority_flagged_in_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, copies = seal_skill_roots(monkeypatch, tmp_path)
    bad = authority / "broken"
    bad.mkdir()
    (bad / "SKILL.md").write_text("# frontmatter-free\n", encoding="utf-8")
    (copies / "broken").mkdir(parents=True)
    (copies / "broken" / "SKILL.md").write_bytes((bad / "SKILL.md").read_bytes())
    result = runner.invoke(app, ["skill", "doctor"])
    assert result.exit_code == 0  # content matches; validity is validate's verdict
    assert "INVALID frontmatter" in result.output
