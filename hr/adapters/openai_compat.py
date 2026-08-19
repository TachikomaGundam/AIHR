"""Adapter for OpenAI-compatible chat completions APIs (DeepSeek-direct).

Implements the same Adapter protocol as AnthropicCompatAdapter but speaks
the OpenAI chat completions SSE format: POST {base}/chat/completions with
Authorization: Bearer <key>. Streaming uses ``data: {...}\n\n`` + ``data: [DONE]``.

DeepSeek V4 Flash emits ``reasoning_content`` (mapped to ModelResponse.thinking),
``content`` (mapped to text), and tool_calls in OpenAI format (function.name +
function.arguments JSON string → {name, input} with parsed JSON).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

import requests

from hr.graders.base import ModelResponse
from hr.scheduler.taxonomy import classify_failure, retryable

from .base import Adapter, AdapterError, Capabilities
from .fleet import VALID_TYPES, provider_type, resolve_capabilities

log = logging.getLogger(__name__)

DEFAULT_CONFIG = os.path.expanduser("~/.cache/opencode/models.json")
DEFAULT_AUTH = os.path.expanduser("~/.local/share/opencode/auth.json")
DEFAULT_TIMEOUT_S = 180.0
MAX_TOKENS_DEFAULT = 8192
MAX_RETRIES = 6


# --------------------------------------------------------------------------- #
# Provider / Endpoint resolution                                               #
# --------------------------------------------------------------------------- #


def _provider_for(model_id: str) -> str:
    """Resolve the provider key from a namespaced model_id — config-driven.

    The provider must be declared with type ``openai-compat`` in the fleet
    config; undeclared providers or wrong-wire declarations are explicit
    errors (no name heuristics, no fallthrough).
    """
    provider = model_id.split("/", 1)[0] if "/" in model_id else model_id
    wire = provider_type(provider)
    if wire is None:
        raise ValueError(
            f"no provider type configured for {provider!r} (from model_id "
            f"{model_id!r}); valid types: {', '.join(VALID_TYPES)} — declare "
            f"a 'wire_overrides:' entry in configs/fleet.yaml or add the "
            f"provider to the opencode config with a known 'npm'"
        )
    if wire != "openai-compat":
        raise ValueError(
            f"provider {provider!r} is typed {wire!r}, not 'openai-compat' "
            f"(from model_id {model_id!r}); valid types: "
            f"{', '.join(VALID_TYPES)}"
        )
    return provider


@dataclass(frozen=True)
class _Endpoint:
    provider: str
    url: str  # full chat/completions URL
    headers: dict[str, str]


def _resolve_endpoint(
    model_id: str,
    *,
    config_path: str = DEFAULT_CONFIG,
    auth_path: str = DEFAULT_AUTH,
    base_url_override: str | None = None,
) -> _Endpoint:
    """Resolve (url, headers) for an OpenAI-compatible provider.

    The base URL is resolved dynamically — never hardcoded — from, in order:

      1. ``base_url_override`` (explicit constructor flag);
      2. the ``api``/``baseURL`` entry for the provider in the opencode models
         cache (``config_path``);
      3. the opencode provider block ``options.baseURL`` (live opencode.jsonc);
      4. ``gateway_urls: {provider: ...}`` in configs/fleet.yaml.

    When nothing declares a base URL the resolution FAILS LOUD, naming every
    declaration site. The bearer token comes from auth.json (``auth_path``).
    """
    provider = _provider_for(model_id)

    # --- API base URL --------------------------------------------------- #
    if base_url_override:
        base = base_url_override
    else:
        base = _api_base_from_config(provider, config_path)
        if base is None:
            base = _api_base_from_opencode_config(provider)
        if base is None:
            base = _api_base_from_gateway_urls(provider)
        if base is None:
            raise AdapterError(
                f"No base URL declared for provider {provider!r} (from model "
                f"{model_id!r}). Declare it in one of: the provider block "
                f"'options.baseURL' in opencode.jsonc; 'api'/'baseURL' for "
                f"{provider!r} in the opencode models cache at {config_path}; "
                f"or 'gateway_urls:' for {provider!r} in configs/fleet.yaml"
            )

    url = base.rstrip("/") + "/chat/completions"

    # --- Auth ----------------------------------------------------------- #
    api_key = _api_key_from_auth(provider, auth_path)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    return _Endpoint(provider=provider, url=url, headers=headers)


def _api_base_from_config(provider: str, config_path: str) -> str | None:
    """Base URL from the opencode models cache (``api``/``baseURL``), if present.

    The cache is optional: None means "not declared here" and the caller moves
    on to the next declaration site. There is deliberately NO hardcoded
    fallback — the historic DEFAULT_BASE_URL deepseek special-case was removed.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    entry = _provider_entry(cfg, provider) or {}
    base = entry.get("api") or entry.get("baseURL")
    if not isinstance(base, str) or not base.strip():
        return None
    return base


