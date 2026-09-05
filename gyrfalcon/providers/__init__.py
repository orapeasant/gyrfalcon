"""Provider base — ProviderProfile and registry."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("providers")


@dataclass
class ProviderProfile:
    """Provider profile defining an inference backend."""
    name: str
    api_mode: str = "chat_completions"
    aliases: list[str] = field(default_factory=list)
    display_name: str = ""
    env_vars: list[str] = field(default_factory=list)
    base_url: str = ""
    models_url: Optional[str] = None
    auth_type: str = "api_key"  # api_key, oauth_device_code, oauth_external, copilot, aws_sdk
    fallback_models: list[str] = field(default_factory=list)
    hostname: str = ""
    fixed_temperature: Optional[float] = None
    default_max_tokens: Optional[int] = None
    default_aux_model: Optional[str] = None
    default_model: Optional[str] = None

    def prepare_messages(self, messages: list[dict]) -> list[dict]:
        """Hook to transform messages before API call."""
        return messages

    def build_extra_body(self, **kwargs) -> dict:
        """Hook for extra request body parameters."""
        return {}

    def build_api_kwargs_extras(self, **kwargs) -> dict:
        """Hook for additional API kwargs."""
        return {}

    def fetch_models(self) -> list[str]:
        """Fetch available models from provider."""
        return self.fallback_models


_providers: dict[str, ProviderProfile] = {}
_discovered = False
_lock = threading.RLock()


def register_provider(profile: ProviderProfile) -> None:
    """Register a provider profile."""
    with _lock:
        _providers[profile.name] = profile
        for alias in profile.aliases:
            _providers[alias] = profile
    logger.debug(f"Registered provider: {profile.name}")


def get_provider_profile(name: str) -> Optional[ProviderProfile]:
    """Get a provider by name."""
    _ensure_discovered()
    return _providers.get(name)


def list_providers() -> list[str]:
    """List all registered provider names."""
    _ensure_discovered()
    return [k for k, v in _providers.items() if k == v.name]


def _ensure_discovered() -> None:
    """Lazy discovery of providers."""
    global _discovered
    if _discovered:
        return
    with _lock:
        if _discovered:
            return
        _discover_providers()
        _discovered = True


def _discover_providers() -> None:
    """Scan and register built-in providers."""
    from gyrfalcon.providers import copilot, openai_provider, anthropic_provider
    # Bedrock imported lazily (boto3 is slow to import)
    try:
        from gyrfalcon.providers import bedrock
    except ImportError:
        pass
