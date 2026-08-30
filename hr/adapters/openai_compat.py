from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import requests

from hr.graders.base import ModelResponse
from hr.scheduler.taxonomy import classify_failure, retryable

from .base import Adapter, AdapterError, Capabilities
from .fleet import resolve_capabilities
from .openai_endpoint import (
    Endpoint as _Endpoint,
    provider_entry as _provider_entry,
    provider_for as _provider_for,
    resolve_endpoint as _resolve_endpoint_impl,
)
from .openai_protocol import (
    build_tools_payload as _build_tools_payload,
    extract_int as _extract_int,
    parse_sse_stream as _parse_sse_stream,
    thinking_budget_to_effort as _thinking_budget_to_effort,
    to_messages as _to_oai_messages,
)


log = logging.getLogger(__name__)

DEFAULT_CONFIG = os.path.expanduser("~/.cache/opencode/models.json")
DEFAULT_AUTH = os.path.expanduser("~/.local/share/opencode/auth.json")
MAX_RETRIES = 6


def _resolve_endpoint(
    model_id: str,
    *,
    config_path: str = DEFAULT_CONFIG,
    auth_path: str = DEFAULT_AUTH,
    base_url_override: str | None = None,
) -> _Endpoint:
    return _resolve_endpoint_impl(
        model_id,
        config_path=config_path,
        auth_path=auth_path,
        base_url_override=base_url_override,
    )


