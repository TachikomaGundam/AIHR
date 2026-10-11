"""On-demand interpreter lane tests (0.6.3 slim bundle, owner ruling 2026-10-10).

Pure + injected download — no network, no PyInstaller: the lane's contract
is verify-then-place, never-clobber-on-failure, and the naming mirror between
runtime (hr/py_fetch) and build tooling (scripts/bundle_core).
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from hr.py_fetch import (
    FetchError,
    fetch_interpreter,
    platform_pair,
    py_asset_name,
    py_fetch_cli,
    read_receipt_version,
)


def _bundle_core():
    from scripts.bundle_core import py_archive_name  # same import style as scripts/bundle_selftest

    return py_archive_name


def test_asset_name_mirrors_build_tooling() -> None:
    build_side = _bundle_core()
    for version, os_name, arch in (
        ("0.6.3", "linux", "x86_64"),
        ("0.6.3", "macos", "aarch64"),
        ("0.7.0", "macos", "x86_64"),
    ):
        assert py_asset_name(version, os_name, arch) == build_side(version, os_name, arch)


def test_platform_pair_mapping() -> None:
    assert platform_pair("Linux", "x86_64") == ("linux", "x86_64")
    assert platform_pair("Darwin", "arm64") == ("macos", "aarch64")
    assert platform_pair("Windows", "AMD64") is None


def _make_py_asset(version: str, os_name: str, arch: str) -> tuple[str, bytes]:
    """Return (asset_name, tar.gz bytes) carrying a minimal py/bin/python3."""
    name = py_asset_name(version, os_name, arch)
    buf = io.BytesIO()
    payload = b"#!/bin/sh\necho fake\n"
    info = tarfile.TarInfo("py/bin/python3")
    info.size = len(payload)
    info.mode = 0o755
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.addfile(info, io.BytesIO(payload))
    return name, buf.getvalue()


def _fetcher(asset: str, blob: bytes):
    def download(url: str) -> bytes:
        if url.endswith(".sha256"):
            return (f"{hashlib.sha256(blob).hexdigest()}  {asset}\n").encode()
        return blob
    return download


def test_fetch_places_verified_asset_and_stamps(tmp_path: Path) -> None:
    asset, blob = _make_py_asset("0.6.3", "linux", "x86_64")
    rc = fetch_interpreter(
        d=tmp_path, version="0.6.3", os_name="linux", arch="x86_64",
        download=_fetcher(asset, blob), say=lambda _s: None,
    )
    assert rc == 0
    assert (tmp_path / "py" / "bin" / "python3").is_file()
    assert (tmp_path / "py" / ".aihr-py-stamp").read_text(encoding="ascii").strip() == "0.6.3"


def test_tampered_bytes_never_clobber_existing_interpreter(tmp_path: Path) -> None:
    old = tmp_path / "py" / "bin" / "python3"
    old.parent.mkdir(parents=True)
    old.write_text("OLD", encoding="ascii")
    asset, blob = _make_py_asset("0.6.3", "linux", "x86_64")

    def lying_download(url: str) -> bytes:
        if url.endswith(".sha256"):
            return ("0" * 64 + f"  {asset}\n").encode()
        return blob

    rc = fetch_interpreter(
        d=tmp_path, version="0.6.3", os_name="linux", arch="x86_64",
        download=lying_download, say=lambda _s: None,
    )
    assert rc == 1
    assert old.read_text(encoding="ascii") == "OLD", "a failed fetch must not touch D/py"


def test_missing_sidecar_is_a_clean_failure(tmp_path: Path) -> None:
    def download(_url: str) -> bytes:
        raise FetchError("HTTP 404")

    rc = fetch_interpreter(
        d=tmp_path, version="0.6.3", os_name="linux", arch="x86_64",
        download=download, say=lambda _s: None,
    )
    assert rc == 1
    assert not (tmp_path / "py").exists()


def test_receipt_version_reader(tmp_path: Path) -> None:
    assert read_receipt_version(tmp_path) is None
    (tmp_path / "receipt.json").write_text(json.dumps({"version": "0.6.3"}), encoding="utf-8")
    assert read_receipt_version(tmp_path) == "0.6.3"


def test_cli_refuses_without_bundle_or_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIHR_HOME_DIR", str(tmp_path))
    assert py_fetch_cli() == 1  # no app/hr -> install first
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "hr").write_text("x", encoding="ascii")
    assert py_fetch_cli() == 1  # still no receipt version and no --version


def test_cli_reports_already_placed_without_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIHR_HOME_DIR", str(tmp_path))
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "hr").write_text("x", encoding="ascii")
    (tmp_path / "receipt.json").write_text(json.dumps({"version": "0.6.3"}), encoding="ascii")
    py = tmp_path / "py" / "bin"
    py.mkdir(parents=True)
    (py / "python3").write_text("x", encoding="ascii")
    assert py_fetch_cli() == 0