def _api_base_from_opencode_config(provider: str) -> str | None:
    """Base URL from the live opencode.jsonc provider block ``options.baseURL``."""
    from hr import opencfg  # lazy: keep adapter import cycles out

    try:
        blocks = opencfg.read_providers()
    except Exception:
        # Missing/unreadable opencode config: the models cache and the
        # gateway_urls overlay may still declare the provider — keep the chain.
        return None
    opts = (blocks.get(provider) or {}).get("options") or {}
    base = opts.get("baseURL")
    if not isinstance(base, str) or not base.strip():
        return None
    return base


def _api_base_from_gateway_urls(provider: str) -> str | None:
    """Base URL from the configs/fleet.yaml ``gateway_urls`` overlay."""
    from hr import config as hr_config  # lazy: keep adapter import cycles out

    try:
        urls = hr_config.gateway_urls()
    except FileNotFoundError:
        # No fleet.yaml — the overlay declares nothing; earlier sources rule.
        return None
    base = urls.get(provider)
    if not isinstance(base, str) or not base.strip():
        return None
    return base


def _provider_entry(cfg: dict, provider: str) -> dict | None:
    """Resolve provider entry from either the nested or flat models.json layout.

    Two observed shapes:
      - nested: ``{"providers": {provider: {...}}}``
      - flat:   ``{provider: {api, models, ...}, ...}``
    """
    nested = (cfg.get("providers") or {}).get(provider)
    if nested:
        return nested
    top = cfg.get(provider)
    return top if isinstance(top, dict) else None


def _api_key_from_auth(provider: str, auth_path: str) -> str:
    try:
        with open(auth_path, "r", encoding="utf-8") as f:
            auth = json.load(f)
    except OSError as e:
        raise AdapterError(f"Cannot read auth.json at {auth_path}: {e}") from e

    # auth.json layout (observed): {provider_name: {type: ..., key: ...}, ...}
    entry = auth.get(provider)
    if not isinstance(entry, dict):
        raise AdapterError(f"No auth entry for provider '{provider}' in {auth_path}")
    key = entry.get("key")
    if not key or not isinstance(key, str):
        raise AdapterError(
            f"Auth entry for '{provider}' in {auth_path} has no 'key' field"
        )
    # Keys in auth.json sometimes carry trailing whitespace/newlines — strip
    # them so 'Bearer ...' headers don't contain invalid characters.
    return key.strip()


# --------------------------------------------------------------------------- #
# Message translation                                                          #
# --------------------------------------------------------------------------- #


def _to_oai_messages(messages: list[dict], images: list[str] | None = None) -> list[dict]:
    """Translate hr2 messages → OpenAI chat messages.

    hr2 format: {'role': 'system|user|assistant', 'content': '...'} with optional
    'images' list of base64 strings on user messages.
    """
    out: list[dict] = []
    for i, m in enumerate(messages):
        msg = {"role": m["role"], "content": m.get("content") or ""}
        # OpenAI text-only: ignore images (DeepSeek-direct is text-only for flash).
        out.append(msg)
    return out


def _build_tools_payload(tools: list[dict] | None) -> list[dict] | None:
    """Translate hr2 tool definitions → OpenAI function tools.

    hr2 tool fields: {'name', 'description' (optional), 'input_schema'|'schema'}
    OpenAI format: {'type':'function', 'function':{'name', 'description', 'parameters'}}
    """
    if not tools:
        return None
    oai_tools: list[dict] = []
    for t in tools:
        schema = t.get("input_schema") or t.get("schema") or {"type": "object", "properties": {}}
        oai_tools.append(
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": schema,
                },
            }
        )
    return oai_tools or None


