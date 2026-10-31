"""S2 tape transport tests: protocol conformance with the live parse chains."""

from __future__ import annotations

from pathlib import Path

import pytest

from hr.adapters.openai_protocol import parse_sse_stream
from hr.tape import TapeMissError, TapeReader, TapeStore
from hr.tape_transports import (
    RecordingRequestsPost,
    ReplayHttpxClient,
    replay_post,
)

HEADERS = {"authorization": "s", "content-type": "application/json"}
BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
SSE = [
    'data: {"choices":[{"delta":{"content":"A: 4"}}]}',
    "data: [DONE]",
]


def _taped(tmp_path: Path) -> TapeReader:
    store = TapeStore(tmp_path)
    store.record("POST", "https://gw.example/v1/chat/completions", HEADERS, BODY, 200, list(SSE))
    store.finalize()
    return TapeReader(tmp_path)


def test_replay_post_feeds_the_live_openai_parser(tmp_path: Path) -> None:
    """The point of wire-level replay: recorded lines walk the SAME parser."""
    reader = _taped(tmp_path)
    response = replay_post(reader)(
        "https://any.host/v1/chat/completions", headers=HEADERS, json=BODY, stream=True
    )
    accumulator = parse_sse_stream(response)
    assert "A: 4" in "".join(accumulator.text_parts)
    response.close()


def test_replay_post_miss_raises_without_network(tmp_path: Path) -> None:
    reader = _taped(tmp_path)
    with pytest.raises(TapeMissError):
        replay_post(reader)("https://h/v1/chat/completions", headers=HEADERS, json={"x": 1})


def test_replay_httpx_client_protocol(tmp_path: Path) -> None:
    reader = _taped(tmp_path)
    client = ReplayHttpxClient(reader)
    with client.stream(
        "POST", "https://x.example/v1/chat/completions", json=BODY, headers=HEADERS, timeout=5
    ) as response:
        assert response.status_code == 200
        assert response.read().startswith(b"data:")
        assert list(response.iter_lines()) == SSE


def test_recording_post_tees_stream_and_finalizes_on_close(tmp_path: Path) -> None:
    store = TapeStore(tmp_path)

    class _Inner:
        status_code = 200
        text = ""

        def iter_lines(self, decode_unicode: bool = False):
            return iter(SSE)

        def close(self) -> None:
            return None

    recording = RecordingRequestsPost(store, lambda *a, **k: _Inner())
    response = recording("https://gw/v1/chat/completions", headers=HEADERS, json=BODY)
    lines = list(response.iter_lines(decode_unicode=True))
    response.close()
    assert lines == SSE
    store.finalize()
    reader = TapeReader(tmp_path)  # what was streamed is what the tape holds
    assert reader.lookup("POST", "https://gw/v1/chat/completions", HEADERS, BODY).sse_lines == tuple(SSE)


def test_recording_error_branch_captures_body(tmp_path: Path) -> None:
    store = TapeStore(tmp_path)

    class _ErrInner:
        status_code = 429
        text = '{"error":"rate limited"}'

        def iter_lines(self, decode_unicode: bool = False):
            return iter(())

        def close(self) -> None:
            return None

    recording = RecordingRequestsPost(store, lambda *a, **k: _ErrInner())
    response = recording("https://gw/v1/chat", headers=HEADERS, json=BODY)
    assert response.text == '{"error":"rate limited"}'
    response.close()
    store.finalize()
    entry = TapeReader(tmp_path).lookup("POST", "https://gw/v1/chat", HEADERS, BODY)
    assert entry.status == 429
    assert "rate limited" in entry.sse_lines[0]
