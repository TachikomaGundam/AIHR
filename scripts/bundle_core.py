"""Pure logic for the AIHR turnkey bundle builder (Item 11 packaging).

No network, no subprocess: everything here is deterministic and exercised
by ``build_bundle.py --self-test`` on fabricated tmp trees. The IO halves
(download, PyInstaller spawn, archive writing) live in ``build_bundle.py``.

Bundle contract (owned dir ``D`` = ``~/.aihr`` posix / ``%LOCALAPPDATA%\\aihr``
windows), mirrored inside the produced archive with no top-level prefix::

    app/hr(.exe)          PyInstaller onedir output (+ _internal/)
    pg/bin/...            vendored PostgreSQL: initdb, pg_ctl, psql, postgres
    pg/lib/  pg/share/    runtime + share trees
    share/aihr/           repo templates: hr.toml.example + configs/*.yaml

``D/bin`` (shim dir), ``D/db.env`` and ``D/receipt.json`` are created at
runtime by ``hr install-post`` — never by the builder.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

OsName = Literal["linux", "windows", "macos"]
ArchName = Literal["x86_64", "aarch64"]
ArchiveKind = Literal["tar.gz", "zip"]

OS_NAMES: Final[tuple[OsName, ...]] = ("linux", "windows", "macos")
ARCH_NAMES: Final[tuple[ArchName, ...]] = ("x86_64", "aarch64")

#: PostgreSQL server binaries the lifecycle layer (hr install-post / db-up)
#: requires inside ``pg/bin/``.
PG_REQUIRED_BINS: Final[tuple[str, ...]] = ("initdb", "pg_ctl", "psql", "createdb", "postgres")

#: Directories copied from the theseus tree into ``pg/`` (everything else —
#: include/, debug symbols — is dropped to keep the bundle lean).
PG_DIRS: Final[tuple[str, ...]] = ("bin", "lib", "share")

_KIND_MAP: Final[dict[str, ArchiveKind]] = {"tar.gz": "tar.gz", "zip": "zip"}

_HEX_DIGITS: Final = "0123456789abcdef"

CACHE_SUBDIR: Final = Path("aihr-build") / "pg"


class BundleError(RuntimeError):
    """Any violation of the pin file, the theseus layout, or the bundle contract."""


@dataclass(frozen=True, slots=True)
class PgTarget:
    """One vendored-PostgreSQL target from ``release/pg-pin.json``."""

    triple: str
    asset: str
    kind: ArchiveKind
    sha256: str


@dataclass(frozen=True, slots=True)
class PgPin:
    """Parsed ``release/pg-pin.json``: exact theseus release + per-target hashes."""

    source_repo: str
    release_tag: str
    pg_version: str
    targets: dict[str, PgTarget]


def _require_dict(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BundleError(f"{what} must be a JSON object, got {type(value).__name__}")
    return {str(k): v for k, v in value.items()}


def _require_str(doc: dict[str, object], field: str, label: str | None = None) -> str:
    value = doc.get(field)
    if not isinstance(value, str) or not value:
        raise BundleError(f"field {label or field!r} must be a non-empty string")
    return value


def target_key(os_name: str, arch: str) -> str:
    return f"{os_name}-{arch}"


def pin_file(repo_root: Path) -> Path:
    return repo_root / "release" / "pg-pin.json"


def archive_kind_for(os_name: str) -> ArchiveKind:
    return "zip" if os_name == "windows" else "tar.gz"


def exe_suffix(os_name: str) -> str:
    return ".exe" if os_name == "windows" else ""


def bundle_archive_name(version: str, os_name: str, arch: str) -> str:
    return f"aihr-{version}-{os_name}-{arch}.{archive_kind_for(os_name)}"


def load_pin_text(raw: str) -> PgPin:
    """Parse + validate the pin JSON text. Raises BundleError on anything malformed."""
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BundleError(f"pg-pin.json is not valid JSON: {exc}") from exc
    doc = _require_dict(parsed, "pg-pin.json root")
    source_repo = _require_str(doc, "source_repo")
    release_tag = _require_str(doc, "release_tag")
    pg_version = _require_str(doc, "pg_version")
    targets_raw = _require_dict(doc.get("targets"), "targets")
    targets: dict[str, PgTarget] = {}
    for key, value in targets_raw.items():
        t = _require_dict(value, f"targets.{key}")
        kind = _KIND_MAP.get(_require_str(t, "kind", f"targets.{key}.kind"))
        if kind is None:
            raise BundleError(f"targets.{key}.kind must be tar.gz or zip")
        sha = _require_str(t, "sha256", f"targets.{key}.sha256").lower()
        if len(sha) != 64 or any(c not in _HEX_DIGITS for c in sha):
            raise BundleError(f"targets.{key}.sha256 is not a 64-char hex digest: {sha!r}")
        triple = _require_str(t, "triple", f"targets.{key}.triple")
        asset = _require_str(t, "asset", f"targets.{key}.asset")
        asset_prefix = f"postgresql-{pg_version}-"
        if not asset.startswith(asset_prefix):
            raise BundleError(
                f"targets.{key}.asset must start with {asset_prefix!r}, got {asset!r}"
            )
        targets[key] = PgTarget(triple=triple, asset=asset, kind=kind, sha256=sha)
    return PgPin(
        source_repo=source_repo, release_tag=release_tag, pg_version=pg_version, targets=targets
    )


def pick_target(pin: PgPin, os_name: str, arch: str) -> PgTarget:
    key = target_key(os_name, arch)
    target = pin.targets.get(key)
    if target is None:
        raise BundleError(
            f"pg-pin.json has no target {key!r}; available: {sorted(pin.targets)}"
        )
    return target


def download_url(pin: PgPin, target: PgTarget) -> str:
    return (
        f"https://github.com/{pin.source_repo}/releases/download/"
        f"{pin.release_tag}/{target.asset}"
    )


# --------------------------------------------------------------------------
# linux system-library closure (release/libs-pin.json) — see module docstring
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LibFile:
    """One ``.so`` to lift out of a pinned .deb into ``pg/lib/`` (plus SONAME link)."""

    member: str
    dest: str
    symlink: str


@dataclass(frozen=True, slots=True)
class DebPin:
    """One pinned Ubuntu .deb and the members it contributes."""

    url: str
    sha256: str
    files: tuple[LibFile, ...]

    @property
    def asset(self) -> str:
        return self.url.rsplit("/", 1)[-1]


#: Members ``pg/lib`` gains on linux targets; the missing SONAME that killed
#: testbed acceptance of the first 0.4.0 build (Ubuntu 26.04 ships libxml2 .16
#: only while the vendored server still DT_NEEDEDs ``libxml2.so.2``).
PG_LINUX_LIBS: Final[tuple[str, ...]] = ("pg/lib/libxml2.so.2", "pg/lib/liblzma.so.5")

PG_LIBS_NOTICE: Final[str] = (
    "Vendored system libraries for the linux turnkey bundle.\n"
    "\n"
    "libxml2 2.9.14 (package 2.12.7+dfsg+really2.9.14-0.4ubuntu0.4) - MIT license\n"
    "  https://gitlab.gnome.org/GNOME/libxml2\n"
    "liblzma 5.8.1 (package liblzma5 5.8.1-1ubuntu0.1) - 0BSD-style license\n"
    "  https://github.com/tukaani-project/xz\n"
    "\n"
    "Both are unmodified upstream binaries taken from Ubuntu noble-security\n"
    "(archive.ubuntu.com pool); sha256 pins live in release/libs-pin.json.\n"
    "They are loaded ONLY by the bundled PostgreSQL server process via a\n"
    "scoped LD_LIBRARY_PATH and never shadow system libraries elsewhere.\n"
)


def libs_pin_file(repo_root: Path) -> Path:
    return repo_root / "release" / "libs-pin.json"


def parse_libs_pin(raw: str) -> tuple[DebPin, ...]:
    """Parse + validate ``release/libs-pin.json`` text. Raises BundleError."""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BundleError(f"libs-pin.json is not valid JSON: {exc}") from exc
    body = _require_dict(doc, "libs-pin document")
    entries = body.get("debs")
    if not isinstance(entries, list) or not entries:
        raise BundleError("libs-pin.json must contain a non-empty 'debs' list")
    out: list[DebPin] = []
    for index, entry in enumerate(entries):
        label = f"libs-pin debs[{index}]"
        row = _require_dict(entry, label)
        url = _require_str(row, "url", f"{label}.url")
        sha256 = _require_str(row, "sha256", f"{label}.sha256")
        if not url.startswith(("http://", "https://")):
            raise BundleError(f"{label}.url must be an http(s) URL: {url}")
        if len(sha256) != 64 or any(c not in _HEX_DIGITS for c in sha256.lower()):
            raise BundleError(f"{label}.sha256 must be a 64-char hex digest")
        files_raw = row.get("extract")
        if not isinstance(files_raw, list) or not files_raw:
            raise BundleError(f"{label}.extract must be a non-empty list")
        files: list[LibFile] = []
        for f_index, f_raw in enumerate(files_raw):
            f_label = f"{label}.extract[{f_index}]"
            f_row = _require_dict(f_raw, f_label)
            files.append(
                LibFile(
                    member=_require_str(f_row, "member", f"{f_label}.member"),
                    dest=_require_str(f_row, "dest", f"{f_label}.dest"),
                    symlink=_require_str(f_row, "symlink", f"{f_label}.symlink"),
                )
            )
        out.append(DebPin(url=url, sha256=sha256, files=tuple(files)))
    return tuple(out)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sidecar_text(archive_name: str, sha256_hex: str) -> str:
    """sha256sum -c compatible sidecar line."""
    return f"{sha256_hex}  {archive_name}\n"


def parse_sidecar(text: str) -> str:
    """Extract the hex digest from the first sidecar line."""
    for line in text.splitlines():
        parts = line.split()
        if parts and len(parts[0]) == 64 and all(c in _HEX_DIGITS for c in parts[0].lower()):
            return parts[0].lower()
    raise BundleError(f"no 64-char hex digest found in sidecar text: {text[:80]!r}")


def verify_archive_checksum(archive: Path) -> tuple[str, str]:
    """Recompute sha256 of ``archive`` and compare against its ``.sha256`` sidecar.

    Returns (expected, actual); raises BundleError on mismatch or missing sidecar.
    """
    sidecar = archive.with_name(archive.name + ".sha256")
    if not sidecar.is_file():
        raise BundleError(f"missing checksum sidecar: {sidecar}")
    expected = parse_sidecar(sidecar.read_text(encoding="ascii"))
    actual = sha256_file(archive)
    if expected != actual:
        raise BundleError(f"checksum mismatch for {archive.name}: {actual} != {expected}")
    return expected, actual


# --------------------------------------------------------------------------
# theseus layout normalization
# --------------------------------------------------------------------------


def pick_pg_root(extracted: Path) -> Path:
    """Return the directory that directly contains ``bin/`` in an extracted
    theseus tree. Three shapes occur in the wild: windows zip has ``bin/`` at
    the archive root; some builds nest under ``usr/``; current theseus
    tarballs wrap everything in ONE directory named after the asset stem.
    """
    candidates = [extracted]
    children = [p for p in extracted.iterdir() if p.is_dir()]
    if len(children) == 1:
        candidates.append(children[0])
    for base in candidates:
        if (base / "bin").is_dir():
            return base
        usr = base / "usr"
        if (usr / "bin").is_dir():
            return usr
    raise BundleError(f"extracted tree has neither bin/ nor usr/bin/ at top level: {extracted}")


def normalize_pg(extracted: Path, dest: Path, os_name: str) -> list[str]:
    """Copy {bin,lib,share} from a normalized theseus root into ``dest`` (pg/).

    Verifies the four required server binaries exist and (posix) marks
    everything under bin/ executable. Returns the copied top-level dir names.
    """
    root = pick_pg_root(extracted)
    copied: list[str] = []
    for name in PG_DIRS:
        src = root / name
        if not src.is_dir():
            raise BundleError(f"theseus tree is missing {name}/ under {root}")
        shutil.copytree(src, dest / name, symlinks=True)
        copied.append(name)
    suffix = exe_suffix(os_name)
    missing = [b for b in PG_REQUIRED_BINS if not (dest / "bin" / f"{b}{suffix}").exists()]
    if missing:
        raise BundleError(f"pg/bin is missing required executables: {missing}")
    if os_name != "windows":
        _chmod_exec_tree(dest / "bin")
    return copied


def _chmod_exec_tree(directory: Path) -> None:
    for path in directory.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# --------------------------------------------------------------------------
# bundle archive layout validation (--verify / --list consumers)
# --------------------------------------------------------------------------


def required_bundle_members(os_name: str) -> tuple[str, ...]:
    """The D-layout entries every bundle must carry (no top-level prefix)."""
    suffix = exe_suffix(os_name)
    members = [
        f"app/hr{suffix}",
        "share/aihr/hr.toml.example",
        "share/aihr/configs/models.yaml",
    ]
    members += [f"pg/bin/{b}{suffix}" for b in PG_REQUIRED_BINS]
    members += ["pg/lib", "pg/share"]
    if os_name == "linux":
        members += list(PG_LINUX_LIBS) + ["pg/lib/NOTICE"]
    return tuple(members)


def check_bundle_members(members: list[str], os_name: str) -> list[str]:
    """Return human-readable descriptions of contract violations.

    ``members`` are archive-relative paths (tar names or zip namelist entries;
    directories may appear as ``pg/lib/`` style entries).
    """
    dirs = {m.rstrip("/") for m in members}
    problems: list[str] = []
    for req in required_bundle_members(os_name):
        if req in ("pg/lib", "pg/share"):
            if not any(d == req or d.startswith(req + "/") for d in dirs):
                problems.append(f"missing directory: {req}/")
        elif req not in members:
            problems.append(f"missing file: {req}")
    if os_name != "windows" and not any(m.startswith("app/_internal/") for m in members):
        problems.append("missing PyInstaller app/_internal/ payload")
    return problems


# --------------------------------------------------------------------------
# defaults shared by builder + docs
# --------------------------------------------------------------------------


def default_pg_cache_dir() -> Path:
    return Path.home() / ".cache" / CACHE_SUBDIR


def default_repo_root() -> Path:
    """Repo root inferred from this file's location (scripts/ -> repo)."""
    return Path(__file__).resolve().parent.parent
