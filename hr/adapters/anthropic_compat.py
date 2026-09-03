"""Adapter for Anthropic Messages-compatible gateways.

Wraps every provider whose fleet wire type is ``anthropic-compat``
(``x-api-key`` + ``anthropic-version`` gateways). Provider routing, gateway
base URL and API key resolution are DATA-driven via :mod:`hr.config`
(opencode provider block ``options`` -> ``configs/fleet.yaml``
``gateway_urls`` -> ``auth.json``); endpoints are read from config files
(Wiki exit: constructor-injected paths win, tests inject fakes). No
provider-name literals live in this module.

Streaming SSE is mandatory — the Alibaba gateway hard-504s non-streaming
thinking calls at ~300s. v1's SSE parser tolerates both ``event:`` and
``event: `` forms (the Alibaba gateway omits the space).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hr import config
from hr.adapters.base import AdapterError, Capabilities
from hr.adapters.fleet import resolve_capabilities
from hr.adapters.anthropic_messages import attach_images
from hr.adapters.anthropic_stream import decode_stream
from hr.graders.base import ModelResponse
from hr.opencfg import strip_jsonc_comments as _strip_jsonc_comments
from hr.scheduler.taxonomy import retryable


def _provider_for(model_id: str) -> str:
    """Provider = the namespaced prefix of the model id.

    Generic by construction: the prefix before ``/`` names the provider (a
    bare slug is its own provider key), mirroring the fleet router in
    :mod:`hr.adapters`. There are no provider-name heuristics here — which
    gateway, key and wire a provider needs is resolved from config.
    """
    return model_id.split("/", 1)[0] if "/" in model_id else model_id


@dataclass
class _Endpoint:
    provider: str
    url: str
    headers: dict[str, str]


class AnthropicCompatAdapter:
    """Concrete adapter for the two x-api-key + anthropic-version gateways."""

    def __init__(
        self,
        *,
        opencode_config_path: Path | str | None = None,
        auth_json_path: Path | str | None = None,
        max_retries: int = 3,
        http_client: Any | None = None,
        default_timeout_s: int = 600,
    ) -> None:
        #: Explicit config paths win (tests inject fakes); None defers every
        #: lookup to hr.config's environment-aware chain.
        self._opencode_config = (
            Path(opencode_config_path) if opencode_config_path is not None else None
        )
        self._auth_json = (
            Path(auth_json_path) if auth_json_path is not None else None
        )
        self._max_retries = max_retries
        self._default_timeout_s = default_timeout_s
        self._client = http_client  # lazily built
        self._capabilities_cache: dict[str, Capabilities] = {}

    # ------------------------------------------------------------------
    # Credential plumbing — config-driven, no provider-name special cases
    # ------------------------------------------------------------------
    def _read_opencode_provider(self, name: str) -> dict[str, Any]:
        path = self._opencode_config
        if path is None or not path.exists():
            return {}
        raw = path.read_text(encoding="utf-8")
        data = json.loads(_strip_jsonc_comments(raw))
        return data.get("provider", {}).get(name, {})

    def _read_auth_key(self, provider: str) -> str:
        # Injected-path only, v2-blind by design: constructor-injected auth
        # files are test fakes; the production path above resolves through
        # get_provider_config, which reads auth-v2.json first.
        path = self._auth_json
        if path is None or not path.exists():
            return ""
        raw = path.read_text(encoding="utf-8")
        entry = json.loads(raw).get(provider, {})
        key = entry.get("key", "") if isinstance(entry, dict) else ""
        return key.strip() if isinstance(key, str) else ""

    def _resolve_base_and_key(self, provider: str) -> tuple[str, str]:
        """Base URL + API key for ``provider``, data-driven.

        With constructor-injected config paths (tests), the options come
        from those files with the ``configs/fleet.yaml`` ``gateway_urls``
        fallback; otherwise :func:`hr.config.get_provider_config` runs its
        environment-aware chain (opencode block -> gateway_urls ->
        auth.json). No provider name is ever special-cased here.
        """
        if self._opencode_config is None and self._auth_json is None:
            try:
                resolved = config.get_provider_config(provider)
            except ValueError as exc:
                raise AdapterError(str(exc)) from exc
            return resolved.base_url, resolved.api_key

        base_url = ""
        api_key = ""
        if self._opencode_config is not None:
            block = self._read_opencode_provider(provider)
            options = block.get("options") or {}
            base_url = str(options.get("baseURL") or "")
            api_key = str(options.get("apiKey") or "")
        if not api_key and self._auth_json is not None:
            api_key = self._read_auth_key(provider)
        if not base_url:
            base_url = config.gateway_urls().get(provider, "")
        return base_url.rstrip("/"), api_key

    def _resolve_endpoint(self, model_id: str) -> _Endpoint:
        provider = _provider_for(model_id)
        base_url, api_key = self._resolve_base_and_key(provider)
        if not base_url or not api_key:
            missing = [
                label
                for label, value in (
                    ("base URL", base_url),
                    ("API key", api_key),
                )
                if not value
            ]
            raise AdapterError(
                f"provider {provider!r} has no configured {', '.join(missing)} "
                f"(for model {model_id!r}): declare options baseURL/apiKey in "
                "the opencode config provider block, a 'gateway_urls:' entry "
                "in configs/fleet.yaml, and/or an auth-v2.json/auth.json "
                "entry for the provider"
            )
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Accept": "text/event-stream",
        }
        return _Endpoint(
            provider=provider,
            url=f"{base_url}/messages",
            headers=headers,
        )

    # ------------------------------------------------------------------
    # Capability probing
    # ------------------------------------------------------------------
    def probe_capabilities(self, model_id: str) -> Capabilities:
        cached = self._capabilities_cache.get(model_id)
        if cached is not None:
            return cached
        provider = _provider_for(model_id)
        overlay = resolve_capabilities(model_id)
        endpoint = self._resolve_endpoint(model_id)
        cap = Capabilities(
            model_id=model_id,
            provider=provider,
            api_base_url=endpoint.url[: -len("/messages")],
            supports_thinking=overlay["thinking"],
            supports_vision=overlay["vision"],
        )
        self._capabilities_cache[model_id] = cap
        return cap

    # ------------------------------------------------------------------
    # Chat — streaming SSE call with retry + budget capture
    # ------------------------------------------------------------------
    def chat(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        images: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        thinking_budget: int | None = None,
        max_output: int = 16384,
        timeout_s: int = 600,
    ) -> ModelResponse:
        cap = self.probe_capabilities(model_id)
        if not cap.supports_thinking:
            thinking_budget = None

        if thinking_budget and max_output < 16384:
            max_output = max(max_output, 16384)

        messages = self._attach_images(messages, images)

        # The gateway expects the bare model slug (e.g. ``deepseek-v4-flash``);
        # The ``bailian-token-plan/`` prefix selects the deployment namespace.
        wire_model = (
            model_id.split("/", 1)[-1] if "/" in model_id else model_id
        )
        body: dict[str, Any] = {
            "model": wire_model,
            "max_tokens": max_output,
            "messages": messages,
            "stream": True,
        }
        if thinking_budget:
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": int(thinking_budget),
            }
        if tools:
            # Item banks describe tools as {"name", "schema"}; the Anthropic
            # Messages API wants {"name", "description", "input_schema"}.
            body["tools"] = [
                {
                    "name": t.get("name", ""),
                    "description": t.get("description")
                    or t.get("desc")
                    or f"Tool {t.get('name', '')}",
                    "input_schema": t.get("input_schema")
                    or t.get("schema")
                    or {"type": "object", "properties": {}},
                }
                for t in tools
            ]

        endpoint = self._resolve_endpoint(model_id)
        return self._run_with_retry(endpoint, body, model_id, timeout_s)

    # ------------------------------------------------------------------
    # Image attachment (Anthropic vision format)
    # ------------------------------------------------------------------
    @staticmethod
    def _attach_images(
        messages: list[dict[str, Any]],
        images: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        return attach_images(messages, images)

    # ------------------------------------------------------------------
    # Retry loop
    # ------------------------------------------------------------------
    def _get_client(self, timeout_s: int) -> Any:
        if self._client is None:
            import httpx
            self._client = httpx.Client(
                timeout=httpx.Timeout(timeout_s, connect=30.0),
            )
        return self._client

    def _run_with_retry(
        self,
        endpoint: _Endpoint,
        body: dict[str, Any],
        model_id: str,
        timeout_s: int,
    ) -> ModelResponse:
        last_err: AdapterError | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return self._do_stream(
                    endpoint, body, model_id, timeout_s
                )
            except AdapterError as e:
                last_err = e
                failure = e.failure
                if failure is not None and retryable(failure.code):
                    if attempt < self._max_retries:
                        _backoff(attempt)
                        continue
                raise
        if last_err is not None:
            raise last_err
        raise AdapterError(f"adapter exhausted retries for {model_id}")

    # ------------------------------------------------------------------
    # SSE stream parser (carried over from v1 benchmark._call_api)
    # ------------------------------------------------------------------
    def _do_stream(
        self,
        endpoint: _Endpoint,
        body: dict[str, Any],
        model_id: str,
        timeout_s: int,
    ) -> ModelResponse:
        return decode_stream(
            self._get_client(timeout_s),
            url=endpoint.url,
            headers=endpoint.headers,
            body=body,
            model_id=model_id,
            timeout_s=timeout_s,
        )


def _backoff(attempt: int) -> None:
    delay = min(30.0, 2 ** (attempt - 1))
    time.sleep(delay)


# Used by endpoint-resolution tests to expose the bare routing function.
__all__ = ["AnthropicCompatAdapter", "_provider_for", "_strip_jsonc_comments"]
