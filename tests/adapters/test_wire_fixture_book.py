"""S1a wire-level fixture book: raw wire frames -> the REAL parsers -> closed
expected values (SIM-LADDER v2, owner order 2026-10-16).

The 0.6.5/0.6.7 lesson: fake adapters can carry a field end to end while the
actual stream parsers drop it. These fixtures are literal wire text; the only
things under test here are the production parsers themselves. Expected values
are pinned in WIRE_FIXTURES (closed enumeration) — a parser change that loses
a stop cause fails here, not on a customer testbed at 3am.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, cast

from hr.adapters.anthropic_stream import decode_stream
from hr.adapters.openai_protocol import parse_sse_stream

# --- closed fixture book ----------------------------------------------------

WIRE_FIXTURES: dict[str, dict[str, Any]] = {
    # openai-compatible wire: the chunk carries the stop cause explicitly.
    "W1_openai_length": {
        "wire": [
            'data: {"choices":[{"delta":{"content":"42"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
            "data: [DONE]",
        ],
        "expect_finish": "length",
    },
    # openai-compatible wire: NO finish_reason anywhere (old anthropic-style
    # defect on this wire) -> None, the ENV-UNKNOWN-STOP chain's origin.
    "W2_openai_missing": {
        "wire": [
            'data: {"choices":[{"delta":{"content":"42"}}]}',
            "data: [DONE]",
        ],
        "expect_finish": None,
    },
    # openai-compatible wire: a provider stop cause outside the classic trio
    # must pass through VERBATIM (downstream names it, never guesses).
    "W3_openai_content_filter": {
        "wire": [
            'data: {"choices":[{"delta":{"content":"x"},"finish_reason":"content_filter"}]}',
            "data: [DONE]",
        ],
        "expect_finish": "content_filter",
    },
}

ANTHROPIC_WIRE: dict[str, Any] = {
    # anthropic wire: an unmapped stop_reason passes through verbatim
    # (documented behavior of _STOP_REASON_MAP.get).
    "W4_anthropic_refusal_verbatim": {
        "stop_reason": "refusal",
        "expect_finish": "refusal",
    },
    # absent stop_reason (delta omits the field) -> None, never a guessed "stop".
    "W5_anthropic_absent": {
        "stop_reason": None,
        "expect_finish": None,
    },
}


# --- openai-compatible side --------------------------------------------------


class _FakeResponse:
    """Feeds literal wire lines to _iter_sse_lines(iter_lines contract)."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def iter_lines(self, decode_unicode: bool = True):  # noqa: FBT001,FBT002
        return iter(self._lines)


def _parse_openai(lines: list[str]) -> str | None:
    return parse_sse_stream(cast(Any, _FakeResponse(lines))).finish_reason


def test_wire_fixture_book_openai_scenarios() -> None:
    for name, fixture in WIRE_FIXTURES.items():
        assert _parse_openai(fixture["wire"]) == fixture["expect_finish"], name


def test_wire_fixture_book_is_closed_enumeration() -> None:
    # every scenario names its wire and expectation; no runtime-computed truth
    for fixture in (*WIRE_FIXTURES.values(), *ANTHROPIC_WIRE.values()):
        assert isinstance(fixture, dict) and "expect_finish" in fixture


# --- anthropic side -----------------------------------------------------------


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.status_code = 200
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
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    @contextmanager
    def stream(self, method, url, **kw):  # type: ignore[no-untyped-def]
        yield _FakeStream(self._lines)


def _anthropic_events(stop_reason: str | None) -> list[tuple[str, dict]]:
    delta: dict = {}
    if stop_reason is not None:
        delta["stop_reason"] = stop_reason
    return [
        ("message_start", {"type": "message_start",
                           "message": {"usage": {"input_tokens": 3}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "hi"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": delta,
                           "usage": {"output_tokens": 5}}),
        ("message_stop", {"type": "message_stop"}),
    ]


def _decode_anthropic(stop_reason: str | None):
    lines: list[str] = []
    for name, payload in _anthropic_events(stop_reason):
        lines.append(f"event: {name}")
        lines.append(f"data: {json.dumps(payload)}")
        lines.append("")
    return decode_stream(_FakeClient(lines), url="https://x/v1/messages",  # type: ignore[arg-type]
                         headers={}, body={}, model_id="prov/m", timeout_s=5)


def test_wire_fixture_book_anthropic_scenarios() -> None:
    for name, fixture in ANTHROPIC_WIRE.items():
        out = _decode_anthropic(fixture["stop_reason"])
        assert out.finish_reason == fixture["expect_finish"], name
