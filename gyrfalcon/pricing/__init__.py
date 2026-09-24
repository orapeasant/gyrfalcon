"""LLM pricing — per-model rates and cost calculation.

Spec: `docs/spec/gyrfalcon/19-tokenomics.md` §19.5.2.

Costs are USD. This was a flat module with a hardcoded rate dict; it is now a
package over a fetched, overridable catalog (`catalog.py`), because rates change
monthly and a dict makes every price change a code change. The public surface is
unchanged: `calculate_cost`, `usd_to_aic`, `format_cost`.
"""

from __future__ import annotations

from typing import Optional

from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.pricing.catalog import (
    FEED_URL,
    get_rates,
    invalidate,
    load,
    meta,
    refresh,
)
from gyrfalcon.pricing.rates import (
    AIC_PER_USD,
    CREDITS_PER_USD,
    DEFAULT_RATES,
    ModelRates,
    Tier,
    break_even_calls,
    cost_of,
)

logger = get_logger("pricing")

__all__ = [
    "AIC_PER_USD",
    "CREDITS_PER_USD",
    "DEFAULT_RATES",
    "FEED_URL",
    "ModelRates",
    "Tier",
    "break_even_calls",
    "calculate_cost",
    "cost_of",
    "format_cost",
    "get_rates",
    "invalidate",
    "load",
    "meta",
    "refresh",
    "usd_to_aic",
]


def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_ttl: str = "5m",
) -> float:
    """Return cost in USD for a single LLM call.

    `input_tokens` must be the **uncached** portion of the prompt — the three
    token figures are priced separately and must not overlap. Use
    `gyrfalcon.tokenomics.usage.normalize()` to get them in that shape from a
    provider payload; passing a provider's raw `prompt_tokens` (which already
    includes cached tokens on OpenAI-shaped APIs) double-counts the cache.
    """
    cost = cost_of(
        get_rates(model),
        uncached_input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        cache_ttl=cache_ttl,
    )
    logger.debug(
        "Cost calc: model=%s in=%d cache_read=%d cache_write=%d out=%d "
        "reasoning=%d → $%.6f",
        model, input_tokens, cache_read_tokens, cache_write_tokens,
        output_tokens, reasoning_tokens, cost,
    )
    return cost


def usd_to_aic(usd: float) -> float:
    """Convert USD to GitHub AI Credits.

    Credits are dollar-denominated — a $10/month plan carries $10 in credits —
    so this is an identity. It previously multiplied by 100, overstating credit
    spend by 100x.
    """
    return round(usd * CREDITS_PER_USD, 6)


def format_cost(usd: float, provider: Optional[str] = None,
                model: Optional[str] = None) -> dict:
    """Return a display-friendly cost dict.

    Credits are shown only for models actually billed that way — determined
    from the catalog, or from an explicit Copilot `provider`. The previous rule
    (`or aic > 0`) rendered *every* non-zero cost as AIC regardless of provider.
    """
    credits = usd_to_aic(usd)
    billed_in_credits = (provider or "").lower() in (
        "copilot", "github-copilot", "gh-copilot", "github_copilot",
    )
    if not billed_in_credits and model:
        billed_in_credits = get_rates(model).billing == "credits"

    return {
        "usd": round(usd, 6),
        "aic": credits,
        "credits": credits,
        "billing": "credits" if billed_in_credits else "tokens",
        "display": f"{credits:.4f} credits" if billed_in_credits else f"${usd:.6f}",
    }
