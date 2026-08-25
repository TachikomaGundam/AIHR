"""Tests for plugin safety features (transactional apply contract, todo 7).

Contract under test (plan row 7):
- backup manifest records file presence + SHA-256 + snapshot identity
- rollback restores pre-existing files byte-for-byte and DELETES files
  that did not exist before the apply
- corrupt backups (missing/tampered manifest, missing blob, hash mismatch)
  REFUSE restoration
- compatibility compares the declared HR FastDraw schema version against
  fastdraw/package.json — mismatch refuses apply (was: accepts all)
- retention prunes beyond 10 backups / 30 days while always preserving the
  newest valid recovery point
"""

from __future__ import annotations

import json
import os
import site
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from hr.plugin_safety import (
    HR_FASTDRAW_SCHEMA_VERSION,
    MANIFEST_FILENAME,
    PRESETS_FILENAME,
    STATE_FILENAME,
    check_compatibility,
    create_backup,
    list_backups,
    prune_backups,
    rollback,
)


def _write_old_manifest(backup_path: Path, days_old: int) -> None:
    """Backdate a snapshot's manifest created_at so retention sees it as expired."""
    manifest_path = backup_path / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text())
    stamp = datetime.now(timezone.utc) - timedelta(days=days_old)
    manifest["created_at"] = stamp.isoformat()
    manifest_path.write_text(json.dumps(manifest))


class TestCreateBackup:
    def test_create_backup_creates_directory_and_copies_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)

            # Create test files
            presets_file = config_dir / PRESETS_FILENAME
            state_file = config_dir / STATE_FILENAME

            presets_file.write_text('{"presets": {}}')
            state_file.write_text('{"agents": {}}')

            with patch("hr.plugin_safety.get_config_dir", return_value=config_dir):
                backup_path = create_backup("test-backup")

                assert backup_path.exists()
                assert backup_path.name == "test-backup"
                assert (backup_path / PRESETS_FILENAME).exists()
                assert (backup_path / STATE_FILENAME).exists()

    def test_create_backup_writes_manifest_with_presence_hashes_and_snapshot_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / PRESETS_FILENAME).write_text('{"presets": {"a": {}}}')

            with patch("hr.plugin_safety.get_config_dir", return_value=config_dir):
                backup_path = create_backup("manifest-backup")

            manifest = json.loads((backup_path / MANIFEST_FILENAME).read_text())
            # snapshot identity: unique id per backup
            assert isinstance(manifest["snapshot_id"], str) and manifest["snapshot_id"]
            assert isinstance(manifest["created_at"], str)
            # retention policy recorded in the manifest itself (revisable default)
            assert manifest["retention"]["max_backups"] == 10
            assert manifest["retention"]["max_age_days"] == 30
            # per-file presence + SHA-256
            presets_entry = manifest["files"][PRESETS_FILENAME]
            assert presets_entry["existed_before"] is True
            assert presets_entry["sha256"] is not None
            state_entry = manifest["files"][STATE_FILENAME]
            assert state_entry["existed_before"] is False
            assert state_entry["sha256"] is None

    def test_create_backup_with_auto_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)

            presets_file = config_dir / PRESETS_FILENAME
            presets_file.write_text('{"presets": {}}')

            with patch("hr.plugin_safety.get_config_dir", return_value=config_dir):
                backup_path = create_backup()

                assert backup_path.exists()
                assert backup_path.name.startswith("backup-")

    def test_create_backup_refuses_existing_snapshot_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)

            with patch("hr.plugin_safety.get_config_dir", return_value=config_dir):
                create_backup("dup-backup")
                try:
                    create_backup("dup-backup")
                except ValueError as exc:
                    assert "already exists" in str(exc)
                else:
                    raise AssertionError("expected ValueError for duplicate backup name")


class TestListBackups:
    def test_list_backups_returns_sorted_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            backup_dir = config_dir / "hr-apply-backups"
            backup_dir.mkdir()

            # Create multiple backups
            (backup_dir / "backup-001").mkdir()
            (backup_dir / "backup-002").mkdir()
            (backup_dir / "backup-003").mkdir()

            with patch("hr.plugin_safety.get_config_dir", return_value=config_dir):
                backups = list_backups()

                assert len(backups) == 3
                assert backups[0]["name"] == "backup-003"  # Most recent first
                assert backups[1]["name"] == "backup-002"
                assert backups[2]["name"] == "backup-001"
                # manifest-less dirs are not valid recovery points
                assert backups[0]["valid"] is False

    def test_list_backups_returns_empty_when_no_backups(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)

            with patch("hr.plugin_safety.get_config_dir", return_value=config_dir):
                backups = list_backups()

                assert backups == []


