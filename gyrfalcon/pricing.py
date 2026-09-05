"""LLM pricing — per-model token rates and cost calculation.

Costs are stored in USD. AIC (AI Credits) = $0.01 each.
GitHub Copilot billing uses AIC units; 1 AIC = 10,000 input tokens or 5,000 output tokens
(approximate, based on Copilot for Business published rates).

All rates in USD per 1,000 tokens.
"""

from __future__ import annotations

from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("pricing")

# ── AIC conversion ─────────────────────────────────────────────────────────────
AIC_PER_USD = 100.0        # 1 USD = 100 AIC  (1 AIC = $0.01)


# ── Model pricing table (USD per 1K tokens) ───────────────────────────────────
# Format: { model_prefix: (input_per_1k, output_per_1k) }
# GitHub Copilot AIC rates (approximate, public pricing as of 2025):
#   gpt-4o:        0.1  input / 0.3  output  AIC per 1K tokens
#   gpt-4o-mini:   0.01 input / 0.03 output  AIC per 1K tokens
#   claude-3-...:  0.15 input / 0.45 output  AIC per 1K tokens
#   gemini-2.5-*:  0.05 input / 0.15 output  AIC per 1K tokens
# Converted to USD: divide AIC by 100

_MODEL_RATES: dict[str, tuple[float, float]] = {
    # GitHub Copilot / OpenAI
    "gpt-4o":                   (0.0010, 0.0030),   # AIC: 0.10 / 0.30
    "gpt-4o-mini":              (0.0001, 0.0003),   # AIC: 0.01 / 0.03
    "gpt-4.1":                  (0.0020, 0.0060),
    "gpt-4.1-mini":             (0.0002, 0.0006),
    "gpt-4-turbo":              (0.0100, 0.0300),
    "gpt-4":                    (0.0300, 0.0600),
    "gpt-3.5-turbo":            (0.0005, 0.0015),
    "o1":                       (0.0150, 0.0600),
    "o1-mini":                  (0.0030, 0.0120),
    "o3-mini":                  (0.0011, 0.0044),
    "o4-mini":                  (0.0011, 0.0044),
    "gpt-5":                    (0.0080, 0.0320),
    "gpt-5-mini":               (0.0008, 0.0032),
    # Anthropic
    "claude-opus-4":            (0.0150, 0.0750),
    "claude-opus":              (0.0150, 0.0750),
    "claude-sonnet-4":          (0.0030, 0.0150),
    "claude-sonnet-3-7":        (0.0030, 0.0150),
    "claude-sonnet-3-5":        (0.0030, 0.0150),
    "claude-sonnet":            (0.0030, 0.0150),
    "claude-haiku-3-5":         (0.0008, 0.0040),
    "claude-haiku":             (0.0008, 0.0040),
    "claude-3-opus":            (0.0150, 0.0750),
    "claude-3-sonnet":          (0.0030, 0.0150),
    "claude-3-haiku":           (0.0003, 0.0013),
    # Google
    "gemini-2.5-pro":           (0.0013, 0.0050),
    "gemini-2.5-flash":         (0.0003, 0.0012),
    "gemini-2.0-flash":         (0.0001, 0.0004),
    "gemini-1.5-pro":           (0.0013, 0.0050),
    "gemini-1.5-flash":         (0.0001, 0.0004),
    # Fallback
    "__default__":              (0.0010, 0.0030),
}


def _match_rate(model: str) -> tuple[float, float]:
    """Find the closest rate for a model by prefix matching."""
    m = (model or "").lower().strip()
    # Exact match first
    if m in _MODEL_RATES:
        return _MODEL_RATES[m]
    # Prefix match — longest wins
    best_key = "__default__"
    best_len = 0
    for key in _MODEL_RATES:
        if key == "__default__":
            continue
        if m.startswith(key) and len(key) > best_len:
            best_key = key
            best_len = len(key)
    return _MODEL_RATES[best_key]


def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int = 0,
) -> float:
    """Return cost in USD for a single LLM call."""
    in_rate, out_rate = _match_rate(model)
    total_output = output_tokens + reasoning_tokens
    cost = (input_tokens / 1000.0) * in_rate + (total_output / 1000.0) * out_rate
    logger.debug(
        "Cost calc: model=%s in=%d out=%d reasoning=%d → $%.6f",
        model, input_tokens, output_tokens, reasoning_tokens, cost,
    )
    return cost


def usd_to_aic(usd: float) -> float:
    """Convert USD cost to AIC units."""
    return round(usd * AIC_PER_USD, 4)


def format_cost(usd: float, provider: str | None = None) -> dict:
    """Return a display-friendly cost dict."""
    aic = usd_to_aic(usd)
    return {
        "usd":      round(usd, 6),
        "aic":      aic,
        "display":  f"{aic:.4f} AIC" if (provider or "").lower() in ("copilot", "github-copilot", "gh-copilot") or aic > 0
                    else f"${usd:.6f}",
    }
