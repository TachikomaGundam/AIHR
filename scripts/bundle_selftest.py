"""Offline self-tests for the pure bundle-builder logic (bundle_core).

Driven by ``python scripts/build_bundle.py --self-test``. Everything here
runs on fabricated tmp trees — no network, no PyInstaller, no root.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.bundle_core import (  # noqa: E402
    PG_LINUX_LIBS,
    PG_REQUIRED_BINS,
    BundleError,
    check_bundle_members,
    default_repo_root,
    libs_pin_file,
    load_pin_text,
    normalize_pg,
    parse_libs_pin,
    parse_sidecar,
    pick_pg_root,
    pick_target,
    pin_file,
    required_bundle_members,
    sha256_file,
    sidecar_text,
    verify_archive_checksum,
    bundle_archive_name,
    download_url,
)

_VALID_PIN: Final = """
{
  "source_repo": "theseus-rs/postgresql-binaries",
  "release_tag": "17.5.0",
  "pg_version": "17.5.0",
  "targets": {
    "linux-x86_64": {
      "triple": "x86_64-unknown-linux-gnu",
      "asset": "postgresql-17.5.0-x86_64-unknown-linux-gnu.tar.gz",
      "kind": "tar.gz",
      "sha256": "0000000000000000000000000000000000000000000000000000000000000001"
    },
    "windows-x86_64": {
      "triple": "x86_64-pc-windows-msvc",
      "asset": "postgresql-17.5.0-x86_64-pc-windows-msvc.zip",
      "kind": "zip",
      "sha256": "0000000000000000000000000000000000000000000000000000000000000002"
    }
  }
}
"""


def _expect_error(fn: Callable[[], object], needle: str) -> str:
    try:
        fn()
    except BundleError as exc:
        assert needle in str(exc), f"error {exc!r} lacks {needle!r}"
        return str(exc)
    raise AssertionError(f"expected BundleError containing {needle!r}; none raised")


def test_pin_parse() -> None:
    pin = load_pin_text(_VALID_PIN)
    assert pin.release_tag == "17.5.0"
    assert pin.targets["linux-x86_64"].kind == "tar.gz"
    assert download_url(pin, pin.targets["windows-x86_64"]) == (
        "https://github.com/theseus-rs/postgresql-binaries/releases/download/"
        "17.5.0/postgresql-17.5.0-x86_64-pc-windows-msvc.zip"
    )
    _expect_error(lambda: load_pin_text("{not json"), "not valid JSON")
    bad_sha = _VALID_PIN.replace("0" * 63 + "1", "deadbeef")
    _expect_error(lambda: load_pin_text(bad_sha), "64-char hex digest")
    bad_kind = _VALID_PIN.replace('"tar.gz"', '"rar"')
    _expect_error(lambda: load_pin_text(bad_kind), "must be tar.gz or zip")
    bad_asset = _VALID_PIN.replace("postgresql-17.5.0-x86_64-unknown", "postgres-17.5.0-x86_64-unknown")
    _expect_error(lambda: load_pin_text(bad_asset), "must start with")


def test_real_pin_file() -> None:
    """The committed release/pg-pin.json must satisfy the builder's invariants."""
    pin = load_pin_text(pin_file(default_repo_root()).read_text(encoding="utf-8"))
    by_target = {
        "linux-x86_64": ("linux", "x86_64", "x86_64-unknown-linux-gnu"),
        "windows-x86_64": ("windows", "x86_64", "x86_64-pc-windows-msvc"),
        "macos-aarch64": ("macos", "aarch64", "aarch64-apple-darwin"),
        "macos-x86_64": ("macos", "x86_64", "x86_64-apple-darwin"),
    }
    for key, (os_name, arch, triple) in by_target.items():
        target = pin.targets.get(key)
        assert target is not None, f"pin missing target {key}"
        assert target.triple == triple, f"{key}: triple {target.triple} != {triple}"
        assert target.asset.startswith(f"postgresql-{pin.pg_version}-{triple}")
        expected_kind = "zip" if key.startswith("windows") else "tar.gz"
        assert target.kind == expected_kind, f"{key}: kind {target.kind}"
        # the CLI token pair (os, arch) must resolve to this target: locks the
        # contract naming aihr-V-os-arch against the pin keys.
        assert pick_target(pin, os_name, arch) is target


