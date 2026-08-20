"""Offline unit tests for hr.adapters.openai_protocol (pure request shaping).

The module is a pure function library: message/tool payload shaping and
SSE-stream parsing. Everything here is exercised with in-memory stand-ins
for ``requests.Response`` — no network, no real endpoints.
"""

from __future__ import annotations

import json

import pytest

from hr.adapters.openai_protocol import (
    _strip_prefix,
    build_tools_payload,
    extract_int,
    parse_sse_stream,
    to_messages,
)


class FakeSSEResponse:
    """requests.Response stand-in exposing only iter_lines()."""

    def __init__(self, lines: list[str | None]) -> None:
        self._lines = lines

    def iter_lines(self, decode_unicode: bool = True) -> object:
        for line in self._lines:
            yield line


# ---------------------------------------------------------------------------
# to_messages: role/content mapping + image attachment
# ---------------------------------------------------------------------------
def test_to_messages_no_images_passes_roles_and_fills_missing_content() -> None:
    out = to_messages(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": None}]
    )
    assert out == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": ""}]


def test_to_messages_images_leave_messages_without_user_unchanged() -> None:
    messages = [{"role": "system", "content": "sys"}]
    assert to_messages(messages, images=[{"data": "AAA="}]) == messages


def test_to_messages_images_merge_into_existing_dict_content_parts() -> None:
    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
        }
    ]
    out = to_messages(messages, images=[{"data": "x"}])
    text_parts = [p for p in out[0]["content"] if p["type"] == "text"]
    assert [p["text"] for p in text_parts] == ["a", "b"]
    assert out[0]["content"][-1]["type"] == "image_url"


# ---------------------------------------------------------------------------
# build_tools_payload: function-call tool schema payload
# ---------------------------------------------------------------------------
def test_build_tools_payload_none_and_empty_mean_no_tools() -> None:
    assert build_tools_payload(None) is None
    assert build_tools_payload([]) is None


def test_build_tools_payload_description_default() -> None:
    payload = build_tools_payload([{"name": "calc"}])
    assert payload[0]["function"]["description"] == ""
    assert payload[0]["function"]["parameters"] == {"type": "object", "properties": {}}


def test_build_tools_payload_schema_fallback() -> None:
    payload = build_tools_payload([{"name": "calc", "schema": {"type": "object"}}])
    assert payload[0]["function"]["parameters"] == {"type": "object"}


# ---------------------------------------------------------------------------
# _strip_prefix / _iter_sse_lines: raw SSE line filtering
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# parse_sse_stream: full stream walk
# ---------------------------------------------------------------------------
def _stream(*chunks: object) -> list[str]:
    return [chunk if chunk == "[DONE]" else f"data: {json.dumps(chunk)}" for chunk in chunks]


# ---------------------------------------------------------------------------
# parse_sse_stream: full stream walk
# ---------------------------------------------------------------------------
def _stream(*chunks: object) -> list[str]:
    return [chunk if chunk == "[DONE]" else f"data: {json.dumps(chunk)}" for chunk in chunks]


def test_parse_sse_stream_survives_chunks_without_choices() -> None:
    response = FakeSSEResponse(_stream({"usage": {"completion_tokens": 9}}, {"foo": 1}))
    acc = parse_sse_stream(response)
    text, _, _, usage = acc.finalize()
    assert text == ""
    assert usage == {"completion_tokens": 9}


# ---------------------------------------------------------------------------
# thinking_budget_to_effort / extract_int
# ---------------------------------------------------------------------------
def test_extract_int_handles_missing_non_numeric_and_bool() -> None:
    assert extract_int(None, "k") == 0
    assert extract_int({}, "k") == 0
    assert extract_int({"k": "nope"}, "k") == 0
    assert extract_int({"k": True}, "k") == 1
    assert extract_int({"k": 42}, "k") == 42
    assert extract_int({"k": 4.9}, "k") == 4