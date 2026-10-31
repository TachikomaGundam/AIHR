"""S2 tape store tests: key stability, roundtrip, fail-closed invariants."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hr.tape import (
    TapeError,
    TapeIntegrityError,
    TapeMissError,
    TapeReader,
    TapeStore,
    request_key,
)

HEADERS = {"Authorization": "Bearer secret", "content-type": "application/json"}
BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
SSE = ['data: {"choices":[{"delta":{"content":"A: 4"}}]}', "data: [DONE]"]


def _record(tape_dir: Path) -> str:
    store = TapeStore(tape_dir)
    key = store.record("POST", "https://gw.example/v1/chat", HEADERS, BODY, 200, list(SSE))
    store.finalize(endpoints=["gw.example"])
    return key


def test_key_ignores_secret_headers_and_host(tmp_path: Path) -> None:
    a = request_key("POST", "https://A.example/v1/chat", {"authorization": "x", "c": "1"}, BODY)
    b = request_key("POST", "https://B.other/v1/chat", {"Authorization": "y", "c": "1"}, BODY)
    assert a == b  # host out of key, secrets excluded


def test_body_noise_drifts_key(tmp_path: Path) -> None:
    a = request_key("POST", "https://g/v1", HEADERS, BODY)
    b = request_key("POST", "https://g/v1", HEADERS, {**BODY, "max_tokens": 64})
    assert a != b  # body in key: drift means re-record, by design


def test_record_read_roundtrip(tmp_path: Path) -> None:
    key = _record(tmp_path)
    reader = TapeReader(tmp_path)
    entry = reader.lookup("POST", "https://other.host/v1/chat", HEADERS, BODY)
    assert entry.status == 200
    assert entry.sse_lines == tuple(SSE)
    assert reader.manifest["entries"][key]["path"] == "/v1/chat"


def test_miss_raises_never_falls_back(tmp_path: Path) -> None:
    _record(tmp_path)
    reader = TapeReader(tmp_path)
    with pytest.raises(TapeMissError, match="never"):
        reader.lookup("POST", "https://gw.example/v1/chat", HEADERS, {"different": 1})


def test_no_manifest_is_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(TapeError, match="fail-closed"):
        TapeReader(tmp_path)


def test_tampered_entry_fails_integrity(tmp_path: Path) -> None:
    _record(tmp_path)
    entries = tmp_path / "entries"
    victim = next(entries.glob("*.json"))
    victim.write_text('{"k":1,"status":200,"sse_lines":["data: evil"]}', "utf-8")
    with pytest.raises(TapeIntegrityError, match="hash mismatch"):
        TapeReader(tmp_path)


def test_manifest_version_gate(tmp_path: Path) -> None:
    _record(tmp_path)
    manifest_path = tmp_path / "tape.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["version"] = 99
    manifest_path.write_text(json.dumps(manifest), "utf-8")
    with pytest.raises(TapeError, match="version"):
        TapeReader(tmp_path)


def test_entries_are_content_addressed(tmp_path: Path) -> None:
    _record(tmp_path)
    manifest = json.loads((tmp_path / "tape.json").read_text("utf-8"))
    import hashlib

    for meta in manifest["entries"].values():
        blob = (tmp_path / "entries" / f"{meta['sha256']}.json").read_bytes()
        assert hashlib.sha256(blob).hexdigest() == meta["sha256"]