# --------------------------------------------------------------------------- #
# SSE streaming parser                                                         #
# --------------------------------------------------------------------------- #


def _strip_prefix(line: str, prefix: str) -> str | None:
    """Strip a case-sensitive prefix, tolerating an optional single space."""
    if line == prefix:
        return ""
    if line.startswith(prefix + " "):
        return line[len(prefix) + 1 :]
    if line.startswith(prefix):
        return line[len(prefix) :]
    return None


def _iter_sse_lines(resp: requests.Response) -> Iterable[str]:
    """Yield raw SSE 'data:' payloads (after stripping the prefix)."""
    for raw in resp.iter_lines(decode_unicode=True):
        if raw is None:
            continue
        line = raw.rstrip("\r\n")
        # Drop event: lines — we only care about data:
        if line.startswith("event:"):
            continue
        payload = _strip_prefix(line, "data:")
        if payload is None:
            continue
        yield payload


@dataclass
class _StreamAccum:
    """Mutable accumulator for SSE chunks from OpenAI streaming."""

    text_parts: list[str] = field(default_factory=list)
    thinking_parts: list[str] = field(default_factory=list)
    tool_calls: dict[int, dict[str, Any]] = field(default_factory=dict)  # index -> {id, name, arg_parts}
    last_usage: dict[str, int] | None = None

    def apply_delta(self, delta: dict) -> None:
        # reasoning_content -> thinking
        rc = delta.get("reasoning_content")
        if rc:
            self.thinking_parts.append(rc)
        # content -> text
        c = delta.get("content")
        if c:
            self.text_parts.append(c)
        # tool_calls -> function name + arguments
        tcs = delta.get("tool_calls")
        if tcs:
            for tc in tcs:
                idx = tc.get("index", 0)
                entry = self.tool_calls.setdefault(idx, {"id": None, "name": "", "arg_parts": []})
                fn = tc.get("function", {})
                if tc.get("id"):
                    entry["id"] = tc["id"]
                if fn.get("name"):
                    entry["name"] += fn["name"]
                if fn.get("arguments"):
                    entry["arg_parts"].append(fn["arguments"])

    def apply_usage(self, usage: dict) -> None:
        if usage:
            self.last_usage = usage

    def finalize(self) -> tuple[str, str, list[dict], dict]:
        text = "".join(self.text_parts)
        thinking = "".join(self.thinking_parts)
        # Tool calls: parse JSON argument fragments
        out_tool_calls: list[dict] = []
        for _idx, entry in sorted(self.tool_calls.items()):
            arg_str = "".join(entry["arg_parts"]).strip()
            try:
                arguments = json.loads(arg_str) if arg_str else {}
            except json.JSONDecodeError:
                log.warning("Failed to parse tool_arguments JSON: %r", arg_str[:300])
                arguments = {}
            out_tool_calls.append({"name": entry["name"], "input": arguments})
        usage = self.last_usage or {}
        return text, thinking, out_tool_calls, usage


def _parse_sse_stream(resp: requests.Response) -> _StreamAccum:
    """Parse an OpenAI chat SSE stream into a StreamAccum.

    Format: ``data: {json}\n\n`` with ``data: [DONE]`` at end. Tolerates spacing
    variants and ignores ``event:`` lines.
    """
    acc = _StreamAccum()
    for payload in _iter_sse_lines(resp):
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
        choices = chunk.get("choices") or []
        for ch in choices:
            delta = ch.get("delta")
            if delta:
                acc.apply_delta(delta)
        usage = chunk.get("usage")
        if usage:
            acc.apply_usage(usage)
    return acc


# --------------------------------------------------------------------------- #
# Text extractor for non-streaming fallback                                    #
# --------------------------------------------------------------------------- #


def _parse_non_streaming(body: dict) -> tuple[str, str, list[dict], dict]:
    """Parse a non-streaming chat completion response body."""
    choices = body.get("choices") or []
    if not choices:
        return "", "", [], body.get("usage") or {}
    message = choices[0].get("message") or {}
    text = message.get("content") or ""
    thinking = message.get("reasoning_content") or ""
    tool_calls_raw = message.get("tool_calls") or []
    out_tool_calls: list[dict] = []
    for tc in tool_calls_raw:
        fn = tc.get("function") or {}
        arg_str = (fn.get("arguments") or "").strip()
        try:
            arguments = json.loads(arg_str) if arg_str else {}
        except json.JSONDecodeError:
            log.warning("Failed to parse tool_arguments JSON: %r", arg_str[:300])
            arguments = {}
        out_tool_calls.append({"name": fn.get("name") or "", "input": arguments})
    return text, thinking, out_tool_calls, body.get("usage") or {}