class TestRollback:
    def test_rollback_restores_original_bytes_and_deletes_newly_created_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            original_presets = (
                '{"presets": {"old": {"description": "x", "createdAt": "c",'
                ' "agents": {"oracle": "p/m1"}}}}\n'
            )
            (config_dir / PRESETS_FILENAME).write_text(original_presets)

            with patch("hr.plugin_safety.get_config_dir", return_value=config_dir):
                create_backup("test-backup")
                # apply phase: pre-existing file replaced, new file created
                (config_dir / PRESETS_FILENAME).write_text('{"presets": {"new": {}}}')
                (config_dir / STATE_FILENAME).write_text('{"agents": {"x": "p/m"}}')

                result = rollback("test-backup")

                assert result["success"] is True
                assert result["restored_from"] == "test-backup"
                assert result["snapshot_id"] is not None
                # pre-existing file restored byte-for-byte (SHA-256 match)
                rendered = (config_dir / PRESETS_FILENAME).read_text()
                assert rendered == original_presets
                # file that did not exist before the apply was DELETED
                assert not (config_dir / STATE_FILENAME).exists()
                assert STATE_FILENAME in result["deleted"]

    def test_rollback_fails_when_backup_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)

            with patch("hr.plugin_safety.get_config_dir", return_value=config_dir):
                result = rollback("nonexistent-backup")

                assert result["success"] is False
                assert "not found" in result["error"]

    def test_rollback_refuses_corrupt_backup_without_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            backup_dir = config_dir / "hr-apply-backups" / "no-manifest"
            backup_dir.mkdir(parents=True)
            (backup_dir / PRESETS_FILENAME).write_text('{"presets": {"old": {}}}')

            with patch("hr.plugin_safety.get_config_dir", return_value=config_dir):
                result = rollback("no-manifest")

                assert result["success"] is False
                assert "corrupt" in result["error"]
                # nothing was restored and nothing deleted
                assert not (config_dir / PRESETS_FILENAME).exists()

    def test_rollback_refuses_tampered_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / PRESETS_FILENAME).write_text('{"presets": {"a": {}}}')
            (config_dir / STATE_FILENAME).write_text('{"agents": {}}')

            with patch("hr.plugin_safety.get_config_dir", return_value=config_dir):
                backup_path = create_backup("tampered")
                # tamper: manifests claims the state file existed before
                manifest_path = backup_path / MANIFEST_FILENAME
                manifest = json.loads(manifest_path.read_text())
                state_entry = manifest["files"][STATE_FILENAME]
                state_entry["existed_before"] = True
                state_entry["sha256"] = "0" * 64
                manifest_path.write_text(json.dumps(manifest))

                result = rollback("tampered")

                assert result["success"] is False
                assert "corrupt" in result["error"]
                # the pre-apply files are untouched (refusal happened before any write)
                assert (config_dir / PRESETS_FILENAME).exists()
                assert (config_dir / STATE_FILENAME).exists()

    def test_rollback_refuses_missing_blob_and_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / PRESETS_FILENAME).write_text('{"presets": {"a": {}}}')

            with patch("hr.plugin_safety.get_config_dir", return_value=config_dir):
                backup_path = create_backup("blob-missing")
                (backup_path / PRESETS_FILENAME).unlink()
                result = rollback("blob-missing")
                assert result["success"] is False
                assert "blob" in result["error"]

            (config_dir / STATE_FILENAME).write_text('{"agents": {}}')
            with patch("hr.plugin_safety.get_config_dir", return_value=config_dir):
                create_backup("blob-tampered")
                backup_path = config_dir / "hr-apply-backups" / "blob-tampered"
                (backup_path / PRESETS_FILENAME).write_text('{"presets": {"FORGED": {}}}')
                result = rollback("blob-tampered")
                assert result["success"] is False
                assert "hash mismatch" in result["error"]


