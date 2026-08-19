"""hr2.adapters — concrete Adapter implementations.

Two concrete adapters are wired behind a provider→adapter router that is
DATA-driven: each provider's wire type (``openai-compat`` or
``anthropic-compat``) is DERIVED at runtime from the opencode config's
``npm`` field (explicit table in :mod:`hr.fleet`) plus the OPTIONAL
``wire_overrides:`` map in ``configs/fleet.yaml``. There are no model-name
heuristics and no implicit default wire.

- :class:`AnthropicCompatAdapter` — speaks Anthropic Messages API (bailian-token-plan,
  kimi-for-coding, and any other provider typed ``anthropic-compat``).
- :class:`OpenAICompatAdapter` — speaks the OpenAI chat-completions SSE format
  (deepseek-direct and any other provider typed ``openai-compat``).

Use :func:`adapter_for` to auto-route a model_id to the right adapter.
"""

from hr.adapters.base import (
    Adapter,
    AdapterError,
    Capabilities,
    ChatRequest,
)
from hr.adapters.anthropic_compat import AnthropicCompatAdapter
from hr.adapters.openai_compat import OpenAICompatAdapter
from hr.adapters import fleet

__all__ = [
    "Adapter",
    "AdapterError",
    "AnthropicCompatAdapter",
    "OpenAICompatAdapter",
    "Capabilities",
    "ChatRequest",
    "adapter_for",
]


# --------------------------------------------------------------------------- #
# Provider → Adapter router                                                    #
# --------------------------------------------------------------------------- #

_ANTHROPIC_ADAPTER: AnthropicCompatAdapter | None = None
_OPENAI_ADAPTER: OpenAICompatAdapter | None = None


def adapter_for(model_id: str) -> Adapter:
    """Route a model_id to the adapter whose wire matches the provider config.

    The provider is the namespace prefix of ``model_id`` (a bare slug is
    treated as its own provider key); its wire type comes from the fleet
    config — no name heuristics, no fallthrough:

    - provider type ``anthropic-compat`` → :class:`AnthropicCompatAdapter`
    - provider type ``openai-compat`` → :class:`OpenAICompatAdapter`
    - unknown provider or unknown type → :class:`ValueError` listing the
      valid types (``fleet.VALID_TYPES``).
    """
    global _ANTHROPIC_ADAPTER, _OPENAI_ADAPTER
    provider = model_id.split("/", 1)[0] if "/" in model_id else model_id
    wire = fleet.provider_type(provider)
    if wire is None:
        raise ValueError(
            f"no provider type configured for {provider!r} (from model_id "
            f"{model_id!r}); valid types: {', '.join(fleet.VALID_TYPES)} — "
            f"declare a 'wire_overrides:' entry in configs/fleet.yaml or "
            f"add the provider to the opencode config with a known 'npm'"
        )
    if wire == "anthropic-compat":
        if _ANTHROPIC_ADAPTER is None:
            _ANTHROPIC_ADAPTER = AnthropicCompatAdapter()
        return _ANTHROPIC_ADAPTER
    if wire == "openai-compat":
        if _OPENAI_ADAPTER is None:
            _OPENAI_ADAPTER = OpenAICompatAdapter()
        return _OPENAI_ADAPTER
    raise ValueError(
        f"unknown provider type {wire!r} for provider {provider!r} (from "
        f"model_id {model_id!r}); valid types: {', '.join(fleet.VALID_TYPES)}"
    )


# --------------------------------------------------------------------------- #
# Routed adapter (multi-provider pool)                                         #
# --------------------------------------------------------------------------- #


class RoutedAdapter(Adapter):
    """Thin Adapter that dispatches each call to the provider-specific adapter.

    Lets a single sweep pool contain models from both families (bailian-token-plan
    / kimi-for-coding via Anthropic, deepseek via OpenAI). Each call resolves
    the right underlying adapter lazily via :func:`adapter_for`.
    """

    def list_models(self) -> list[str]:
        models: set[str] = set()
        for cls in (AnthropicCompatAdapter, OpenAICompatAdapter):
            try:
                models.update(cls().list_models())
            except Exception:
                # Best-effort introspection; skip providers whose config isn't readable.
                continue
        return sorted(models)

    def probe_capabilities(self, model_id: str) -> Capabilities:
        return adapter_for(model_id).probe_capabilities(model_id)

    def chat(self, model_id: str, messages: list[dict], **kwargs: Any) -> ModelResponse:  # type: ignore[override]
        return adapter_for(model_id).chat(model_id, messages, **kwargs)
