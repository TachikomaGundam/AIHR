"""anthropic wire stop_reason -> finish_reason mapping (0.6.6 P5 preflight fix)."""
from __future__ import annotations

import json
from contextlib import contextmanager

from hr.adapters.anthropic_stream import decode_stream
from hr.adapters.base import AdapterError


class _FakeStream:
    def __init__(self, lines: list[str], status: int = 200):
        self._lines = lines
        self.status_code = status
        self.text = ""

    def iter_lines(self):
        return iter(self._lines)

    def read(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, lines: list[str]):
        self._lines = lines

    @contextmanager
    def stream(self, method, url, **kw):
        yield _FakeStream(self._lines)


def _sse(events: list[tuple[str, dict]]) -> list[str]:
    out: list[str] = []
    for name, payload in events:
        out.append(f"event: {name}")
        out.append(f"data: {json.dumps(payload)}")
        out.append("")
    return out


def _decode(events: list[tuple[str, dict]]):
    lines = _sse(events)
    client = _FakeClient(lines)
    return decode_stream(client, url="https://x/v1/messages", headers={},
                         body={}, model_id="prov/m", timeout_s=5)


def _base_events(stop_reason: str | None) -> list[tuple[str, dict]]:
    events = [
        ("message_start", {"type": "message_start",
                           "message": {"usage": {"input_tokens": 10}}}),
        ("content_block_delta", {"type": "content_block_delta",
                                 "delta": {"type": "text_delta", "text": "T1: 2"}}),
    ]
    delta: dict = {"usage": {"output_tokens": 42}}
    if stop_reason is not None:
        delta["delta"] = {"stop_reason": stop_reason}
    events.append(("message_delta", {"type": "message_delta", **delta}))
    return events


def test_max_tokens_maps_to_length():
    assert _decode(_base_events("max_tokens")).finish_reason == "length"


def test_end_turn_maps_to_stop():
    assert _decode(_base_events("end_turn")).finish_reason == "stop"


def test_stop_sequence_maps_to_stop():
    assert _decode(_base_events("stop_sequence")).finish_reason == "stop"


def test_unknown_stop_reason_passes_through_verbatim():
    assert _decode(_base_events("refusal")).finish_reason == "refusal"


def test_absent_stop_reason_stays_none():
    assert _decode(_base_events(None)).finish_reason is None


def test_token_counts_unaffected():
    r = _decode(_base_events("end_turn"))
    assert (r.tokens_in, r.tokens_out, r.text) == (10, 42, "T1: 2")