def test_target_lookup() -> None:
    pin = load_pin_text(_VALID_PIN)
    assert pick_target(pin, "linux", "x86_64").kind == "tar.gz"
    _expect_error(lambda: pick_target(pin, "macos", "aarch64"), "no target 'macos-aarch64'")


def test_archive_names() -> None:
    assert bundle_archive_name("0.4.0", "linux", "x86_64") == "aihr-0.4.0-linux-x86_64.tar.gz"
    assert bundle_archive_name("0.4.0", "macos", "aarch64") == "aihr-0.4.0-macos-aarch64.tar.gz"
    assert bundle_archive_name("0.4.0", "windows", "x86_64") == "aihr-0.4.0-windows-x86_64.zip"


def test_sha_sidecar_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        payload = Path(tmp) / "aihr-0.4.0-linux-x86_64.tar.gz"
        payload.write_bytes(b"bundle-bytes")
        digest = hashlib.sha256(b"bundle-bytes").hexdigest()
        assert sha256_file(payload) == digest
        sidecar = payload.with_name(payload.name + ".sha256")
        sidecar.write_text(sidecar_text(payload.name, digest), encoding="ascii")
        expected, actual = verify_archive_checksum(payload)
        assert expected == actual == digest
        # certutil-style multi-line sidecar (as shipped by theseus windows assets)
        assert parse_sidecar(f"SHA256 hash of {payload.name}:\n{digest.upper()}\nCertUtil: ok\n") == digest
        payload.write_bytes(b"tampered!")
        _expect_error(lambda: verify_archive_checksum(payload), "checksum mismatch")
        sidecar.unlink()
        _expect_error(lambda: verify_archive_checksum(payload), "missing checksum sidecar")


def _fabricate_theseus(root: Path, *, usr_prefixed: bool, os_name: str, drop: str | None) -> Path:
    base = root / ("usr" if usr_prefixed else "")
    suffix = ".exe" if os_name == "windows" else ""
    (base / "bin").mkdir(parents=True)
    for binary in PG_REQUIRED_BINS:
        if binary == drop:
            continue
        f = base / "bin" / f"{binary}{suffix}"
        f.write_bytes(b"\x7fELF fake")
        f.chmod(0o644)  # deliberately non-exec: normalize_pg must fix this
    (base / "lib").mkdir()
    (base / "lib" / "libpq.so.5").write_bytes(b"lib")
    (base / "share" / "postgresql").mkdir(parents=True)
    (base / "share" / "postgresql" / "postgres.bki").write_bytes(b"bki")
    (base / "include").mkdir()  # must be dropped by normalization
    return root


def test_normalize_layouts() -> None:
    for usr_prefixed, os_name in ((True, "linux"), (False, "windows"), (True, "macos")):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fabricate_theseus(Path(tmp), usr_prefixed=usr_prefixed, os_name=os_name, drop=None)
            dest = Path(tmp) / "D" / "pg"
            copied = normalize_pg(pick_pg_root(root), dest, os_name)
            assert copied == ["bin", "lib", "share"]
            suffix = ".exe" if os_name == "windows" else ""
            for binary in PG_REQUIRED_BINS:
                assert (dest / "bin" / f"{binary}{suffix}").is_file()
            assert not (dest / "include").exists(), "include/ must not ship"
            assert (dest / "share" / "postgresql" / "postgres.bki").is_file()
            if os.name != "nt" and os_name != "windows":
                mode = (dest / "bin" / "initdb").stat().st_mode
                assert mode & stat.S_IXUSR, "posix pg/bin entries must be executable"
    with tempfile.TemporaryDirectory() as tmp:
        root = _fabricate_theseus(Path(tmp), usr_prefixed=True, os_name="linux", drop="psql")
        _expect_error(
            lambda: normalize_pg(pick_pg_root(root), Path(tmp) / "pg", "linux"),
            "missing required executables: ['psql']",
        )
        empty = Path(tmp) / "junk"
        empty.mkdir()
        _expect_error(lambda: pick_pg_root(empty), "neither bin/ nor usr/bin/")
    # Theseus 18.6 tarballs wrap the tree in ONE asset-stem directory.
    with tempfile.TemporaryDirectory() as tmp:
        wrapper = Path(tmp) / "postgresql-18.6.0-x86_64-unknown-linux-gnu"
        _fabricate_theseus(wrapper, usr_prefixed=False, os_name="linux", drop=None)
        copied = normalize_pg(pick_pg_root(Path(tmp)), Path(tmp) / "pg", "linux")
        assert copied == ["bin", "lib", "share"], "single-wrapper tree must resolve"
        assert (Path(tmp) / "pg" / "bin" / "initdb").is_file()