class OpenAICompatAdapter(Adapter):
    def __init__(
        self,
        opencode_config_path: str = DEFAULT_CONFIG,
        auth_json_path: str = DEFAULT_AUTH,
        base_url_override: str | None = None,
    ) -> None:
        self._config_path = opencode_config_path
        self._auth_path = auth_json_path
        self._base_override = base_url_override
        self._endpoint_cache: dict[str, _Endpoint] = {}

    def list_models(self) -> list[str]:
        try:
            with open(self._config_path, encoding="utf-8") as config_file:
                config = json.load(config_file)
        except (OSError, json.JSONDecodeError):
            return []
        nested = config.get("providers")
        providers = nested if isinstance(nested, dict) else config
        models: list[str] = []
        for provider_name, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            provider_models = entry.get("models") or {}
            if not isinstance(provider_models, dict):
                continue
            models.extend(f"{provider_name}/{slug}" for slug in provider_models)
        return models

    def probe_capabilities(self, model_id: str) -> Capabilities:
        provider = _provider_for(model_id)
        endpoint = self._resolve_endpoint_cached(model_id)
        slug = model_id.split("/", 1)[1] if "/" in model_id else model_id
        overlay = resolve_capabilities(model_id)
        return Capabilities(
            model_id=model_id,
            provider=provider,
            api_base_url=endpoint.url,
            supports_thinking=overlay["thinking"],
            supports_vision=overlay["vision"],
            extra=self._read_model_extra(provider, slug),
        )

    def chat(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        images: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        thinking_budget: int | None = None,
        max_output: int = 16_384,
        timeout_s: int = 600,
    ) -> ModelResponse:
        endpoint = self._resolve_endpoint_cached(model_id)
        slug = model_id.split("/", 1)[1] if "/" in model_id else model_id
        body: dict[str, Any] = {
            "model": slug,
            "messages": _to_oai_messages(messages, images),
            "max_tokens": max_output,
            "stream": True,
        }
        effort = _thinking_budget_to_effort(thinking_budget)
        if effort:
            body["reasoning_effort"] = effort
        tools_payload = _build_tools_payload(tools)
        if tools_payload:
            body["tools"] = tools_payload

        for attempt in range(1, MAX_RETRIES + 1):
            started_at = time.monotonic()
            try:
                response = requests.post(
                    endpoint.url,
                    headers=endpoint.headers,
                    json=body,
                    stream=True,
                    timeout=timeout_s,
                )
            except requests.exceptions.Timeout as exc:
                failure = classify_failure(timed_out=True)
                if attempt < MAX_RETRIES and retryable(failure.code):
                    time.sleep(min(30.0, 2 ** (attempt - 1)))
                    continue
                raise AdapterError(
                    f"Timeout calling {model_id}: {exc}",
                    failure=failure,
                    status_code=None,
                ) from exc
            except requests.exceptions.ConnectionError as exc:
                failure = classify_failure(status_code=0, error_message=str(exc))
                if attempt < MAX_RETRIES and retryable(failure.code):
                    time.sleep(min(30.0, 2 ** (attempt - 1)))
                    continue
                raise AdapterError(
                    f"Connection error calling {model_id}: {exc}",
                    failure=failure,
                    status_code=0,
                ) from exc

            status = response.status_code
            latency = int((time.monotonic() - started_at) * 1000)
            if status == 429 or 500 <= status < 600:
                snippet = response.text[:300] if response.text else ""
                failure = classify_failure(status_code=status, error_message=snippet)
                response.close()
                if attempt < MAX_RETRIES and retryable(failure.code):
                    log.warning(
                        "[openai_compat] %s attempt %d: %d %s",
                        model_id,
                        attempt,
                        status,
                        snippet[:120],
                    )
                    time.sleep(min(30.0, 2 ** (attempt - 1)))
                    continue
                raise AdapterError(
                    f"{model_id} returned {status}: {snippet}",
                    failure=failure,
                    status_code=status,
                )
            if status >= 400:
                snippet = response.text[:500] if response.text else ""
                failure = classify_failure(status_code=status, error_message=snippet)
                response.close()
                raise AdapterError(
                    f"{model_id} returned {status}: {snippet}",
                    failure=failure,
                    status_code=status,
                )

            try:
                accumulator = _parse_sse_stream(response)
            finally:
                response.close()
            text, thinking, tool_calls, usage = accumulator.finalize()
            if not text and not tool_calls and not thinking:
                failure = classify_failure(
                    status_code=status,
                    empty_body=True,
                    error_message="Empty response from model",
                )
                if attempt < MAX_RETRIES and retryable(failure.code):
                    time.sleep(min(30.0, 2 ** (attempt - 1)))
                    continue
                log.warning(
                    "[openai_compat] %s empty response (latency=%dms, usage=%s)",
                    model_id,
                    latency,
                    usage,
                )

            tokens_out = _extract_int(usage, "completion_tokens")
            details = usage.get("completion_tokens_details")
            if isinstance(details, dict):
                tokens_out += _extract_int(details, "reasoning_tokens")
            return ModelResponse(
                text=text,
                thinking=thinking,
                tool_calls=tool_calls,
                raw={"usage": usage},
                latency_ms=latency,
                tokens_in=_extract_int(usage, "prompt_tokens"),
                tokens_out=tokens_out,
            )

        raise AssertionError("retry loop exhausted without returning or raising")

    def _resolve_endpoint_cached(self, model_id: str) -> _Endpoint:
        if model_id not in self._endpoint_cache:
            self._endpoint_cache[model_id] = _resolve_endpoint(
                model_id,
                config_path=self._config_path,
                auth_path=self._auth_path,
                base_url_override=self._base_override,
            )
        return self._endpoint_cache[model_id]

    def _read_model_extra(self, provider: str, slug: str) -> dict[str, Any]:
        try:
            with open(self._config_path, encoding="utf-8") as config_file:
                config = json.load(config_file)
        except (OSError, json.JSONDecodeError):
            return {}
        models = (_provider_entry(config, provider) or {}).get("models")
        if not isinstance(models, dict):
            return {}
        model_entry = models.get(slug)
        if not isinstance(model_entry, dict):
            return {}
        field_names = {
            "contextWindow": "context_window",
            "maxOutputTokens": "max_output_tokens",
            "reasoning_options": "reasoning_options",
        }
        return {
            target: model_entry[source]
            for source, target in field_names.items()
            if model_entry.get(source)
        }