class TestCompatibility:
    def test_check_compatibility_returns_version_info(self) -> None:
        with patch("hr.__version__", "0.2.0"):
            result = check_compatibility()

            assert "compatible" in result
            assert "hr_version" in result
            assert "plugin_version" in result
            assert "warnings" in result

    def test_compatibility_match_passes_when_plugin_version_equals_declared_schema(self, tmp_path) -> None:
        pkg = tmp_path / "package.json"
        pkg.write_text(json.dumps({"name": "opencode-fastdraw", "version": HR_FASTDRAW_SCHEMA_VERSION}))

        with patch("hr.__version__", "0.2.0"):
            result = check_compatibility(plugin_package_json=pkg)

            assert result["compatible"] is True
            assert result["plugin_version"] == HR_FASTDRAW_SCHEMA_VERSION

    def test_compatibility_mismatch_fails_loudly(self, tmp_path) -> None:
        pkg = tmp_path / "package.json"
        pkg.write_text(json.dumps({"name": "opencode-fastdraw", "version": "9.9.9"}))

        with patch("hr.__version__", "0.2.0"):
            result = check_compatibility(plugin_package_json=pkg)

            assert result["compatible"] is False
            assert any("does not match" in w for w in result["warnings"])

    def test_check_compatibility_handles_missing_package_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = Path(tmpdir) / "opencode_plugin"
            plugin_dir.mkdir()

            with patch("hr.__version__", "0.2.0"):
                with patch("hr.plugin_safety.Path") as mock_path:
                    mock_path.return_value.parent.parent = Path(tmpdir)
                    result = check_compatibility()

                    assert result["plugin_version"] == "unknown"
                    assert result["compatible"] is False  # fail closed: cannot verify
                    assert len(result["warnings"]) > 0


