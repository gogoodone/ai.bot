"""Model selection + construction — keeps the runtime independent of any specific model.

Strands' BedrockModel can drive any Bedrock model (Anthropic, Nova, Llama, Mistral, …). Only
Anthropic models support prompt/tool caching, so cachePoints are enabled just for those and left
off otherwise — nothing else in the runtime cares which model is in use.

Optional router (override chain, highest first):
  1. payload["model"]      — per-request experiment (try a model for one call)
  2. registry["model"]     — switch the default with no redeploy (access-registry secret)
  3. config.MODEL_ID       — built-in default
"""
from __future__ import annotations

from strands.models import BedrockModel, CacheConfig, CacheToolsConfig

import config

_built: dict[str, BedrockModel] = {}      # model_id -> instance (once per warm container)


def _cache_capable(model_id: str) -> bool:
    # Only Anthropic supports the prompt+tools cachePoint scheme Strands inserts. Nova rejects a
    # cachePoint in the tools array, so it (and others) run without caching — cheap/fast regardless.
    m = model_id.lower()
    return "anthropic" in m or "claude" in m


def build(model_id: str) -> BedrockModel:
    if model_id not in _built:
        kw = dict(model_id=model_id, region_name=config.REGION, temperature=0.0)
        if _cache_capable(model_id):
            # "auto" injects a cachePoint into the conversation (caches system + the message history,
            # incl. a loaded skill — separate from the prompt but still cached); cache_tools caches tools.
            # 1h TTL on both so the warm prefix survives the gaps between Slack questions (write 2x,
            # read ~0.1x — wins whenever a user asks a follow-up within the hour).
            kw.update(cache_config=CacheConfig(strategy="auto", ttl="1h"),
                      cache_tools=CacheToolsConfig(type="default", ttl="1h"))
        _built[model_id] = BedrockModel(**kw)
    return _built[model_id]


def route(payload: dict) -> str:
    override = (payload or {}).get("model")
    if override:
        return override
    try:                                                  # registry switch (optional, no redeploy)
        import access
        m = access.registry().get("model")
        if m:
            return m
    except Exception:  # noqa: BLE001
        pass
    return config.MODEL_ID


def for_request(payload: dict) -> BedrockModel:
    return build(route(payload))


def chain(payload: dict) -> list[str]:
    """Model fail-over order for this request: the routed primary, then the built-in default
    (config.MODEL_ID = Sonnet) as a reliable, cache-capable fallback. Deduped, so when the primary
    already IS the default (e.g. prod) the chain is a single model and nothing falls back."""
    primary = route(payload)
    return [primary] if primary == config.MODEL_ID else [primary, config.MODEL_ID]
