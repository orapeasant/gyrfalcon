"""Anthropic native provider."""

from gyrfalcon.providers import ProviderProfile, register_provider
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("anthropic_provider")



class AnthropicProvider(ProviderProfile):
    """Anthropic native API provider."""

    def __init__(self):
        super().__init__(
            name="anthropic",
            api_mode="anthropic_messages",
            aliases=["claude"],
            display_name="Anthropic",
            env_vars=["ANTHROPIC_API_KEY"],
            base_url="https://api.anthropic.com",
            auth_type="api_key",
            fallback_models=[
                "claude-sonnet-4-20250514",
                "claude-3-5-sonnet-20241022",
                "claude-3-5-haiku-20241022",
                "claude-3-opus-20240229",
            ],
            hostname="api.anthropic.com",
            default_model="claude-sonnet-4-20250514",
            default_max_tokens=8192,
        )

    def fetch_models(self) -> list[str]:
        logger.debug("Beginning of fetch_models")
        return self.fallback_models


# Register
register_provider(AnthropicProvider())
