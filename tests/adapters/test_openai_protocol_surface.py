"""OpenAI wire-protocol contract tests (committed surface).

Covers hr.adapters.openai_protocol: message/vision reshaping, tool
payload building, SSE stream parsing and accumulation, effort mapping,
and integer extraction. Offline and deterministic.
"""

from __future__ import annotations

import json

import pytest

from hr.adapters.openai_protocol import (
    StreamAccumulator,
    build_tools_payload,
    extract_int,
    parse_sse_stream,
    thinking_budget_to_effort,
    to_messages,
)


class _FakeStreamResponse:
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    def iter_lines(self, decode_unicode: bool = False) -> list[str]:  # noqa: ARG002
        return [c.decode() for c in self._chunks]


def test_to_messages_passthrough() -> None:
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": ""}]
    out = to_messages(msgs)
    assert out[0] == {"role": "user", "content": "hi"}
    assert out[1]["content"] == ""


def test_to_messages_attaches_image_to_last_user() -> None:
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "see this"},
    ]
    out = to_messages(
        msgs,
        images=[{"data": "QUJD", "media_type": "image/png"}],
    )
    assert out[0] == {"role": "system", "content": "sys"}
    content = out[1]["content"]
    assert content[0] == {"type": "text", "text": "see this"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == "data:image/png;base64,QUJD"


def test_to_messages_no_user_keeps_content() -> None:
    msgs = [{"role": "assistant", "content": "x"}]
    out = to_messages(msgs, images=[{"data": "A", "media_type": "image/png"}])
    assert out[0] == {"role": "assistant", "content": "x"}


def test_to_messages_list_content_preserved() -> None:
    msgs = [{"role": "user", "content": [{"type": "text", "text": "a"}]}]
    out = to_messages(msgs, images=[{"data": "B", "media_type": "image/png"}])
    assert out[0]["content"][0] == {"type": "text", "text": "a"}


def test_build_tools_payload() -> None:
    assert build_tools_payload(None) is None
    assert build_tools_payload([]) is None
    tools = [
        {"name": "calc", "description": "d", "input_schema": {"type": "object"}},
        {"name": "fetch", "schema": {"type": "object"}},
        {"name": "bare"},
    ]
    out = build_tools_payload(tools)
    assert out[0]["function"]["name"] == "calc"
    assert out[0]["function"]["parameters"] == {"type": "object"}
    assert out[1]["function"]["parameters"] == {"type": "object"}
    assert out[2]["function"]["parameters"] == {"type": "object", "properties": {}}


def test_stream_accumulator_finalize() -> None:
    acc = StreamAccumulator()
    acc.apply_delta({"content": "hel"})
    acc.apply_delta({"content": "lo"})
    acc.apply_delta({"reasoning_content": "think"})
    acc.apply_delta(
        {"tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "calc", "arguments": "{\"a\":"}}]}
    )
    acc.apply_delta(
        {"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}
    )
    text, thinking, calls, usage = acc.finalize()
    assert text == "hello"
    assert thinking == "think"
    assert calls == [{"name": "calc", "input": {"a": 1}}]
    assert usage == {}


def test_stream_accumulator_bad_json_arguments() -> None:
    acc = StreamAccumulator()
    acc.apply_delta({"tool_calls": [{"index": 0, "function": {"name": "x", "arguments": "{broken"}}]})
    _text, _think, calls, _usage = acc.finalize()
    assert calls == [{"name": "x", "input": {}}]


def test_parse_sse_stream_accumulates_and_stops() -> None:
    chunks = [
        f"data: {json.dumps({'choices': [{'delta': {'content': 'a'}}]})}",
        f"data: {json.dumps({'choices': [{'delta': {'content': 'b'}}], 'usage': {'total': 5}})}",
        "data: [DONE]",
        f"data: {json.dumps({'choices': [{'delta': {'content': 'ignored-after-done'}}]})}",
    ]
    resp = _FakeStreamResponse([c.encode() for c in chunks])
    acc = parse_sse_stream(resp)
    assert acc.text_parts == ["a", "b"]
    assert acc.last_usage == {"total": 5}


def test_parse_sse_stream_skips_events_and_malformed() -> None:
    chunks = [
        b"event: ping",
        b"data: ",
        b"data: {not json",
        b"data: {\"choices\":[{\"delta\":{\"content\":\"ok\"}}]}",
    ]
    resp = _FakeStreamResponse(chunks)
    acc = parse_sse_stream(resp)
    assert "".join(acc.text_parts) == "ok"


def test_parse_sse_stream_stops_at_done() -> None:
    resp = _FakeStreamResponse([b"data: [DONE]", b"data: {\"choices\":[]}"])
    acc = parse_sse_stream(resp)
    assert acc.text_parts == []


@pytest.mark.parametrize(
    ("budget", "expected"),
    [
        (None, None),
        (0, "low"),
        (999, "low"),
        (1000, "high"),
        (16384, "high"),
        (16385, "max"),
    ],
)
def test_thinking_budget_to_effort(budget: int | None, expected: str | None) -> None:
    assert thinking_budget_to_effort(budget) == expected


@pytest.mark.parametrize(
    ("values", "key", "expected"),
    [
        (None, "k", 0),
        ({}, "k", 0),
        ({"k": True}, "k", 1),
        ({"k": 5}, "k", 5),
        ({"k": 3.9}, "k", 3),
        ({"k": "str"}, "k", 0),
    ],
)
def test_extract_int(values: dict | None, key: str, expected: int) -> None:
    assert extract_int(values, key) == expected