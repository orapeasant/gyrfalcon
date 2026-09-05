"""CLI models command — list and select models by provider."""

from __future__ import annotations

from typing import Optional
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("models_cmd")



def run_models_cli(provider: Optional[str] = None) -> None:
    """List available models, optionally filtered by provider."""
    logger.debug("Beginning of run_models_cli")
    from gyrfalcon.providers import list_providers, get_provider_profile
    from gyrfalcon.config import cfg_get

    current_model = cfg_get("model.name", "")
    providers = list_providers()

    if not providers:
        print("No providers registered.")
        return

    if provider:
        # Show models for specific provider
        profile = get_provider_profile(provider)
        if not profile:
            print(f"Unknown provider: {provider}")
            print(f"Available: {', '.join(providers)}")
            return
        _print_provider_models(profile, current_model)
    else:
        # Show all providers and their models
        for name in providers:
            profile = get_provider_profile(name)
            if profile:
                _print_provider_models(profile, current_model)
                print()


def _print_provider_models(profile, current_model: str) -> None:
    """Print models for a provider."""
    logger.debug("Beginning of _print_provider_models")
    models = profile.fetch_models()
    print(f"  {profile.display_name} ({profile.name})")
    print(f"  {'─' * 40}")
    if not models:
        print("    (no models available)")
        return
    for model_name in models:
        marker = " ← current" if model_name == current_model else ""
        print(f"    • {model_name}{marker}")
