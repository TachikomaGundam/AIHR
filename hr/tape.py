"""S2 tape store: record live HTTP exchanges, replay them offline.

Authoritative design: .omo/evidence/sim-ladder-20261016/S2-MINI-DESIGN-20261031.md
Spec: SIM-LADDER-v2.md line 8 (verbatim invariants honoured here):

- replay NEVER falls back to live - the code path does not exist here;
- no tape / different machine -> the whole replay layer is fail-closed;
- integrity = content addressing (sha256) + manifest preflight; a mismatch
  raises, it never silently re-fetches.

Key = sha256 over the canonical request: method + URL path (the host is
machine-side endpoint metadata, deliberately NOT part of the key so the same
tape replays on any machine that fronts the same path) + non-secret headers
(closed exclusion list, versioned with the first tape write) + the full body
(recon 2026-10-31: request bodies carry no timestamps or random fields -
zero body exclusions; if noise ever enters, keys drift and the re-record
procedure is the named path).
"""

from __future__ import annotations

import hashlib
import json
import platform
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

TAPE_MANIFEST_VERSION = 1
TAPE_ENTRY_VERSION = 1

#: closed exclusion list v1 - frozen before the first tape write (spec).
#: Matching is case-insensitive; prefix entries end with '*'.
SECRET_HEADER_EXCLUSIONS: tuple[str, ...] = (
    "authorization",
    "x-api-key",
    "api-key",
    "cookie",
    "x-stainless-lang",
    "x-stainless-package-version",
    "x-stainless-runtime",
    "x-stainless-runtime-version",
    "user-agent",
)


class TapeError(RuntimeError):
    """Base for every tape failure - replay never degrades silently."""


class TapeMissError(TapeError):
    """Key not on the tape. NEVER auto-fallback to live: key drift -> the
    re-record ticket procedure; provider drift -> the wound-slip procedure."""


class TapeIntegrityError(TapeError):
    """Content does not match the manifest (tampering, partial copy)."""


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_key(
    method: str,
    url: str,
    headers: dict[str, str] | None,
    body: Any,
) -> str:
    """Stable tape key for one HTTP request. See module docstring."""
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    kept_headers = {
        k: v
        for k, v in sorted(lowered.items())
        if not any(
            k == ex or (ex.endswith("*") and k.startswith(ex[:-1]))
            for ex in SECRET_HEADER_EXCLUSIONS
        )
    }
    payload = {
        "k": TAPE_MANIFEST_VERSION,
        "method": method.upper(),
        "path": urlsplit(url).path,
        "headers": kept_headers,
        "body": body,
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReplayEntry:
    status: int
    sse_lines: tuple[str, ...]


class TapeStore:
    """Recording side: tee live exchanges into a tape directory."""

    def __init__(self, tape_dir: Path | str) -> None:
        self.dir = Path(tape_dir)
        (self.dir / "entries").mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, dict[str, Any]] = {}

    def record(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None,
        body: Any,
        status: int,
        sse_lines: list[str],
    ) -> str:
        key = request_key(method, url, headers, body)
        content = _canonical(
            {"k": TAPE_ENTRY_VERSION, "status": status, "sse_lines": sse_lines}
        )
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        path = self.dir / "entries" / f"{digest}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
        self._entries[key] = {
            "sha256": digest,
            "bytes": path.stat().st_size,
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "path": urlsplit(url).path,
        }
        return key

    def finalize(self, endpoints: list[str] | None = None) -> Path:
        """Write the manifest; call once, at the end of the recording run."""
        manifest = {
            "version": TAPE_MANIFEST_VERSION,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "machine": platform.node(),
            "endpoints": sorted(set(endpoints or [])),
            "entries": dict(sorted(self._entries.items())),
        }
        path = self.dir / "tape.json"
        path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return path


class TapeReader:
    """Replay side. Construction verifies the manifest preflight: a tape that
    cannot be trusted never answers a lookup."""

    def __init__(self, tape_dir: Path | str) -> None:
        self.dir = Path(tape_dir)
        manifest_path = self.dir / "tape.json"
        if not manifest_path.is_file():
            raise TapeError(
                f"no tape manifest at {manifest_path} - replay is fail-closed "
                "without a recorded tape (record one first; there is no "
                "record-on-demand path by design)"
            )
        self.manifest: dict[str, Any] = json.loads(manifest_path.read_text("utf-8"))
        if int(self.manifest.get("version", 0)) != TAPE_MANIFEST_VERSION:
            raise TapeError(
                f"tape manifest version {self.manifest.get('version')!r} not "
                f"supported (this build speaks {TAPE_MANIFEST_VERSION})"
            )
        self._verify()

    def _verify(self) -> None:
        entries: dict[str, Any] = self.manifest.get("entries", {})
        for key, meta in entries.items():
            path = self.dir / "entries" / f"{meta['sha256']}.json"
            if not path.is_file():
                raise TapeIntegrityError(f"tape entry missing for key {key[:12]}…")
            content = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if digest != meta["sha256"]:
                raise TapeIntegrityError(
                    f"tape entry {path.name} hash mismatch (manifest says "
                    f"{meta['sha256'][:12]}…, content hashes {digest[:12]}…)"
                )

    def lookup(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None,
        body: Any,
    ) -> ReplayEntry:
        key = request_key(method, url, headers, body)
        meta = self.manifest.get("entries", {}).get(key)
        if meta is None:
            raise TapeMissError(
                f"tape miss key={key[:16]}… path={urlsplit(url).path} - never "
                "falling back to live: key drift -> re-record ticket procedure; "
                "provider drift -> wound-slip procedure (S2 design doc)"
            )
        content = json.loads(
            (self.dir / "entries" / f"{meta['sha256']}.json").read_text("utf-8")
        )
        return ReplayEntry(
            status=int(content["status"]), sse_lines=tuple(content["sse_lines"])
        )
