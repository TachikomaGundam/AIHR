"""Support-surface tests: empty-board status, plugin cache refresh, support bundle."""

from __future__ import annotations

import os
import tarfile
from pathlib import Path

import pytest

from tests.test_cli import _KeyedConn

from hr import lifecycle
from hr.cli_report_verdict import LATEST_SWEEP_SQL, build_status_report
from hr.dialect_contract import CONTRACT_STATUS_SQL, status_lines
from hr.support_bundle import collect, redact

import json as _json

_CONTRACT_FACTS = {
    "endpoint_url": "http://10.0.0.3:8000/v1",
    "model_slug": "q3",
    "accepted_efforts": ["low", "medium", "xhigh"],
    "thinking_key": "reasoning",
    "usage_in_stream": True,
    "tool_calls_ok": True,
    "answer_chars_at_small_budget": 913,
    "probe_version": 1,
}
_CONTRACT_ROW = (
    "local-qwen/q3", "http://10.0.0.3:8000/v1", _json.dumps(_CONTRACT_FACTS), 1,
    "2026-10-04 02:00:00+00",
)


def _router(**over: object) -> dict[str, object]:
    base = {
        LATEST_SWEEP_SQL: [],
        CONTRACT_STATUS_SQL: [_CONTRACT_ROW],
    }
    base.update(over)
    return base


def test_status_on_empty_board_is_friendly_and_shows_contracts() -> None:
    report = build_status_report(_KeyedConn(_router()))
    assert "board is empty" in report
    assert "hr bench" in report
    assert "xhigh" in report  # contract block rendered, not swallowed


def test_status_lines_no_contracts() -> None:
    text = status_lines(_KeyedConn(_router(**{CONTRACT_STATUS_SQL: []})))
    assert "none yet" in text


def test_refresh_plugin_cache_purges_pinned_wrapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "opencode" / "packages" / "opencode-fastdraw@latest"
    cache.mkdir(parents=True)
    (cache / "package.json").write_text('{"dependencies":{"opencode-fastdraw":"1.2.0"}}')
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    messages: list[str] = []
    lifecycle.refresh_plugin_cache(say=messages.append)
    assert not cache.exists()
    assert any("purged" in m and "1.2.0" in m for m in messages)


def test_refresh_plugin_cache_noop_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    messages: list[str] = []
    lifecycle.refresh_plugin_cache(say=messages.append)
    assert messages == []


def test_redact_covers_credential_shapes() -> None:
    out = redact('apiKey: "sk-abc123"\nAIHR_DB_PASSWORD=hunter2\nauth_token = x.y.z')
    assert "sk-abc123" not in out and "hunter2" not in out and "x.y.z" not in out
    assert out.count("***REDACTED***") == 3


def test_support_bundle_contents_and_redaction(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".aihr" / "log").mkdir(parents=True)
    (home / ".aihr" / "db.env").write_text("AIHR_DB_PASSWORD=hunter2\nAIHR_DB_PORT=5433\n")
    (home / ".aihr" / "log" / "pg.log").write_text("LOG: ok\napi_key=sk-live-999\n" * 500)
    (home / ".aihr" / "receipt.json").write_text('{"version": "0.4.3", "entries": []}')
    router = {
        "SELECT count(*) FROM hr.sweep": [(2,)],
        "SELECT run_id, sweep_id, model_id, battery_id, status, ": [
            ("r1", "s1", "m", "b", "scored", "-", "c0ff33", "2026-10-04")
        ],
        "SELECT incident_id, kind, details_json, recorded_at ": [
            ("i1", "adapter_setup_failure", '{"error":"400 reasoning effort high"}', "2026-10-04")
        ],
        CONTRACT_STATUS_SQL: [_CONTRACT_ROW],
        "SELECT m.run_id, m.item_id, m.repetition, m.score, ": [
            ("r1", "item7", 0, 60.0, 900, 240, 41234, None, "TOTAL: 105.63")
        ],
    }
    out_dir = tmp_path / "out"
    bundle, digest = collect(_KeyedConn(router), home=home, out_dir=out_dir, version="0.4.3")
    assert bundle.exists() and len(digest) == 64
    with tarfile.open(bundle) as tar:
        names = {m.name for m in tar.getmembers()}
        assert "support/receipt.json" in names and "support/db.env" in names
        blobs = {
            m.name: tar.extractfile(m).read().decode()  # type: ignore[union-attr]
            for m in tar.getmembers()
        }
    joined = "\n".join(blobs.values())
    assert "hunter2" not in joined and "sk-live-999" not in joined
    assert "5433" in blobs["support/db.env"]  # non-secret kept: bundle stays useful
    assert blobs["support/hr-version.txt"].startswith("aihr 0.4.3")
    assert "400 reasoning effort" in blobs["support/tables-incidents.tsv"]
    assert "xhigh" in blobs["support/tables-contracts.tsv"]
