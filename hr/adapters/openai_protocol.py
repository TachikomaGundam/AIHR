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
    out: list[dict[str, Any]] = []
    for message in messages:
        out.extend(_translate_message(message))
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


def _flatten_tool_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) and part.get("type") == "text" else ""
            for part in value
        )
    return json.dumps(value)


def _translate_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    role = message["role"]
    content = message.get("content") or ""
    if not isinstance(content, list):
        return [{"role": role, "content": content}]

    tool_calls: list[dict[str, Any]] = []
    tool_msgs: list[dict[str, Any]] = []
    kept: list[Any] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": part.get("id") or f"toolu_{len(tool_calls)}",
                    "type": "function",
                    "function": {
                        "name": part.get("name", ""),
                        "arguments": json.dumps(part.get("input") or {}),
                    },
                }
            )
        elif isinstance(part, dict) and part.get("type") == "tool_result":
            tool_msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": part.get("tool_use_id", ""),
                    "content": _flatten_tool_content(part.get("content")),
                }
            )
        else:
            kept.append(part)

    if tool_msgs:
        translated = [*tool_msgs]
        if kept:
            translated.append({"role": role, "content": kept})
        return translated
    if tool_calls:
        text_parts = [p for p in kept if isinstance(p, dict) and p.get("type") == "text"]
        translated = {"role": role}
        # OpenAI replay accepts a plain string next to tool_calls; keep the
        # part list only when non-text parts (images) ride along.
        translated["content"] = (
            "".join(p.get("text", "") for p in text_parts)
            if len(text_parts) == len(kept)
            else kept
        )
        translated["tool_calls"] = tool_calls
        return [translated]
    return [{"role": role, "content": kept}]


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
        # decode_unicode=True yields str lines at runtime; the requests stubs
        # type the union bytes|str, so narrow defensively (never fires).
        if not isinstance(raw, str):
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
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
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
        return "medium"
    return "xhigh"


def extract_int(values: dict | None, key: str) -> int:
    if not isinstance(values, dict):
        return 0
    value = values.get(key)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    return 0