# --------------------------------------------------------------------------- #
# Reasoning effort mapping                                                     #
# --------------------------------------------------------------------------- #


_REASONING_EFFORT = ["low", "high", "max"]


def _thinking_budget_to_effort(thinking_budget: int | None) -> str | None:
    if thinking_budget is None:
        return None
    # Map thinking_budget (tokens) to effort level.
    # Boundary raised to <16000 so Anthropic-style budgets (8192, 16384) map to
    # 'high' rather than falling through to 'max' (which DeepSeek OpenAI-compat
    # interprets as unbounded thinking). Only truly huge budgets go to 'max'.
    if thinking_budget < 1000:
        return "low"
    if thinking_budget < 16000:
        return "high"
    return "max"


# --------------------------------------------------------------------------- #
# The adapter                                                                  #
# --------------------------------------------------------------------------- #


class OpenAICompatAdapter(Adapter):
    """OpenAI chat-completions adapter for DeepSeek-direct and friends."""

    def __init__(
        self,
        opencode_config_path: str = DEFAULT_CONFIG,
        auth_json_path: str = DEFAULT_AUTH,
        base_url_override: str | None = None,
    ) -> None:
        self._config_path = opencode_config_path
        self._auth_path = auth_json_path
        self._base_override = base_url_override
        # Cache resolved endpoints for speed across many calls.
        self._endpoint_cache: dict[str, _Endpoint] = {}

    # --- Adapter protocol ------------------------------------------------- #

    def list_models(self) -> list[str]:
        # Introspection is best-effort; probe_capabilities returns what we know.
        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except OSError:
            return []
        providers = cfg.get("providers", {})
        out: list[str] = []
        for provider_name, entry in providers.items():
            models = entry.get("models") or {}
            for slug in models.keys():
                out.append(f"{provider_name}/{slug}")
        return out

    def probe_capabilities(self, model_id: str) -> Capabilities:
        provider = _provider_for(model_id)
        endpoint = self._resolve_endpoint_cached(model_id)
        slug = model_id.split("/", 1)[1] if "/" in model_id else model_id

        # Thinking/vision come from the capability overlay in the fleet config;
        # models.json only supplies size-ish extras (context window, limits).
        overlay = resolve_capabilities(model_id)
        extra = self._read_model_extra(provider, slug)

        return Capabilities(
            model_id=model_id,
            provider=provider,
            api_base_url=endpoint.url,
            supports_thinking=overlay["thinking"],
            supports_vision=overlay["vision"],
            extra=extra,
        )

    def chat(  # type: ignore[override]
        self,
        model_id: str,
        messages: list[dict],
        *,
        images: list[str] | None = None,
        tools: list[dict] | None = None,
        thinking_budget: int | None = None,
        max_output: int | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> ModelResponse:
        endpoint = self._resolve_endpoint_cached(model_id)
        slug = model_id.split("/", 1)[1] if "/" in model_id else model_id
        oai_messages = _to_oai_messages(messages, images)

        body: dict[str, Any] = {
            "model": slug,
            "messages": oai_messages,
            "max_tokens": max_output or MAX_TOKENS_DEFAULT,
            "stream": True,
        }
        effort = _thinking_budget_to_effort(thinking_budget)
        if effort:
            body["reasoning_effort"] = effort
        oai_tools = _build_tools_payload(tools)
        if oai_tools:
            body["tools"] = oai_tools

        last_err: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            t0 = time.monotonic()
            try:
                resp = requests.post(
                    endpoint.url,
                    headers=endpoint.headers,
                    json=body,
                    stream=True,
                    timeout=timeout_s,
                )
            except requests.exceptions.Timeout as e:
                last_err = e
                desc = classify_failure(timed_out=True)
                if attempt < MAX_RETRIES and retryable(desc.code):
                    time.sleep(min(30.0, 2 ** (attempt - 1)))
                    continue
                raise AdapterError(
                    f"Timeout calling {model_id}: {e}",
                    failure=desc,
                    status_code=None,
                ) from e
            except requests.exceptions.ConnectionError as e:
                last_err = e
                desc = classify_failure(
                    status_code=0, error_message=str(e)
                )
                if attempt < MAX_RETRIES and retryable(desc.code):
                    time.sleep(min(30.0, 2 ** (attempt - 1)))
                    continue
                raise AdapterError(
                    f"Connection error calling {model_id}: {e}",
                    failure=desc,
                    status_code=0,
                ) from e

            status = resp.status_code
            latency = int((time.monotonic() - t0) * 1000)

            # Retry on 429 / 5xx with backoff
            if status == 429 or (500 <= status < 600):
                snip = resp.text[:300] if resp.text else ""
                desc = classify_failure(status_code=status, error_message=snip)
                resp.close()
                if attempt < MAX_RETRIES and retryable(desc.code):
                    log.warning(
                        "[openai_compat] %s attempt %d: %d %s",
                        model_id,
                        attempt,
                        status,
                        snip[:120],
                    )
                    time.sleep(min(30.0, 2 ** (attempt - 1)))
                    continue
                raise AdapterError(
                    f"{model_id} returned {status}: {snip}",
                    failure=desc,
                    status_code=status,
                )

            # Non-retryable HTTP failure
            if status >= 400:
                snip = resp.text[:500] if resp.text else ""
                desc = classify_failure(status_code=status, error_message=snip)
                resp.close()
                raise AdapterError(
                    f"{model_id} returned {status}: {snip}",
                    failure=desc,
                    status_code=status,
                )

            # Parse the stream
            try:
                acc = _parse_sse_stream(resp)
            finally:
                resp.close()

            text, thinking, tool_calls, usage = acc.finalize()

            if not text and not tool_calls and not thinking:
                desc = classify_failure(
                    status_code=status, empty_body=True,
                    error_message="Empty response from model (no text/thinking/tool_calls)",
                )
                if attempt < MAX_RETRIES and retryable(desc.code):
                    time.sleep(min(30.0, 2 ** (attempt - 1)))
                    continue
                # Return the empty result at the last attempt; let the runner log it.
                log.warning(
                    "[openai_compat] %s empty response (latency=%dms, usage=%s)",
                    model_id,
                    latency,
                    usage,
                )

            tokens_in = _extract_int(usage, "prompt_tokens")
            tokens_out = _extract_int(usage, "completion_tokens")
            # reasoning tokens nested under completion_tokens_details.reasoning_tokens
            details = usage.get("completion_tokens_details") if isinstance(usage, dict) else None
            if isinstance(details, dict):
                rt = _extract_int(details, "reasoning_tokens")
                if rt:
                    tokens_out += rt

            return ModelResponse(
                text=text,
                thinking=thinking,
                tool_calls=tool_calls,
                raw={"usage": usage},
                latency_ms=latency,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )

        # Should not be reached, but defensive:
        raise AdapterError(
            f"{model_id} exhausted retries ({last_err})",
            failure=classify_failure(
                error_message=str(last_err) if last_err else None
            ),
            status_code=None,
        )

    # --- Helpers ---------------------------------------------------------- #

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
        """Read size-ish extras (context_window, max_output_tokens, reasoning_options)
        from models.json for (provider, slug); think/vision live in the overlay."""
        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except OSError:
            return {}
        entry = _provider_entry(cfg, provider) or {}
        # Some cache layouts nest models under a 'models' sub-key; others keep
        # them directly under the provider map.
        models = entry.get("models")
        if not isinstance(models, dict):
            return {}
        model_entry = models.get(slug)
        if not isinstance(model_entry, dict):
            return {}
        extra: dict[str, Any] = {}
        if model_entry.get("contextWindow"):
            extra["context_window"] = model_entry["contextWindow"]
        if model_entry.get("maxOutputTokens"):
            extra["max_output_tokens"] = model_entry["maxOutputTokens"]
        if model_entry.get("reasoning_options"):
            extra["reasoning_options"] = model_entry["reasoning_options"]
        return extra


def _extract_int(d: dict | None, key: str) -> int:
    if not isinstance(d, dict):
        return 0
    v = d.get(key)
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    return 0