class TestPruneRetention:
    def _fresh_backup(self, config_dir: Path, name: str) -> Path:
        with patch("hr.plugin_safety.get_config_dir", return_value=config_dir):
            return create_backup(name, prune=False)

    def test_prune_keeps_newest_10_and_drops_oldest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            for i in range(12):
                self._fresh_backup(config_dir, f"batch-{i:02d}")

            with patch("hr.plugin_safety.get_config_dir", return_value=config_dir):
                result = prune_backups()

            assert len(result["kept"]) == 10
            assert len(result["removed"]) == 2
            assert "batch-11" in result["kept"]  # newest survives
            assert "batch-00" in result["removed"]  # oldest pruned

    def test_prune_drops_snapshots_older_than_30_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            for i in range(3):
                self._fresh_backup(config_dir, f"age-{i:02d}")
            _write_old_manifest(config_dir / "hr-apply-backups" / "age-00", days_old=40)
            _write_old_manifest(config_dir / "hr-apply-backups" / "age-01", days_old=31)

            with patch("hr.plugin_safety.get_config_dir", return_value=config_dir):
                result = prune_backups()

            assert "age-00" in result["removed"]
            assert "age-01" in result["removed"]
            assert result["kept"] == ["age-02"]

    def test_prune_always_preserves_newest_valid_recovery_point(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            # newest valid snapshot is way older than 30 days
            self._fresh_backup(config_dir, "old-but-valid")
            _write_old_manifest(config_dir / "hr-apply-backups" / "old-but-valid", days_old=45)
            # corrupt (manifest-less) snapshots newer than it
            backup_dir = config_dir / "hr-apply-backups"
            for i in range(3):
                (backup_dir / f"corrupt-{i:02d}").mkdir()

            with patch("hr.plugin_safety.get_config_dir", return_value=config_dir):
                result = prune_backups()

            assert "old-but-valid" in result["kept"]  # newest VALID is sacred
            assert "corrupt-00" in result["removed"]  # corrupt snapshots pruned

    def test_prune_removes_corrupt_snapshots_regardless_of_recency(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            self._fresh_backup(config_dir, "good")
            backup_dir = config_dir / "hr-apply-backups"
            (backup_dir / "zz-corrupt-newest").mkdir()  # newest name, no manifest

            with patch("hr.plugin_safety.get_config_dir", return_value=config_dir):
                result = prune_backups()

            assert "zz-corrupt-newest" in result["removed"]
            assert result["kept"] == ["good"]


class TestPreviewAndSafeApplyFlow:
    """Preview-to-apply binding + transactional auto-restore (unit level)."""

    def _assignments(self) -> list:
        from hr.decision import SeatAssignment

        return [
            SeatAssignment(
                seat_code="oracle",
                gate_level="strict",
                primary="test/model",
                fallbacks=[],
                eliminated=[],
                unassigned=None,
            )
        ]

    def _patch_db(self, monkeypatch) -> MagicMock:
        conn = MagicMock()
        monkeypatch.setattr("hr.db.connect", lambda: conn)
        monkeypatch.setattr(
            "hr.apply.latest_assignments",
            lambda conn, **kw: (self._assignments(), "test-sweep-12345"),
        )
        return conn

    def test_preview_with_no_current_state(self, monkeypatch) -> None:
        from hr.plugin_safety import preview_apply

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            self._patch_db(monkeypatch)

            result = preview_apply(config_dir=config_dir)

            assert "current_preset" in result
            assert "new_preset" in result
            assert "changes" in result
            assert result["sweep_id"] == "test-sweep-12345"
            assert len(result["changes"]) > 0

    def test_safe_apply_dry_run(self, monkeypatch) -> None:
        from hr.plugin_safety import safe_apply

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            self._patch_db(monkeypatch)

            result = safe_apply(dry_run=True, config_dir=config_dir)

            assert result["success"] is True
            assert result["dry_run"] is True
            assert "preview" in result

    def test_preview_then_apply_without_drift_applies(self, monkeypatch) -> None:
        """(a) preview-then-apply with NO drift → applies."""
        from hr.plugin_safety import preview_apply, safe_apply

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            self._patch_db(monkeypatch)

            preview = preview_apply(
                preset_name="p1", include_state=True, record_preview=True, config_dir=config_dir,
            )
            assert "preview_record" in preview

            result = safe_apply(
                preset_name="p1", include_state=True, create_backup_before=True, config_dir=config_dir,
            )

            assert result["success"] is True
            store = json.loads((config_dir / PRESETS_FILENAME).read_text())
            assert "p1" in store["presets"]
            assert (config_dir / STATE_FILENAME).exists()
            # the preview record was consumed by the successful apply
            assert not (config_dir / "hr-apply-backups" / "previews").exists()
            # backup snapshot manifest exists with a snapshot id
            assert result["snapshot_id"]

    def test_preview_drift_rejects_apply_without_writes(self, monkeypatch) -> None:
        """(b) files changed between preview and apply → REJECT, no writes."""
        from hr.plugin_safety import preview_apply, safe_apply

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / PRESETS_FILENAME).write_text('{"presets": {"prod-lock": {}}}')
            self._patch_db(monkeypatch)

            preview_apply(preset_name="p1", record_preview=True, config_dir=config_dir)
            # the file drifts between preview and apply
            (config_dir / PRESETS_FILENAME).write_text('{"presets": {"prod-lock": {}, "intruder": {}}}')

            result = safe_apply(preset_name="p1", config_dir=config_dir)

            assert result["success"] is False
            assert "preview drift" in result["error"]
            # no writes happened: the intruder file is byte-identical
            assert json.loads((config_dir / PRESETS_FILENAME).read_text())["presets"]["intruder"] == {}
            # no backup snapshot exists (rejection happened before any write;
            # only the preview record itself sits under hr-apply-backups/)
            backup_dir = config_dir / "hr-apply-backups"
            snapshots = [p for p in backup_dir.iterdir() if p.is_dir() and p.name != "previews"]
            assert snapshots == []

    def test_safe_apply_auto_restores_when_apply_fails_mid_write(self, monkeypatch) -> None:
        """(c) failure after any write → AUTO-RESTORE byte-for-byte."""
        from hr.plugin_safety import safe_apply

        original_presets = (
            '{"presets": {"prod-lock": {"description": "locked", "createdAt": "x",'
            ' "agents": {"oracle": "p/m1"}}}}\n'
        )

        def _boom_write_state(agents, config_dir):
            (config_dir / STATE_FILENAME).write_text('{"agents": {"partial": "state"}}')
            raise OSError("simulated disk failure on state write")

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / PRESETS_FILENAME).write_text(original_presets)
            self._patch_db(monkeypatch)
            monkeypatch.setattr("hr.apply.write_state", _boom_write_state)

            result = safe_apply(preset_name="p1", include_state=True, config_dir=config_dir)

            assert result["success"] is False
            assert "simulated disk failure" in result["error"]
            # auto-restore happened
            assert result["restore"]["success"] is True
            # pre-existing file restored byte-for-byte
            assert (config_dir / PRESETS_FILENAME).read_text() == original_presets
            # file that did not exist before the apply is gone (not left partial)
            assert not (config_dir / STATE_FILENAME).exists()

    def test_safe_apply_refuses_on_compatibility_mismatch(self, monkeypatch, tmp_path) -> None:
        """(f) declared schema version mismatch → refuses apply."""
        from hr.plugin_safety import safe_apply

        pkg = tmp_path / "fastdraw" / "package.json"
        pkg.parent.mkdir()
        pkg.write_text(json.dumps({"name": "opencode-fastdraw", "version": "9.9.9"}))

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            self._patch_db(monkeypatch)

            result = safe_apply(preset_name="p1", config_dir=config_dir, plugin_package_json=pkg)

            assert result["success"] is False
            assert "Compatibility" in result["error"]
            assert result["compatibility"]["compatible"] is False
            # nothing written, no backup created
            assert not (config_dir / PRESETS_FILENAME).exists()
            assert not (config_dir / "hr-apply-backups").exists()