def test_check_bundle_members() -> None:
    for os_name in ("linux", "windows", "macos"):
        suffix = ".exe" if os_name == "windows" else ""
        members = [
            f"app/hr{suffix}",
            "app/_internal/base_library.zip",
            *[f"pg/bin/{b}{suffix}" for b in PG_REQUIRED_BINS],
            "pg/lib/libpq.so.5",
            "pg/share/postgresql/postgres.bki",
            "share/aihr/hr.toml.example",
            "share/aihr/configs/models.yaml",
        ]
        if os_name == "linux":
            members += [
                "pg/lib/NOTICE",
                "pg/lib/libxml2.so.2",
                "pg/lib/liblzma.so.5",
            ]
        assert check_bundle_members(members, os_name) == [], os_name
    bad = [m for m in ["app/hr", "share/aihr/hr.toml.example"] ]
    problems = check_bundle_members(bad, "linux")
    assert any("app/_internal" in p for p in problems)
    assert any("missing file: pg/bin/initdb" == p for p in problems)
    assert any("missing directory: pg/lib/" == p for p in problems)
    required = required_bundle_members("windows")
    assert "app/hr.exe" in required and "pg/bin/postgres.exe" in required
    linux = required_bundle_members("linux")
    assert "pg/lib/libxml2.so.2" in linux and "pg/lib/NOTICE" in linux


_VALID_LIBS_PIN: Final = """
{
  "debs": [
    {
      "url": "http://archive.ubuntu.com/ubuntu/pool/main/libx/libxml2/libxml2_2.9.14_amd64.deb",
      "sha256": "0000000000000000000000000000000000000000000000000000000000000001",
      "extract": [
        {"member": "usr/lib/x86_64-linux-gnu/libxml2.so.2.9.14",
         "dest": "libxml2.so.2.9.14", "symlink": "libxml2.so.2"}
      ]
    }
  ]
}
"""


def test_libs_pin_parse() -> None:
    entries = parse_libs_pin(_VALID_LIBS_PIN)
    assert len(entries) == 1 and entries[0].asset.endswith(".deb")
    assert entries[0].files[0].symlink == "libxml2.so.2"
    _expect_error(lambda: parse_libs_pin('{"debs": []}'), "non-empty 'debs'")
    _expect_error(
        lambda: parse_libs_pin('{"debs": [{"url": "ftp://x/y.deb", "sha256": '
        '"0000000000000000000000000000000000000000000000000000000000000001", '
        '"extract": [{"member": "a", "dest": "b", "symlink": "c"}]}]}'),
        "http(s) URL",
    )
    _expect_error(
        lambda: parse_libs_pin('{"debs": [{"url": "http://x/y.deb", "sha256": "nope", '
        '"extract": [{"member": "a", "dest": "b", "symlink": "c"}]}]}'),
        "64-char hex digest",
    )
    text = libs_pin_file(default_repo_root()).read_text(encoding="utf-8")
    committed = parse_libs_pin(text)
    symlinks = {f.symlink for e in committed for f in e.files}
    assert symlinks == {s.removeprefix("pg/lib/") for s in PG_LINUX_LIBS}, (
        "libs-pin.json and PG_LINUX_LIBS must stay in lockstep"
    )


_TESTS: Final[tuple[tuple[str, Callable[[], None]], ...]] = (
    ("pin parse + validation errors", test_pin_parse),
    ("committed release/pg-pin.json invariants", test_real_pin_file),
    ("target lookup", test_target_lookup),
    ("bundle archive naming", test_archive_names),
    ("sha256 sidecar roundtrip + tamper detection", test_sha_sidecar_roundtrip),
    ("theseus layout normalization (usr/ prefix, flat, missing bin)", test_normalize_layouts),
    ("bundle member contract checks", test_check_bundle_members),
    ("libs pin parse + committed release/libs-pin.json invariants", test_libs_pin_parse),
)


def run_self_test() -> int:
    failures = 0
    for name, fn in _TESTS:
        try:
            fn()
        except (AssertionError, BundleError, OSError) as exc:
            failures += 1
            print(f"FAIL  {name}: {exc}")
        else:
            print(f"PASS  {name}")
    print(f"--self-test: {len(_TESTS) - failures}/{len(_TESTS)} passed")
    return 1 if failures else 0
