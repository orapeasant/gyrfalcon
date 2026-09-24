"""Per-model rates and the cost arithmetic over them.

Spec: §19.5.2.

Rates are USD per 1,000 tokens throughout. The upstream feed quotes per-token
figures; conversion happens once, at ingest (`scripts/build_pricing_snapshot.py`
and `catalog.py`), so nothing downstream has to remember which unit it holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("pricing.rates")

# ── Credits ───────────────────────────────────────────────────────────────────
#
# GitHub AI Credits are **dollar-denominated**: a $10/month Copilot plan comes
# with $10 in credits, and credits are consumed from token usage (input, output
# and cached) at published per-model rates. The previous constant here encoded
# 1 credit = $0.01, which overstated credit spend by 100x.

CREDITS_PER_USD = 1.0

# Retained so existing imports keep working; `usd_to_aic` is now an identity.
AIC_PER_USD = CREDITS_PER_USD


@dataclass(frozen=True)
class Tier:
    """A higher rate that applies once the prompt passes a size threshold."""

    above_input_tokens: int
    input: Optional[float] = None
    output: Optional[float] = None
    cache_read: Optional[float] = None
    cache_write_5m: Optional[float] = None


@dataclass(frozen=True)
class ModelRates:
    """USD per 1K tokens for one model."""

    model: str = "__default__"
    provider: str = ""
    input: float = 0.0010
    output: float = 0.0030
    cache_read: Optional[float] = None
    cache_write_5m: Optional[float] = None
    cache_write_1h: Optional[float] = None
    context_window: Optional[int] = None
    billing: str = "tokens"          # "tokens" | "credits"
    tier: Optional[Tier] = None
    source: str = "default"          # bundled | refreshed | overlay | default

    # ── Derived cache rates ──────────────────────────────────────────────────
    #
    # Not every catalog entry carries explicit cache rates. Rather than treat a
    # missing rate as free — which would understate cost, the dangerous
    # direction — fall back to the published multipliers for the provider.

    def effective_cache_read(self) -> float:
        if self.cache_read is not None:
            return self.cache_read
        multiplier = 0.1 if self.provider_is_anthropic else 0.25
        return self.input * multiplier

    def effective_cache_write(self, ttl: str = "5m") -> float:
        if ttl == "1h" and self.cache_write_1h is not None:
            return self.cache_write_1h
        if ttl == "5m" and self.cache_write_5m is not None:
            return self.cache_write_5m
        if not self.provider_is_anthropic:
            # OpenAI-shaped automatic prefix caching does not bill writes.
            return 0.0
        return self.input * (2.0 if ttl == "1h" else 1.25)

    @property
    def provider_is_anthropic(self) -> bool:
        return "anthropic" in (self.provider or "") or self.model.startswith("claude")

    def at(self, prompt_tokens: int) -> "ModelRates":
        """This model's rates for a prompt of the given size.

        Above a vendor's context threshold several models cost strictly more;
        ignoring the tier silently under-quotes exactly the large prompts a
        cost estimator exists to warn about.
        """
        tier = self.tier
        if tier is None or prompt_tokens <= tier.above_input_tokens:
            return self
        return ModelRates(
            model=self.model,
            provider=self.provider,
            input=tier.input if tier.input is not None else self.input,
            output=tier.output if tier.output is not None else self.output,
            cache_read=tier.cache_read if tier.cache_read is not None else self.cache_read,
            cache_write_5m=(
                tier.cache_write_5m if tier.cache_write_5m is not None
                else self.cache_write_5m
            ),
            cache_write_1h=self.cache_write_1h,
            context_window=self.context_window,
            billing=self.billing,
            tier=None,
            source=self.source,
        )


DEFAULT_RATES = ModelRates()


# ── Cost ──────────────────────────────────────────────────────────────────────

def cost_of(
    rates: ModelRates,
    uncached_input_tokens: int = 0,
    output_tokens: int = 0,
    reasoning_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_ttl: str = "5m",
) -> float:
    """USD for one call, pricing each token class at its own rate.

    The three input figures must not overlap — see
    `gyrfalcon.tokenomics.usage.normalize()`, which is what produces them in
    that shape.
    """
    prompt = uncached_input_tokens + cache_read_tokens + cache_write_tokens
    r = rates.at(prompt)
    return (
        (uncached_input_tokens / 1000.0) * r.input
        + (cache_read_tokens / 1000.0) * r.effective_cache_read()
        + (cache_write_tokens / 1000.0) * r.effective_cache_write(cache_ttl)
        + ((output_tokens + reasoning_tokens) / 1000.0) * r.output
    )


def break_even_calls(rates: ModelRates, cache_ttl: str = "5m") -> Optional[float]:
    """How many uses of a cached prefix before caching is cheaper.

    N* = (w - r) / (1 - r), with w and r as multiples of the base input rate.
    Returns None when the base rate is zero (nothing to compare against), and
    1.0 when writes are free, which is the OpenAI-shaped case: caching there
    can never lose.
    """
    if rates.input <= 0:
        return None
    w = rates.effective_cache_write(cache_ttl) / rates.input
    r = rates.effective_cache_read() / rates.input
    if w <= 0:
        return 1.0
    if r >= 1:
        return None
    return (w - r) / (1 - r)
