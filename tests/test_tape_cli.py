"""S2b: tape CLI wiring — adapter_for seam, RecordingHttpxClient, tape factory, CLI flags."""

from __future__ import annotations

from typing import Any

import pytest

from hr.adapters import adapter_for, fleet
from hr.tape import TapeReader, TapeStore
from hr.tape_cli import TapeWiringError, format_receipt, make_tape_factory
from hr.tape_transports import RecordingHttpxClient


def _canned_sse() -> list[str]:
    return [
        'data: {"choices":[{"delta":{"content":"42"}}]}',
        "data: [DONE]",
    ]


def _write_tape(dir_: Any, url: str, body: dict) -> str:
    store = TapeStore(dir_)
    headers = {"authorization": "Bearer zzz", "content-type": "application/json"}
    key = store.record(
        "POST", url, headers, body, status=200, sse_lines=_canned_sse()
    )
    store.finalize(endpoints=[urlsplit_path(url)])
    return key


def urlsplit_path(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).path


# --- adapter_for passthrough seam ------------------------------------------ #


def test_adapter_for_with_transport_seam_builds_fresh_uncached_instance(monkeypatch):
    monkeypatch.setattr(fleet, "provider_type", lambda p: "openai-compat")

    def fake_post(*a: Any, **k: Any) -> Any:
        raise AssertionError("must not be called in this test")

    first = adapter_for("tapeprov/m1", transport_post=fake_post)
    second = adapter_for("tapeprov/m1", transport_post=fake_post)
    assert first is not second  # tape instances never ride the singleton cache
    assert first._post is fake_post


def test_adapter_for_without_seam_keeps_singleton_cache(monkeypatch):
    monkeypatch.setattr(fleet, "provider_type", lambda p: "openai-compat")
    assert adapter_for("tapeprov/m2") is adapter_for("tapeprov/m2")


# --- RecordingHttpxClient (anthropic record side, the S2a gap) -------------- #


class _FakeStreamResp:
    def __init__(self, lines: list[str]) -> None:
        self.status_code = 200
        self._lines = lines
        self.text = "\n".join(lines)

    def iter_lines(self) -> Any:
        yield from self._lines


class _FakeHttpxClient:
    def __init__(self, lines: list[str], explode: bool = False) -> None:
        self._lines = lines
        self._explode = explode

    class _Ctx:
        def __init__(self, resp: _FakeStreamResp, explode: bool) -> None:
            self._resp, self._explode = resp, explode

        def __getattr__(self, name: str) -> Any:
            return getattr(self.__dict__["_resp"], name)

        def __enter__(self) -> "_FakeHttpxClient._Ctx":
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        def iter_lines(self) -> Any:
            yield from self._resp.iter_lines()
            if self._explode:
                raise RuntimeError("mid-stream transport death")

    def stream(self, method: str, url: str, **kw: Any) -> "_FakeHttpxClient._Ctx":
        return self._Ctx(_FakeStreamResp(self._lines), self._explode)


def test_recording_httpx_client_records_on_clean_stream(tmp_path):
    url = "https://gateway.example/v1/messages"
    body = {"model": "claude-ish", "messages": []}
    store = TapeStore(tmp_path)
    client = RecordingHttpxClient(store, _FakeHttpxClient(_canned_sse()))
    with client.stream("POST", url, json=body, headers={"x-api-key": "s"}) as resp:
        lines = list(resp.iter_lines())
    assert lines == _canned_sse()
    reader = _reader_from(store, tmp_path, url, body)
    entry = reader.lookup("POST", url, {"x-api-key": "ignored"}, body)
    assert entry.sse_lines == tuple(_canned_sse())


def test_recording_httpx_client_never_records_partial_stream(tmp_path):
    url = "https://gateway.example/v1/messages"
    body = {"model": "claude-ish"}
    store = TapeStore(tmp_path)
    client = RecordingHttpxClient(store, _FakeHttpxClient(_canned_sse(), explode=True))
    with pytest.raises(RuntimeError, match="mid-stream"):
        with client.stream("POST", url, json=body) as resp:
            list(resp.iter_lines())
    assert store._entries == {}  # fail-closed: a half-truth never reaches tape


