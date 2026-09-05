"""Auxiliary LLM client — single resolution chain for side-channel LLM calls."""

from __future__ import annotations

import threading
from typing import Any, Optional

from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.config import cfg_get, get_env_value

logger = get_logger("agent.auxiliary")

_client_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()


def _get_openai_client(base_url: str | None = None, api_key: str | None = None):
    """Lazy OpenAI client creation with proxy-first fallback."""
    logger.debug("Beginning of _get_openai_client")
    cache_key = f"{base_url}:{api_key}"
    with _cache_lock:
        if cache_key in _client_cache:
            return _client_cache[cache_key]

    from gyrfalcon.net import get_openai_client_with_fallback

    client = get_openai_client_with_fallback(base_url=base_url, api_key=api_key)
    with _cache_lock:
        _client_cache[cache_key] = client
    return client


def call_llm(
    task: str,
    *,
    messages: list[dict],
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    tools: list[dict] | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> Any:
    """Centralized synchronous LLM call with auto-resolution and retry."""
    # Check for task-specific config override
    logger.debug("Beginning of call_llm")
    task_provider = cfg_get(f"auxiliary.{task}.provider")
    task_model = cfg_get(f"auxiliary.{task}.model")
    if task_provider:
        provider = task_provider
    if task_model:
        model = task_model

    # Resolution chain
    if not base_url and not api_key:
        resolved = _resolve_credentials(provider)
        if resolved:
            base_url, api_key, default_model = resolved
            if not model:
                model = default_model

    if not model:
        model = cfg_get("model.name", "gpt-4o-mini")
    if not api_key:
        api_key = get_env_value("OPENAI_API_KEY") or "dummy"
    if not base_url:
        base_url = None  # Use OpenAI default

    client = _get_openai_client(base_url, api_key)

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    if tools:
        kwargs["tools"] = tools

    try:
        return client.chat.completions.create(**kwargs)
    except Exception as e:
        logger.warning(f"Auxiliary LLM call failed ({task}): {e}")
        # Try fallback
        fallback = _try_fallback(task, messages, model, kwargs)
        if fallback:
            return fallback
        raise


def _resolve_credentials(provider: str | None) -> Optional[tuple[str, str, str]]:
    """Resolve base_url, api_key, model for a provider."""
    logger.debug("Beginning of _resolve_credentials")
    if provider == "anthropic":
        key = get_env_value("ANTHROPIC_API_KEY")
        if key:
            return "https://api.anthropic.com/v1", key, "claude-3-5-haiku-latest"

    if provider == "openrouter":
        key = get_env_value("OPENROUTER_API_KEY")
        if key:
            return "https://openrouter.ai/api/v1", key, "openai/gpt-4o-mini"

    # Default: try OpenAI
    key = get_env_value("OPENAI_API_KEY")
    if key:
        return None, key, "gpt-4o-mini"

    # Try OpenRouter as fallback
    key = get_env_value("OPENROUTER_API_KEY")
    if key:
        return "https://openrouter.ai/api/v1", key, "openai/gpt-4o-mini"

    return None


def _try_fallback(task: str, messages: list[dict], failed_model: str, kwargs: dict) -> Any:
    """Try next provider on failure."""
    # Simple fallback: try a different provider
    logger.debug("Beginning of _try_fallback")
    providers = ["openai", "openrouter", "anthropic"]
    for prov in providers:
        creds = _resolve_credentials(prov)
        if creds:
            base_url, api_key, model = creds
            if model == failed_model:
                continue
            try:
                client = _get_openai_client(base_url, api_key)
                kwargs_copy = {**kwargs, "model": model}
                return client.chat.completions.create(**kwargs_copy)
            except Exception:
                continue
    return None
