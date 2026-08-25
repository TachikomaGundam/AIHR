from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

import requests


log = logging.getLogger(__name__)


def to_messages(
    messages: list[dict[str, Any]],
    images: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    out = [
        {"role": message["role"], "content": message.get("content") or ""}
        for message in messages
    ]
    if not images:
        return out
    user_index = next(
        (
            index
            for index in range(len(out) - 1, -1, -1)
            if out[index]["role"] == "user"
        ),
        -1,
    )
    if user_index < 0:
        return out
    existing = out[user_index]["content"]
    content: list[dict[str, Any]] = (
        [dict(part) for part in existing if isinstance(part, dict)]
        if isinstance(existing, list)
        else [{"type": "text", "text": str(existing)}]
    )
    content.extend(
        {
            "type": "image_url",
            "image_url": {
                "url": "data:"
                f"{image.get('media_type', 'image/png')};base64,{image['data']}"
            },
        }
        for image in images
    )
    out[user_index]["content"] = content
    return out


def build_tools_payload(tools: list[dict[str, Any]] | None) -> list[dict] | None:
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema")
                or tool.get("schema")
                or {"type": "object", "properties": {}},
            },
        }
        for tool in tools
    ]


def _strip_prefix(line: str, prefix: str) -> str | None:
    if line == prefix:
        return ""
    if line.startswith(prefix + " "):
        return line[len(prefix) + 1 :]
    if line.startswith(prefix):
        return line[len(prefix) :]
    return None


def _iter_sse_lines(response: requests.Response) -> Iterable[str]:
    for raw in response.iter_lines(decode_unicode=True):
        if raw is None:
            continue
        line = raw.rstrip("\r\n")
        if line.startswith("event:"):
            continue
        payload = _strip_prefix(line, "data:")
        if payload is not None:
            yield payload


@dataclass
class StreamAccumulator:
    text_parts: list[str] = field(default_factory=list)
    thinking_parts: list[str] = field(default_factory=list)
    tool_calls: dict[int, dict[str, Any]] = field(default_factory=dict)
    last_usage: dict[str, int] | None = None

    def apply_delta(self, delta: dict) -> None:
        reasoning = delta.get("reasoning_content")
        if reasoning:
            self.thinking_parts.append(reasoning)
        content = delta.get("content")
        if content:
            self.text_parts.append(content)
        for tool_call in delta.get("tool_calls") or []:
            index = tool_call.get("index", 0)
            entry = self.tool_calls.setdefault(
                index, {"id": None, "name": "", "arg_parts": []}
            )
            function = tool_call.get("function", {})
            if tool_call.get("id"):
                entry["id"] = tool_call["id"]
            if function.get("name"):
                entry["name"] += function["name"]
            if function.get("arguments"):
                entry["arg_parts"].append(function["arguments"])

    def finalize(self) -> tuple[str, str, list[dict], dict]:
        tool_calls: list[dict] = []
        for entry in dict(sorted(self.tool_calls.items())).values():
            argument_text = "".join(entry["arg_parts"]).strip()
            try:
                arguments = json.loads(argument_text) if argument_text else {}
            except json.JSONDecodeError:
                log.warning("Failed to parse tool_arguments JSON: %r", argument_text[:300])
                arguments = {}
            tool_calls.append({"name": entry["name"], "input": arguments})
        return (
            "".join(self.text_parts),
            "".join(self.thinking_parts),
            tool_calls,
            self.last_usage or {},
        )


def parse_sse_stream(response: requests.Response) -> StreamAccumulator:
    accumulator = StreamAccumulator()
    for payload in _iter_sse_lines(response):
        if payload == "[DONE]":
            break
        payload = payload.strip()
        if not payload:
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            log.warning("Skipping malformed SSE chunk: %r", payload[:200])
            continue
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta")
            if delta:
                accumulator.apply_delta(delta)
        usage = chunk.get("usage")
        if usage:
            accumulator.last_usage = usage
    return accumulator


def thinking_budget_to_effort(thinking_budget: int | None) -> str | None:
    if thinking_budget is None:
        return None
    if thinking_budget < 1000:
        return "low"
    if thinking_budget <= 16_384:
        return "high"
    return "max"


def extract_int(values: dict | None, key: str) -> int:
    if not isinstance(values, dict):
        return 0
    value = values.get(key)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    return 0
