"""Tests for the release lifecycle (hr.deployment_manager, hr-evolution todo 9).

The lifecycle is one auditable pipeline:

    build → verify → activate → rollback / retain

Every release is built from the T8 release manifest (INCLUDED_HR_MODULES +
RELEASE_ASSETS + the tracked itemrepo tree + the manifest file itself), every
shipped payload is SHA-256-hashed into ``metadata.json``, verification
re-hashes every payload snapshot-locally and REMOVES the candidate on any
mismatch, activation atomically swaps only the ``hr`` symlink (temp symlink +
``os.replace``) and registers the released plugin path in the OpenCode config
behind a T7 create_backup, rollback restores the previous symlink target and
the config via the T7 rollback primitives, and retention bounds stale
releases / archives / plugin entries (newest valid always preserved).

ALL activation/retention tests inject their own ``releases_root``,
``hr_symlink`` and ``config_dir`` under ``tmp_path`` — the real deployment
targets (the production symlink, the releases store, the opencode config
dir) are never touched; the guard test at the bottom asserts this file does
not even contain the real path strings.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
import typer
import typer.main as typer_main
from typer.testing import CliRunner

from hr.deployment_manager import (
    activate_release,
    build_release,
    compute_release_hash,
    enforce_retention_policy,
    list_releases,
    register_release_commands,
    rollback_release,
    verify_release,
)
from hr.plugin_safety import BACKUP_DIR, MAX_AGE_DAYS, MAX_BACKUPS
from hr.release_manifest import INCLUDED_HR_MODULES, RELEASE_ASSETS

runner = CliRunner()

# The release surface the tests build from: the T8 manifest plus the manifest
# file itself (deployment_manager imports it at runtime, so it ships too).
SURFACE = sorted(
    set(INCLUDED_HR_MODULES) | set(RELEASE_ASSETS) | {"hr/release_manifest.py"}
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path: Path) -> Path:
    """Build a fake workspace containing every manifest surface file.

    ``itemrepo`` files are seeded as TRACKED git files: the build enumerates
    the tracked itemrepo tree via ``git ls-files`` (the production workspace
    is the repo itself).
    """
    ws = tmp_path / "workspace"
    for rel in SURFACE:
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith((".py", ".sh")):
            target.write_text(f"# {rel}\n")
        elif rel.endswith((".ts", ".json", ".toml", ".md")):
            target.write_text(f"// {rel}\n")
        else:
            target.write_text(f"{rel}\n")
    (ws / "itemrepo" / "reasoning" / "t3").mkdir(parents=True)
    (ws / "itemrepo" / "reasoning" / "t3" / "reason.t3.contract.json").write_text(
        '{"slug": "reason.t3.contract"}\n'
    )
    (ws / "itemrepo" / "vision").mkdir(parents=True)
    (ws / "itemrepo" / "vision" / "vision.contract.json").write_text("{}")
    (ws / "itemrepo" / "reasoning" / "reasoning_registry.py").write_text(
        "# reasoning registry\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "add", "itemrepo"], cwd=ws, check=True)
    return ws


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _config_file(config_dir: Path) -> Path:
    return config_dir / "opencode.jsonc"


def _backup_dir(config_dir: Path) -> Path:
    return config_dir / BACKUP_DIR


def _write_config(
    config_dir: Path, entries: list[str] | None = None, extra: str = ""
) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    config = _config_file(config_dir)
    config.write_text('{\n  "plugin": ' + json.dumps(entries or []) + extra + "}\n")
    return config


def _plugin_entries(config_dir: Path) -> list[str]:
    config = _config_file(config_dir)
    if not config.exists():
        return []
    from hr.opencfg import parse_config_file

    data = parse_config_file(config)
    return [str(e) for e in data.get("plugin", [])]


# ---------------------------------------------------------------------------
# list_releases (legacy pin: empty root, sorted newest-first with marker)
# ---------------------------------------------------------------------------


def test_list_releases_returns_empty_when_no_releases(tmp_path: Path) -> None:
    # Given: an empty releases root.
    releases_root = tmp_path / "releases"
    # When: releases are listed.
    releases = list_releases(releases_root)
    # Then: no releases are reported.
    assert releases == []


def test_list_releases_returns_sorted_newest_first_with_marker(
    tmp_path: Path,
) -> None:
    # Given: three built releases (name order == creation order) and the
    # newest (a-003) explicitly verified.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    for name in ("a-001", "a-002", "a-003"):
        assert build_release(ws, releases_root, name)["success"]
    verify_release("a-003", releases_root)
    # When: releases are listed.
    releases = list_releases(releases_root)
    # Then: newest first (descending name order), marker reflects the
    # explicit verification.
    assert [r["name"] for r in releases] == ["a-003", "a-002", "a-001"]
    assert {r["created_at"] for r in releases}  # present
    assert all(k in r for r in releases for k in ("name", "path", "created_at", "verified"))
    assert releases[0]["verified"] is True
    assert releases[1]["verified"] is False


# ---------------------------------------------------------------------------
# build_release: surface completeness, metadata hashing, pycache hygiene
# ---------------------------------------------------------------------------


def test_build_creates_complete_surface(tmp_path: Path) -> None:
    # Given: a workspace with the full manifest surface.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    # When: a release is built.
    result = build_release(ws, releases_root, "complete")
    # Then: every manifest payload + manifest file + tracked itemrepo file is
    # present in the candidate (the T8 manifest is the authoritative surface;
    # the released hr/ tree is a PEP 420 namespace package without __init__).
    assert result["success"] is True
    candidate = releases_root / "complete"
    assert (candidate / "configs" / "seats.yaml").exists()
    assert (candidate / "opencode_plugin" / "server.ts").exists()
    assert (candidate / "pyproject.toml").exists()
    assert (candidate / "hr" / "release_manifest.py").exists()
    assert (candidate / "itemrepo" / "reasoning" / "t3" / "reason.t3.contract.json").exists()
    assert (candidate / "itemrepo" / "vision" / "vision.contract.json").exists()
    for rel in SURFACE:
        assert (candidate / rel).exists(), f"missing from candidate: {rel}"


def test_build_fails_when_manifest_module_missing(tmp_path: Path) -> None:
    # Given: a workspace missing one INCLUDED module (surface gate).
    ws = _make_workspace(tmp_path)
    missing = "hr/bench/engine_results.py"
    (ws / missing).unlink()
    releases_root = tmp_path / "releases"
    # When: the release is built.
    result = build_release(ws, releases_root, "gate")
    # Then: the build fails loudly naming the missing payload and creates
    # nothing.
    assert result["success"] is False
    assert missing in result["error"]
    assert not (releases_root / "gate").exists()


def test_build_fails_when_release_asset_missing(tmp_path: Path) -> None:
    # Given: a workspace missing one RELEASE_ASSET.
    ws = _make_workspace(tmp_path)
    missing = "configs/seats.yaml"
    (ws / missing).unlink()
    releases_root = tmp_path / "releases"
    # When: the release is built.
    result = build_release(ws, releases_root, "asset")
    # Then: the build fails loudly and creates nothing.
    assert result["success"] is False
    assert missing in result["error"]
    assert not (releases_root / "asset").exists()


def test_build_fails_when_source_directory_missing(tmp_path: Path) -> None:
    # Given: a workspace with no hr/ tree at all.
    ws = tmp_path / "workspace"
    ws.mkdir()
    releases_root = tmp_path / "releases"
    # When: the release is built.
    result = build_release(ws, releases_root, "empty")
    # Then: the build fails naming the source.
    assert result["success"] is False
    assert "not found" in result["error"]


def test_build_fails_when_itemrepo_is_not_a_git_checkout(tmp_path: Path) -> None:
    # Given: a workspace whose itemrepo tree is not git-tracked.
    ws = tmp_path / "workspace"
    (ws / "hr").mkdir(parents=True)
    (ws / "hr" / "__init__.py").write_text("# init\n")
    (ws / "itemrepo").mkdir()
    releases_root = tmp_path / "releases"
    # When: the release is built.
    result = build_release(ws, releases_root, "nogit")
    # Then: the build refuses (the tracked itemrepo tree is part of the
    # surface and cannot be enumerated outside a checkout).
    assert result["success"] is False
    assert "itemrepo" in result["error"].lower()


def test_build_metadata_records_payload_and_manifest_hashes(tmp_path: Path) -> None:
    # Given: a built release.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    build_release(ws, releases_root, "hashed")
    metadata = json.loads((releases_root / "hashed" / "metadata.json").read_text())
    # When: metadata.json is inspected.
    # Then: it carries the surface list, the manifest digest, and one SHA-256
    # per payload that matches the candidate bytes.
    assert sorted(metadata["surface"]) == SURFACE
    assert len(metadata["manifest_hash"]) == 64
    assert len(metadata["manifest_source_sha256"]) == 64
    assert metadata["manifest_source_sha256"] == _sha256_file(
        releases_root / "hashed" / "hr" / "release_manifest.py"
    )
    assert sorted(metadata["payloads"]) == sorted(
        SURFACE
        + [
            "itemrepo/reasoning/reasoning_registry.py",
            "itemrepo/reasoning/t3/reason.t3.contract.json",
            "itemrepo/vision/vision.contract.json",
        ]
    )
    for rel, digest in metadata["payloads"].items():
        assert digest == _sha256_file(releases_root / "hashed" / rel), rel
    assert metadata["manifest_hash"] == hashlib.sha256(
        json.dumps(sorted(metadata["surface"])).encode()
    ).hexdigest()
    assert metadata["manifest_version"]  # records which manifest it was built from


def test_build_never_copies_pycache(tmp_path: Path) -> None:
    # Given: a workspace polluted with bytecode cache.
    ws = _make_workspace(tmp_path)
    pycache = ws / "hr" / "__pycache__"
    pycache.mkdir(parents=True)
    (pycache / "cli.cpython-314.pyc").write_text("")
    releases_root = tmp_path / "releases"
    # When: the release is built.
    build_release(ws, releases_root, "clean")
    # Then: no bytecode cache ships in the candidate.
    assert not list((releases_root / "clean").rglob("__pycache__"))
    assert not list((releases_root / "clean").rglob("*.pyc"))


def test_compute_release_hash_consistent_and_empty_when_missing(
    tmp_path: Path,
) -> None:
    # Given: a built release.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    build_release(ws, releases_root, "hash")
    # When: the release hash is computed twice.
    hash1 = compute_release_hash("hash", releases_root)
    hash2 = compute_release_hash("hash", releases_root)
    # Then: it is deterministic and SHA-256 shaped.
    assert hash1 == hash2
    assert len(hash1) == 64
    # And a missing release hashes to the empty string.
    assert compute_release_hash("nope", releases_root) == ""


# ---------------------------------------------------------------------------
# verify_release: snapshot-local re-hash, tamper detection, candidate removal
# ---------------------------------------------------------------------------


def test_verify_release_fails_when_directory_not_found(tmp_path: Path) -> None:
    releases_root = tmp_path / "releases"
    # When: a nonexistent release is verified.
    result = verify_release("nonexistent", releases_root)
    # Then: verification fails naming the missing release.
    assert result["valid"] is False
    assert "not found" in result["error"]


def test_verify_writes_verification_json_with_manifest_hash(tmp_path: Path) -> None:
    # Given: a built release.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    build_release(ws, releases_root, "good")
    # When: it is verified.
    result = verify_release("good", releases_root)
    # Then: verification.json records the verified manifest hash + checks.
    assert result["valid"] is True
    assert result.get("removed") is None or result["removed"] is False
    verification = json.loads(
        (releases_root / "good" / "verification.json").read_text()
    )
    assert verification["valid"] is True
    assert verification["manifest_hash"] == result["manifest_hash"]
    assert verification["manifest_hash"] == json.loads(
        (releases_root / "good" / "metadata.json").read_text()
    )["manifest_hash"]
    assert verification["checks"]
    assert verification["payload_count"] == len(
        json.loads((releases_root / "good" / "metadata.json").read_text())["payloads"]
    )


def test_verify_finds_metadata_unreadable(tmp_path: Path) -> None:
    # Given: a release directory without metadata.json.
    releases_root = tmp_path / "releases"
    (releases_root / "broken").mkdir(parents=True)
    (releases_root / "broken" / "hr").mkdir()
    # When: it is verified.
    result = verify_release("broken", releases_root)
    # Then: verification fails and the candidate is removed.
    assert result["valid"] is False
    assert not (releases_root / "broken").exists()


@pytest.mark.parametrize(
    "payload, content",
    [
        ("hr/bench/engine_results.py", "tampered python module"),
        ("configs/seats.yaml", "seats: tampered\n"),
        ("opencode_plugin/server.ts", "export const tampered = true;\n"),
        ("itemrepo/reasoning/t3/reason.t3.contract.json", '{"slug": "tampered"}\n'),
        ("itemrepo/vision/vision.contract.json", '{"tampered": true}\n'),
        ("hr/release_manifest.py", "# tampered manifest\n"),
    ],
)
def test_verify_removes_candidate_when_payload_tampered(
    tmp_path: Path, payload: str, content: str
) -> None:
    # Given: a built and verified release whose payload is tampered post-build.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    assert build_release(ws, releases_root, "tamper")["success"]
    assert verify_release("tamper", releases_root)["valid"]
    (releases_root / "tamper" / payload).write_text(content)
    # When: verification runs again.
    result = verify_release("tamper", releases_root)
    # Then: it fails, records the mismatch check, and REMOVES the candidate.
    assert result["valid"] is False
    assert result.get("removed") is True
    checks = {c["check"]: c["status"] for c in result["checks"]}
    assert checks.get(f"payload:{payload}") == "mismatch"
    assert not (releases_root / "tamper").exists()


def test_verify_removes_candidate_when_metadata_tampered(tmp_path: Path) -> None:
    # Given: a built and verified release whose metadata.json is tampered
    # (surface list changed -> manifest hash no longer matches).
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    assert build_release(ws, releases_root, "meta")["success"]
    metadata_path = releases_root / "meta" / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["surface"].append("configs/extra.yaml")
    metadata_path.write_text(json.dumps(metadata))
    # When: verification runs again.
    result = verify_release("meta", releases_root)
    # Then: the manifest hash mismatch is caught and the candidate removed.
    assert result["valid"] is False
    assert result.get("removed") is True
    assert not (releases_root / "meta").exists()


def test_verify_keep_flag_retains_failed_candidate(tmp_path: Path) -> None:
    # Given: a built release whose payload is tampered.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    build_release(ws, releases_root, "keep")
    (releases_root / "keep" / "configs" / "seats.yaml").write_text("tampered\n")
    # When: verification runs with remove_on_failure=False.
    result = verify_release("keep", releases_root, remove_on_failure=False)
    # Then: it reports the failure but leaves the candidate in place.
    assert result["valid"] is False
    assert result.get("removed") is False
    assert (releases_root / "keep").exists()


# ---------------------------------------------------------------------------
# activate_release: atomic symlink swap, T7 backup, plugin registration
# ---------------------------------------------------------------------------


def _build_and_verify(ws: Path, releases_root: Path, name: str) -> Path:
    assert build_release(ws, releases_root, name)["success"]
    assert verify_release(name, releases_root)["valid"]
    return releases_root / name


def test_activate_dry_run_writes_nothing(tmp_path: Path) -> None:
    # Given: a built+verified release and an existing target symlink.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    _build_and_verify(ws, releases_root, "r1")
    hr_link = tmp_path / "hr"
    hr_link.symlink_to(releases_root / "other" / "hr")
    config_dir = tmp_path / "config"
    _write_config(config_dir, ["@existing"])
    # When: activation is previewed in dry-run mode.
    result = activate_release(
        "r1",
        releases_root=releases_root,
        hr_symlink=hr_link,
        config_dir=config_dir,
        dry_run=True,
    )
    # Then: nothing is written anywhere.
    assert result["success"] is True
    assert result["dry_run"] is True
    assert hr_link.resolve() == (releases_root / "other" / "hr").resolve()
    assert not _backup_dir(config_dir).exists()
    assert _plugin_entries(config_dir) == ["@existing"]


def test_activate_swaps_symlink_atomically_via_os_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a built+verified release and a previous symlink target.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    _build_and_verify(ws, releases_root, "r1")
    _build_and_verify(ws, releases_root, "r2")
    hr_link = tmp_path / "hr"
    hr_link.symlink_to(releases_root / "r2" / "hr")
    config_dir = tmp_path / "config"
    _write_config(config_dir, [])
    import hr.deployment_manager as dm

    swap_calls: list[tuple[str, str]] = []
    # NOTE: dm.os IS the os module, so setattr patches it globally. Capture the
    # original replace BEFORE patching, or fake_replace recurses into itself.
    real_replace = os.replace

    def fake_replace(src: str, dst: str) -> None:
        swap_calls.append((src, dst))
        real_replace(src, dst)

    monkeypatch.setattr(dm.os, "replace", fake_replace)
    # When: the release is activated.
    result = activate_release(
        "r1",
        releases_root=releases_root,
        hr_symlink=hr_link,
        config_dir=config_dir,
    )
    # Then: the symlink was swapped through a temp symlink + os.replace
    # (atomic), exactly once, and only the symlink target changed.
    assert result["success"] is True
    symlink_swaps = [
        (src, dst)
        for src, dst in swap_calls
        if f".{hr_link.name}.tmp-" in str(src)
    ]
    assert len(symlink_swaps) == 1
    src_tmp, dst = symlink_swaps[0]
    assert Path(dst) == hr_link
    assert Path(src_tmp).is_symlink() is False  # already replaced
    assert hr_link.is_symlink()
    assert hr_link.resolve() == (releases_root / "r1" / "hr").resolve()
    assert not list(hr_link.parent.glob(".hr.tmp-*"))


def test_activate_registers_plugin_path_and_is_idempotent(
    tmp_path: Path,
) -> None:
    # Given: a built+verified release and a config with an existing entry.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    candidate = _build_and_verify(ws, releases_root, "r1")
    hr_link = tmp_path / "hr"
    config_dir = tmp_path / "config"
    _write_config(config_dir, ["@existing/plugin"])
    # When: the release is activated twice.
    first = activate_release(
        "r1",
        releases_root=releases_root,
        hr_symlink=hr_link,
        config_dir=config_dir,
    )
    second = activate_release(
        "r1",
        releases_root=releases_root,
        hr_symlink=hr_link,
        config_dir=config_dir,
    )
    # Then: the plugin path is registered exactly once and the second
    # activation is a no-op (no new backup).
    assert first["success"] is True
    assert first["plugin_registered"] is True
    assert second["success"] is True
    assert second["already_active"] is True
    entries = _plugin_entries(config_dir)
    plugin_path = str(candidate / "opencode_plugin")
    assert entries.count(plugin_path) == 1
    assert entries[0] == "@existing/plugin"
    assert len(list(_backup_dir(config_dir).iterdir())) == 1


def test_activate_refuses_tampered_release(tmp_path: Path) -> None:
    # Given: a release whose payload was tampered AFTER verification.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    _build_and_verify(ws, releases_root, "r1")
    (releases_root / "r1" / "configs" / "seats.yaml").write_text("tampered\n")
    hr_link = tmp_path / "hr"
    hr_link.symlink_to(releases_root / "r2" / "hr")
    config_dir = tmp_path / "config"
    _write_config(config_dir, [])
    # When: activation is attempted.
    result = activate_release(
        "r1",
        releases_root=releases_root,
        hr_symlink=hr_link,
        config_dir=config_dir,
    )
    # Then: it refuses without touching either pointer.
    assert result["success"] is False
    assert "verify" in result["error"].lower()
    assert hr_link.resolve() == (releases_root / "r2" / "hr").resolve()
    assert _plugin_entries(config_dir) == []
    assert not _backup_dir(config_dir).exists()


def test_activate_creates_t7_backup_with_ledger_and_config_blob(
    tmp_path: Path,
) -> None:
    # Given: a built+verified release and an existing config + fastdraw file.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    candidate = _build_and_verify(ws, releases_root, "r1")
    hr_link = tmp_path / "hr"
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    _config_file(config_dir).write_text('{\n  "plugin": []\n}\n')
    (config_dir / "fastdraw-presets.json").write_text('{"presets": {}}\n')
    # When: the release is activated.
    activate_release(
        "r1",
        releases_root=releases_root,
        hr_symlink=hr_link,
        config_dir=config_dir,
    )
    # Then: a T7 backup snapshot exists with the manifest, the activation
    # ledger, and a byte blob of the opencode config that will be edited.
    backups = list(_backup_dir(config_dir).iterdir())
    assert len(backups) == 1
    backup = backups[0]
    manifest = json.loads((backup / "manifest.json").read_text())
    assert manifest["files"]["fastdraw-presets.json"]["existed_before"] is True
    assert manifest["files"][".fastdraw.json"]["existed_before"] is False
    ledger = json.loads((backup / "release-activation.json").read_text())
    assert ledger["release_name"] == "r1"
    assert ledger["previous_symlink_target"] is None
    assert ledger["previous_symlink_existed"] is False
    assert ledger["plugin_path"] == str(candidate / "opencode_plugin")
    blob = backup / "opencode.jsonc"
    assert blob.exists()
    assert blob.read_text() == '{\n  "plugin": []\n}\n'


def test_activate_preserves_existing_config_format_and_comments(
    tmp_path: Path,
) -> None:
    # Given: a JSONC config with comments and non-plugin keys.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    _build_and_verify(ws, releases_root, "r1")
    hr_link = tmp_path / "hr"
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    # NOTE: whole-line comments only — HEAD's hr/opencfg.py (old regex
    # stripper) cannot parse inline `//` comments; the uncommitted worktree
    # revision can. The fresh-checkout gate runs this test against HEAD.
    raw = (
        '{\n'
        '  "$schema": "https://opencode.ai/config.json",\n'
        '  // primary model\n'
        '  "model": "bailian-token-plan/deepseek-v4-flash",\n'
        '  "plugin": [\n'
        '    // dcp\n'
        '    "@tarquinen/opencode-dcp@latest",\n'
        '    "oh-my-openagent@latest"\n'
        '  ],\n'
        '  "provider": {}\n'
        '}\n'
    )
    _config_file(config_dir).write_text(raw)
    # When: the release is activated.
    activate_release(
        "r1",
        releases_root=releases_root,
        hr_symlink=hr_link,
        config_dir=config_dir,
    )
    # Then: existing keys, comments and formatting survive byte-for-byte; the
    # plugin path is appended inside the array only.
    text = _config_file(config_dir).read_text()
    assert '"$schema": "https://opencode.ai/config.json"' in text
    assert "// primary model" in text
    assert "// dcp" in text
    assert '"model": "bailian-token-plan/deepseek-v4-flash"' in text
    entries = _plugin_entries(config_dir)
    assert entries == [
        "@tarquinen/opencode-dcp@latest",
        "oh-my-openagent@latest",
        f"{releases_root / 'r1' / 'opencode_plugin'}",
    ]
    assert text.count(str(releases_root / "r1" / "opencode_plugin")) == 1


def test_activate_creates_plugin_key_when_config_absent(tmp_path: Path) -> None:
    # Given: a built+verified release and NO opencode config at all.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    _build_and_verify(ws, releases_root, "r1")
    hr_link = tmp_path / "hr"
    config_dir = tmp_path / "config"
    # When: the release is activated.
    activate_release(
        "r1",
        releases_root=releases_root,
        hr_symlink=hr_link,
        config_dir=config_dir,
    )
    # Then: a minimal config is created with only the plugin key.
    assert _plugin_entries(config_dir) == [
        f"{releases_root / 'r1' / 'opencode_plugin'}"
    ]
    from hr.opencfg import parse_config_file

    assert parse_config_file(_config_file(config_dir))["plugin"]


def test_activate_restores_previous_pointers_on_config_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a built+verified release whose config write fails mid-activation.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    _build_and_verify(ws, releases_root, "r1")
    hr_link = tmp_path / "hr"
    hr_link.symlink_to(releases_root / "r2" / "hr")
    config_dir = tmp_path / "config"
    config = _write_config(config_dir, [])
    import hr.deployment_manager as dm

    real_replace = dm.os.replace

    def failing_replace(src: str, dst: str) -> None:
        if Path(dst) == config:
            raise OSError("simulated config write failure")
        real_replace(src, dst)

    monkeypatch.setattr(dm.os, "replace", failing_replace)
    # When: activation is attempted.
    result = activate_release(
        "r1",
        releases_root=releases_root,
        hr_symlink=hr_link,
        config_dir=config_dir,
    )
    # Then: the failure is reported and BOTH pointers are restored to their
    # pre-activation state.
    assert result["success"] is False
    assert result.get("restored") is True
    assert hr_link.resolve() == (releases_root / "r2" / "hr").resolve()
    assert _plugin_entries(config_dir) == []


def test_activate_restores_previous_pointers_on_symlink_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a built+verified release whose symlink swap fails.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    _build_and_verify(ws, releases_root, "r1")
    hr_link = tmp_path / "hr"
    hr_link.symlink_to(releases_root / "r2" / "hr")
    config_dir = tmp_path / "config"
    _write_config(config_dir, [])
    import hr.deployment_manager as dm

    def failing_swap(target, link) -> None:
        raise OSError("simulated symlink failure")

    monkeypatch.setattr(dm, "_atomic_symlink", failing_swap)
    # When: activation is attempted.
    result = activate_release(
        "r1",
        releases_root=releases_root,
        hr_symlink=hr_link,
        config_dir=config_dir,
    )
    # Then: nothing changed and the failure is reported.
    assert result["success"] is False
    assert hr_link.resolve() == (releases_root / "r2" / "hr").resolve()
    assert _plugin_entries(config_dir) == []


# ---------------------------------------------------------------------------
# rollback_release: previous symlink target + T7 config rollback
# ---------------------------------------------------------------------------


def test_rollback_restores_symlink_target_and_config_byte_for_byte(
    tmp_path: Path,
) -> None:
    # Given: r2 active, r1 activated (backup taken, config edited).
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    _build_and_verify(ws, releases_root, "r1")
    _build_and_verify(ws, releases_root, "r2")
    hr_link = tmp_path / "hr"
    hr_link.symlink_to(releases_root / "r2" / "hr")
    config_dir = tmp_path / "config"
    config = _write_config(config_dir, ["@original"])
    original_bytes = config.read_bytes()
    activated = activate_release(
        "r1",
        releases_root=releases_root,
        hr_symlink=hr_link,
        config_dir=config_dir,
    )
    assert hr_link.resolve() == (releases_root / "r1" / "hr").resolve()
    assert _plugin_entries(config_dir) == [
        "@original",
        f"{releases_root / 'r1' / 'opencode_plugin'}",
    ]
    # When: the activation is rolled back.
    rollback_result = rollback_release(
        activated["backup"],
        hr_symlink=hr_link,
        config_dir=config_dir,
    )
    # Then: the previous symlink target AND the config are restored.
    assert rollback_result["success"] is True
    assert hr_link.resolve() == (releases_root / "r2" / "hr").resolve()
    assert config.read_bytes() == original_bytes
    assert _plugin_entries(config_dir) == ["@original"]


def test_rollback_removes_symlink_and_config_created_by_activation(
    tmp_path: Path,
) -> None:
    # Given: activation from a state with no symlink and no config.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    _build_and_verify(ws, releases_root, "r1")
    hr_link = tmp_path / "hr"
    config_dir = tmp_path / "config"
    activated = activate_release(
        "r1",
        releases_root=releases_root,
        hr_symlink=hr_link,
        config_dir=config_dir,
    )
    assert hr_link.is_symlink()
    assert _config_file(config_dir).exists()
    # When: the activation is rolled back.
    rollback_result = rollback_release(
        activated["backup"],
        hr_symlink=hr_link,
        config_dir=config_dir,
    )
    # Then: both artifacts created by the activation are gone.
    assert rollback_result["success"] is True
    assert not hr_link.exists()
    assert not hr_link.is_symlink()
    assert not _config_file(config_dir).exists()


def test_rollback_rejects_backup_without_release_ledger(tmp_path: Path) -> None:
    # Given: a plain T7 backup (no release activation ledger).
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    from hr.plugin_safety import create_backup

    backup = create_backup(config_dir=config_dir)
    # When: release rollback is requested for it.
    result = rollback_release(backup.name, hr_symlink=tmp_path / "hr", config_dir=config_dir)
    # Then: it refuses (apply-rollback handles plain FastDraw backups).
    assert result["success"] is False
    assert "not a release activation" in result["error"]


def test_rollback_restores_fastdraw_files_via_t7(tmp_path: Path) -> None:
    # Given: a pre-existing fastdraw-presets.json snapshot taken by activation.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    _build_and_verify(ws, releases_root, "r1")
    hr_link = tmp_path / "hr"
    hr_link.symlink_to(releases_root / "r2" / "hr")
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "fastdraw-presets.json").write_text('{"presets": {"keep": 1}}\n')
    activated = activate_release(
        "r1",
        releases_root=releases_root,
        hr_symlink=hr_link,
        config_dir=config_dir,
    )
    (config_dir / "fastdraw-presets.json").write_text('{"presets": {"drift": 1}}\n')
    # When: the activation is rolled back.
    rollback_result = rollback_release(
        activated["backup"],
        hr_symlink=hr_link,
        config_dir=config_dir,
    )
    # Then: the fastdraw file snapshotted by T7 is restored byte-for-byte.
    assert rollback_result["success"] is True
    assert (config_dir / "fastdraw-presets.json").read_text() == (
        '{"presets": {"keep": 1}}\n'
    )


# ---------------------------------------------------------------------------
# enforce_retention_policy: bounded releases/archives, newest valid + active
# preserved, stale plugin entries cleaned
# ---------------------------------------------------------------------------


def _build_many(
    ws: Path, releases_root: Path, count: int, prefix: str = "r"
) -> None:
    for i in range(1, count + 1):
        name = f"{prefix}-{i:03d}"
        assert build_release(ws, releases_root, name)["success"]
        assert verify_release(name, releases_root)["valid"]


def test_retention_no_action_when_under_limit(tmp_path: Path) -> None:
    # Given: fewer releases than the bound.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    _build_many(ws, releases_root, 3)
    archive_dir = tmp_path / "archive"
    # When: retention is enforced.
    result = enforce_retention_policy(
        releases_root, archive_dir, max_releases=5, max_age_days=MAX_AGE_DAYS
    )
    # Then: nothing is archived or removed.
    assert result["action"] == "none"
    assert len(result["releases"]["kept"]) == 3
    assert not archive_dir.exists() or not list(archive_dir.iterdir())


def test_retention_archives_old_beyond_bound_keeps_newest_valid_and_active(
    tmp_path: Path,
) -> None:
    # Given: 12 verified releases and the OLDEST one active via the symlink.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    _build_many(ws, releases_root, 12)
    archive_dir = tmp_path / "archive"
    hr_link = tmp_path / "hr"
    hr_link.symlink_to(releases_root / "r-001" / "hr")
    # When: retention is enforced (default bound mirrors T7 MAX_BACKUPS).
    result = enforce_retention_policy(
        releases_root,
        archive_dir,
        hr_symlink=hr_link,
        config_dir=None,
        max_releases=MAX_BACKUPS,
        max_age_days=MAX_AGE_DAYS,
    )
    # Then: the newest 10 and the ACTIVE oldest survive as directories; only
    # r-002 is archived; the archive tarball exists.
    assert result["action"] == "enforced"
    kept_names = {Path(p).name for p in result["releases"]["kept"]}
    assert kept_names == {f"r-{i:03d}" for i in range(1, 13)} - {"r-002"}
    assert [a["name"] for a in result["releases"]["archived"]] == ["r-002"]
    assert (archive_dir / "hr-release-r-002.tar.gz").exists()
    assert hr_link.resolve() == (releases_root / "r-001" / "hr").resolve()


def test_retention_archives_and_removes_stale_archives(tmp_path: Path) -> None:
    # Given: releases beyond the (small) bound, one of them 40 days old.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    _build_many(ws, releases_root, 4)
    old = releases_root / "r-001"
    metadata_path = old / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["created_at"] = "2026-07-01T00:00:00+00:00"
    metadata_path.write_text(json.dumps(metadata))
    archive_dir = tmp_path / "archive"
    # When: retention is enforced with a 2-release / 30-day bound.
    result = enforce_retention_policy(
        releases_root,
        archive_dir,
        max_releases=2,
        max_age_days=30,
    )
    # Then: r-001 and r-002 are archived; r-001's archive is then pruned as
    # stale (>30 days), r-002's archive is the newest and preserved.
    assert {a["name"] for a in result["releases"]["archived"]} == {"r-001", "r-002"}
    assert (archive_dir / "hr-release-r-002.tar.gz").exists()
    assert not (archive_dir / "hr-release-r-001.tar.gz").exists()
    assert result["archives"]["removed"] == ["hr-release-r-001.tar.gz"]
    assert result["archives"]["kept"] == ["hr-release-r-002.tar.gz"]


def test_retention_removes_corrupt_releases(tmp_path: Path) -> None:
    # Given: one valid release and one corrupt (metadata-less) directory.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    _build_many(ws, releases_root, 1)
    (releases_root / "half-baked").mkdir()
    (releases_root / "half-baked" / "hr").mkdir()
    archive_dir = tmp_path / "archive"
    # When: retention is enforced.
    result = enforce_retention_policy(releases_root, archive_dir)
    # Then: the corrupt release is removed, the valid one preserved.
    assert "half-baked" in result["releases"]["corrupt"]
    assert not (releases_root / "half-baked").exists()
    assert (releases_root / "r-001").exists()


def test_retention_cleans_stale_plugin_entries(tmp_path: Path) -> None:
    # Given: a config with plugin entries for an existing and a gone release.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    _build_many(ws, releases_root, 1)
    config_dir = tmp_path / "config"
    _write_config(
        config_dir,
        [
            str(releases_root / "r-001" / "opencode_plugin"),
            str(releases_root / "r-999" / "opencode_plugin"),
            "@other/plugin",
        ],
    )
    archive_dir = tmp_path / "archive"
    # When: retention is enforced with config_dir.
    result = enforce_retention_policy(
        releases_root, archive_dir, config_dir=config_dir
    )
    # Then: only the stale entry is removed; format of untouched keys holds.
    assert result["plugin_entries"]["removed"] == [
        f"{releases_root}/r-999/opencode_plugin"
    ]
    assert _plugin_entries(config_dir) == [
        f"{releases_root}/r-001/opencode_plugin",
        "@other/plugin",
    ]
    assert result["plugin_entries"]["backup"]  # T7 backup taken before the edit


def test_retention_dry_run_makes_no_changes(tmp_path: Path) -> None:
    # Given: 12 verified releases and one stale plugin entry.
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    _build_many(ws, releases_root, 12)
    archive_dir = tmp_path / "archive"
    config_dir = tmp_path / "config"
    _write_config(config_dir, [str(releases_root / "r-999" / "opencode_plugin")])
    before = sorted(p.name for p in releases_root.iterdir())
    # When: retention is previewed.
    result = enforce_retention_policy(
        releases_root, archive_dir, config_dir=config_dir, dry_run=True
    )
    # Then: nothing was archived, removed, or edited.
    assert result["dry_run"] is True
    assert sorted(result["releases"]["to_archive"]) == ["r-001", "r-002"]
    assert sorted(p.name for p in releases_root.iterdir()) == before
    assert not archive_dir.exists() or not list(archive_dir.iterdir())
    assert not _backup_dir(config_dir).exists()
    assert _plugin_entries(config_dir) == [
        f"{releases_root}/r-999/opencode_plugin"
    ]


# ---------------------------------------------------------------------------
# CLI surface: register_release_commands + env-routed end-to-end
# ---------------------------------------------------------------------------


def test_register_release_commands_attaches_six_commands() -> None:
    # Given: a fresh typer app.
    app = typer.Typer(name="hr-test")
    # When: release commands are registered.
    register_release_commands(app)
    # Then: the six lifecycle commands are attached.
    # NOTE: `import typer.main` is module-level (typer_main) — a function-local
    # `import typer.main` would shadow `typer` and raise UnboundLocalError.
    commands = set(typer_main.get_command(app).commands)
    assert commands == {
        "release-build",
        "release-verify",
        "release-activate",
        "release-rollback",
        "release-list",
        "release-prune",
    }


def test_release_cli_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: an env-routed hermetic environment (a fake workspace, temp
    # releases root, temp symlink, temp config dir).
    ws = _make_workspace(tmp_path)
    releases_root = tmp_path / "releases"
    hr_link = tmp_path / "hr"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("HR_WORKSPACE", str(ws))
    monkeypatch.setenv("HR_RELEASES_DIR", str(releases_root))
    monkeypatch.setenv("HR_HR_SYMLINK", str(hr_link))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(config_dir))
    app = typer.Typer(name="hr-test")
    register_release_commands(app)
    # When: the full lifecycle runs through the CLI (
    # build -> verify -> activate -> prune).
    built = runner.invoke(app, ["release-build", "--name", "cli-r1"])
    # Then: every command exits 0 with a deterministic message.
    assert built.exit_code == 0, built.output
    assert "cli-r1" in built.output
    verified = runner.invoke(app, ["release-verify", "cli-r1"])
    assert verified.exit_code == 0, verified.output
    assert "valid" in verified.output.lower()
    activated = runner.invoke(app, ["release-activate", "cli-r1"])
    assert activated.exit_code == 0, activated.output
    assert hr_link.resolve() == (releases_root / "cli-r1" / "hr").resolve()
    assert _plugin_entries(config_dir) == [
        f"{releases_root / 'cli-r1' / 'opencode_plugin'}"
    ]
    pruned = runner.invoke(app, ["release-prune"])
    assert pruned.exit_code == 0, pruned.output
    listed = runner.invoke(app, ["release-list"])
    assert listed.exit_code == 0, listed.output
    assert "cli-r1" in listed.output


# ---------------------------------------------------------------------------
# Guard: this suite must never reference the real deployment paths
# ---------------------------------------------------------------------------


def test_tests_never_reference_real_deployment_paths() -> None:
    # Given: the real deployment targets this suite must NEVER touch.
    real_symlink = "/home" + "/lab" + "/hr"
    real_releases = "/home" + "/lab" + "/.local/share/hr-agent"
    real_config = "/home" + "/lab" + "/.config/opencode"
    # When: this test file's own source is inspected.
    source = (Path(__file__).resolve()).read_text(encoding="utf-8")
    # Then: the real paths appear nowhere — a literal grep of this file for
    # any of them must come up empty, so tests can only ever act on injected
    # tmp_path locations.
    for needle in (real_symlink, real_releases, real_config):
        assert needle not in source, f"test file references real deployment path {needle!r}"