class TestSubprocessLifecycle:
    """Subprocess-level apply/rollback/prune in a TEMP config dir (never real config).

    The subprocess runs a small scenario script with OPENCODE_CONFIG_DIR pointed
    at a pytest tmp dir; assertions run on the script's JSON stdout.
    """

    def _run_scenario(self, tmp_path: Path, script: str) -> dict:
        repo_root = Path(__file__).resolve().parents[1]
        cfg_dir = tmp_path / "opencode"
        script_path = tmp_path / "scenario.py"
        script_path.write_text(script)
        env = dict(os.environ)
        env["OPENCODE_CONFIG_DIR"] = str(cfg_dir)
        # conftest's session-scoped autouse fixture seals HOME into a tmp dir;
        # the subprocess would lose the user-site (site.USER_SITE is frozen at
        # interpreter startup with the REAL home) — put it back on PYTHONPATH.
        user_site = getattr(site, "USER_SITE", None)
        pythonpath = [str(repo_root)]
        if user_site:
            pythonpath.append(str(user_site))
        env["PYTHONPATH"] = os.pathsep.join(pythonpath)
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        assert proc.returncode == 0, f"scenario failed:\n{proc.stdout}\n{proc.stderr}"
        return json.loads(proc.stdout)

    def test_subprocess_rollback_deletes_newly_created_files_and_restores_bytes(self, tmp_path) -> None:
        script = '''
import json
from pathlib import Path
import os
from hr.plugin_safety import PRESETS_FILENAME, STATE_FILENAME, create_backup, rollback

cfg = Path(os.environ["OPENCODE_CONFIG_DIR"])
cfg.mkdir(parents=True, exist_ok=True)
ORIGINAL = '{"presets": {"old": {"description": "x", "createdAt": "c", "agents": {"oracle": "p/m1"}}}}\\n'
(cfg / PRESETS_FILENAME).write_text(ORIGINAL)
backup = create_backup("subproc-b1")
# apply phase: replace the pre-existing file, create a brand-new one
(cfg / PRESETS_FILENAME).write_text('{"presets": {"new": {}}}')
(cfg / STATE_FILENAME).write_text('{"agents": {"oracle": "p/m1"}}')
r = rollback("subproc-b1")
print(json.dumps({
    "success": r["success"],
    "restored": r.get("restored"),
    "deleted": r.get("deleted"),
    "presets_exists": (cfg / PRESETS_FILENAME).exists(),
    "presets_restored": (cfg / PRESETS_FILENAME).read_text() == ORIGINAL,
    "state_exists": (cfg / STATE_FILENAME).exists(),
}))
'''
        out = self._run_scenario(tmp_path, script)
        assert out["success"] is True
        assert out["restored"] == [PRESETS_FILENAME]
        assert out["deleted"] == [STATE_FILENAME]
        assert out["presets_exists"] is True
        assert out["presets_restored"] is True
        assert out["state_exists"] is False

    def test_subprocess_prune_bounds_retention(self, tmp_path) -> None:
        script = '''
import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from hr.plugin_safety import MANIFEST_FILENAME, create_backup, list_backups, prune_backups

cfg = Path(os.environ["OPENCODE_CONFIG_DIR"])
cfg.mkdir(parents=True, exist_ok=True)
for i in range(12):
    create_backup(f"subproc-{i:02d}", prune=False)
# backdate the two OLDEST manifests by >30 days
for name in ("subproc-00", "subproc-01"):
    mp = cfg / "hr-apply-backups" / name / MANIFEST_FILENAME
    m = json.loads(mp.read_text())
    m["created_at"] = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    mp.write_text(json.dumps(m))
result = prune_backups()
print(json.dumps({
    "kept": sorted(result["kept"]),
    "removed": sorted(result["removed"]),
    "count": len(result["kept"]),
}))
'''
        out = self._run_scenario(tmp_path, script)
        assert out["count"] == 10
        assert "subproc-00" in out["removed"]  # expired by age
        assert "subproc-01" in out["removed"]  # expired by age + beyond budget
        assert "subproc-11" in out["kept"]  # newest survives