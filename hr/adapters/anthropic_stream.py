from __future__ import annotations

import json
import time
from typing import Any

import httpx

from hr.adapters.base import AdapterError
from hr.graders.base import ModelResponse
from hr.scheduler.taxonomy import classify_failure


def decode_stream(
    client: Any,
    *,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    model_id: str,
    timeout_s: int,
) -> ModelResponse:
    started_at = time.perf_counter()
    input_tokens = 0
    output_tokens = 0
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    open_tool: dict[str, Any] | None = None
    current_event: str | None = None
    data_lines: list[str] = []

    def close_tool() -> None:
        nonlocal open_tool
        if open_tool is None:
            return
        raw_json = "".join(open_tool.pop("_json_parts", []))
        try:
            open_tool["input"] = json.loads(raw_json) if raw_json else {}
        except (json.JSONDecodeError, ValueError):
            open_tool["input"] = {"_raw": raw_json}
        tool_calls.append(open_tool)
        open_tool = None

    def flush() -> None:
        nonlocal input_tokens, output_tokens, open_tool
        if not data_lines:
            return
        payload = "\n".join(data_lines)
        data_lines.clear()
        try:
            event = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return
        match current_event:
            case "message_start":
                usage = (event.get("message") or {}).get("usage") or {}
                input_tokens = int(usage.get("input_tokens", 0) or 0)
            case "content_block_start":
                block = event.get("content_block") or {}
                if block.get("type") == "tool_use":
                    close_tool()
                    open_tool = {
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "_json_parts": [],
                    }
            case "content_block_delta":
                delta = event.get("delta") or {}
                match delta.get("type"):
                    case "text_delta":
                        if text := delta.get("text", ""):
                            text_parts.append(text)
                    case "thinking_delta":
                        if thinking := delta.get("thinking", ""):
                            thinking_parts.append(thinking)
                    case "input_json_delta":
                        if open_tool is not None:
                            open_tool["_json_parts"].append(
                                delta.get("partial_json", "")
                            )
                    case _:
                        pass
            case "content_block_stop":
                close_tool()
            case "message_delta":
                usage = event.get("usage") or {}
                output_tokens = int(usage.get("output_tokens", 0) or 0)
            case _:
                pass

    try:
        with client.stream(
            "POST",
            url,
            json=body,
            headers=headers,
            timeout=timeout_s,
        ) as response:
            status = response.status_code
            if status >= 400:
                response.read()
                error_body = response.text
                failure = classify_failure(
                    status_code=status,
                    error_message=error_body,
                )
                raise AdapterError(
                    f"HTTP {status} from {url}: {error_body[:200]}",
                    failure=failure,
                    status_code=status,
                )
            for line in response.iter_lines():
                event = strip_prefix(line, "event")
                if event is not None:
                    current_event = event.strip()
                    continue
                data = strip_prefix(line, "data")
                if data is not None:
                    data_lines.append(data)
                    continue
                if line == "":
                    flush()
            flush()
    except httpx.TimeoutException as exc:
        failure = classify_failure(timed_out=True, error_message=str(exc))
        raise AdapterError(
            f"timeout after {timeout_s}s for {model_id}",
            failure=failure,
        ) from exc
    except httpx.HTTPError as exc:
        failure = classify_failure(error_message=str(exc))
        raise AdapterError(
            f"HTTP error for {model_id}: {exc}",
            failure=failure,
        ) from exc

    close_tool()
    text = "".join(text_parts).strip()
    thinking = "".join(thinking_parts).strip()
    if not text and not thinking and not tool_calls and not output_tokens and not input_tokens:
        raise AdapterError(
            f"empty response from {model_id}",
            failure=classify_failure(empty_body=True),
        )
    return ModelResponse(
        text=text,
        thinking=thinking,
        tool_calls=tool_calls,
        raw=None,
        latency_ms=int((time.perf_counter() - started_at) * 1000),
        tokens_in=input_tokens,
        tokens_out=output_tokens,
    )


def strip_prefix(line: str, prefix: str) -> str | None:
    if line.startswith(prefix + ": "):
        return line[len(prefix) + 2 :]
    if line.startswith(prefix + ":"):
        return line[len(prefix) + 1 :]
    return None
