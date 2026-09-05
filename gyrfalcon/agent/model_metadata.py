"""Model context length resolution and token estimation."""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("agent.model_metadata")

DEFAULT_CONTEXT_LENGTHS: dict[str, int] = {
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-sonnet": 200_000,
    "claude-3-haiku": 200_000,
    "claude-4-opus": 200_000,
    "claude-4-sonnet": 200_000,
    "gemini-2.0-flash": 1_000_000,
    "gemini-2.0-pro": 2_000_000,
    "gemini-1.5-pro": 2_000_000,
    "gemini-1.5-flash": 1_000_000,
    "deepseek-chat": 64_000,
    "deepseek-coder": 128_000,
    "deepseek-r1": 128_000,
    "qwen-2.5-72b": 128_000,
    "llama-3.1-405b": 128_000,
    "llama-3.1-70b": 128_000,
    "mistral-large": 128_000,
}

FALLBACK_CONTEXT_LENGTH = 128_000


def get_model_context_length(
    model: str,
    base_url: str | None = None,
    api_key: str | None = None,
    config_context_length: int | None = None,
    provider: str | None = None,
) -> int:
    """Multi-source resolution for context window size."""
    # 1. User config override
    logger.debug("Beginning of get_model_context_length")
    if config_context_length:
        return config_context_length

    # 2. Match against known models
    model_lower = model.lower()
    for known_model, length in DEFAULT_CONTEXT_LENGTHS.items():
        if known_model in model_lower:
            return length

    # 3. Check for patterns
    if "200k" in model_lower or "200000" in model_lower:
        return 200_000
    if "128k" in model_lower or "128000" in model_lower:
        return 128_000
    if "1m" in model_lower or "1000000" in model_lower:
        return 1_000_000

    # 4. Fallback
    return FALLBACK_CONTEXT_LENGTH


def estimate_tokens_rough(text: str) -> int:
    """Rough estimate: len(text) // 4."""
    logger.debug("Beginning of estimate_tokens_rough")
    return len(text) // 4


def estimate_messages_tokens_rough(messages: list[dict]) -> int:
    """Token estimate for full message list."""
    logger.debug("Beginning of estimate_messages_tokens_rough")
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content) // 4
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        total += len(part.get("text", "")) // 4
                    elif part.get("type") == "image_url":
                        total += 1600
        # Tool calls
        tool_calls = msg.get("tool_calls", [])
        if tool_calls:
            total += sum(len(str(tc)) // 4 for tc in tool_calls)
        total += 4  # message overhead
    return total


def estimate_request_tokens_rough(
    messages: list[dict], system: str = "", tools: list[dict] | None = None
) -> int:
    """Full request estimate including tool schemas."""
    logger.debug("Beginning of estimate_request_tokens_rough")
    total = estimate_messages_tokens_rough(messages)
    total += len(system) // 4
    if tools:
        total += sum(len(str(t)) // 4 for t in tools)
    return total


def is_local_endpoint(base_url: str | None) -> bool:
    """Detect localhost/Tailscale endpoints."""
    logger.debug("Beginning of is_local_endpoint")
    if not base_url:
        return False
    parsed = urlparse(base_url)
    hostname = parsed.hostname or ""
    return hostname in ("localhost", "127.0.0.1", "0.0.0.0") or hostname.endswith(".ts.net")
