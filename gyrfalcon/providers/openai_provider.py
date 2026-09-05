"""OpenAI-compatible provider."""

from gyrfalcon.providers import ProviderProfile, register_provider
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("openai_provider")



class OpenAIProvider(ProviderProfile):
    """OpenAI API provider."""

    def __init__(self):
        super().__init__(
            name="openai",
            api_mode="chat_completions",
            aliases=["gpt"],
            display_name="OpenAI",
            env_vars=["OPENAI_API_KEY"],
            base_url="https://api.openai.com/v1",
            auth_type="api_key",
            fallback_models=["gpt-4o", "gpt-4o-mini", "o1", "o1-mini"],
            hostname="api.openai.com",
            default_model="gpt-4o",
        )

    def fetch_models(self) -> list[str]:
        logger.debug("Beginning of fetch_models")
        try:
            import os
            from gyrfalcon.net import get_openai_client_with_fallback
            client = get_openai_client_with_fallback(api_key=os.environ.get("OPENAI_API_KEY"))
            models = client.models.list()
            return sorted([m.id for m in models.data if "gpt" in m.id or "o1" in m.id])
        except Exception:
            return self.fallback_models


class OpenRouterProvider(ProviderProfile):
    """OpenRouter multi-model provider."""

    def __init__(self):
        super().__init__(
            name="openrouter",
            api_mode="chat_completions",
            aliases=["or"],
            display_name="OpenRouter",
            env_vars=["OPENROUTER_API_KEY"],
            base_url="https://openrouter.ai/api/v1",
            auth_type="api_key",
            fallback_models=["openai/gpt-4o", "anthropic/claude-3.5-sonnet", "google/gemini-2.0-flash-exp"],
            hostname="openrouter.ai",
            default_model="openai/gpt-4o",
        )

    def build_extra_body(self, **kwargs) -> dict:
        logger.debug("Beginning of build_extra_body")
        return {"transforms": ["middle-out"]}


# Register providers
register_provider(OpenAIProvider())
register_provider(OpenRouterProvider())