def _reader_from(store: TapeStore, dir_: Any, url: str, body: dict) -> TapeReader:
    store.finalize(endpoints=["/v1/messages"])
    return TapeReader(dir_)


# --- make_tape_factory ------------------------------------------------------- #


def test_make_tape_factory_replay_routes_openai_wire_to_tape_post(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, "provider_type", lambda p: "openai-compat")
    url = "https://gate.example/v1/chat/completions"
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    _write_tape(tmp_path, url, body)
    factory, _handle = make_tape_factory(replay_dir=tmp_path)
    adapter = factory("tapeprov/m1")
    headers = {"authorization": "Bearer zzz", "content-type": "application/json"}
    got = adapter._post(url, headers=headers, json=body, stream=True)
    assert list(got.iter_lines()) == _canned_sse()


def test_make_tape_factory_replay_without_manifest_refuses_at_construction(tmp_path):
    with pytest.raises(TapeWiringError, match="fail-closed"):
        make_tape_factory(replay_dir=tmp_path / "nope")


def test_make_tape_factory_rejects_both_directions(tmp_path):
    with pytest.raises(TapeWiringError, match="mutually exclusive"):
        make_tape_factory(record_dir=tmp_path, replay_dir=tmp_path)


def test_format_receipt_names_manifest_sha_and_endpoints(tmp_path):
    store = TapeStore(tmp_path)
    store.record("POST", "https://g.example/v1/chat/completions", {}, {"b": 1},
                 status=200, sse_lines=["data: [DONE]"])
    manifest = store.finalize(endpoints=["/v1/chat/completions"])
    text = format_receipt(manifest)
    assert str(manifest) in text
    digest = __import__("hashlib").sha256(manifest.read_bytes()).hexdigest()
    assert digest in text
    assert "/v1/chat/completions" in text


def test_replay_miss_aborts_by_name_and_never_falls_through(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fleet, "provider_type", lambda p: "openai-compat")
    _write_tape(tmp_path, "https://gate.example/v1/chat/completions",
                {"model": "m", "messages": [], "stream": True})
    factory, _ = make_tape_factory(replay_dir=tmp_path)
    adapter = factory("tapeprov/m1")
    monkeypatch.setattr(
        "hr.adapters.openai_compat.requests.post",
        lambda *a, **k: pytest.fail("LIVE CALL DURING REPLAY"),
    )
    with pytest.raises(SystemExit) as exit_:
        adapter._post("https://gate.example/v1/chat/completions",
                      headers={}, json={"different": "request"}, stream=True)
    assert exit_.value.code == 2
    err = capsys.readouterr().err
    assert "tape replay aborted" in err and "re-record procedure" in err


# --- CLI surface ------------------------------------------------------------- #


def _runner():
    from typer.testing import CliRunner

    from hr.cli import app

    return CliRunner(), app


def test_bench_help_advertises_both_tape_flags():
    runner, app = _runner()
    res = runner.invoke(app, ["bench", "--help"])
    assert res.exit_code == 0
    assert "--tape-record" in res.output and "--tape-replay" in res.output


def test_bench_tape_flags_mutually_exclusive_exit2(tmp_path):
    runner, app = _runner()
    res = runner.invoke(
        app,
        ["bench", "--models", "x/y", "--tape-record", str(tmp_path / "a"),
         "--tape-replay", str(tmp_path / "b")],
    )
    assert res.exit_code == 2
    assert "mutually exclusive" in (res.output + getattr(res, "stderr", ""))


def test_preflight_replay_without_manifest_fails_closed_before_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HR_HOME", str(tmp_path))  # isolate from real config
    runner, app = _runner()
    res = runner.invoke(
        app, ["preflight", "--tape-replay", str(tmp_path / "absent")]
    )
    assert res.exit_code == 2
    assert "fail-closed" in res.output
