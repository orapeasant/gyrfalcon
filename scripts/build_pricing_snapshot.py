#!/usr/bin/env python3
"""Regenerate the bundled price snapshot at `gyrfalcon/pricing/catalog.json`.

Spec: §19.5.2.

The upstream feed is ~2.8 MB of 4,300 entries covering embeddings, rerankers,
image models and a long tail of proxy re-exports. Bundling it whole would put
megabytes of mostly-irrelevant JSON in the repo and in every install, so this
trims it to chat models that have a price, keeping only the fields the cost
model actually uses.

Run it to refresh the shipped snapshot:

    uv run python scripts/build_pricing_snapshot.py

This is a *build* step, not a runtime path — `gyrfalcon pricing refresh` is what
users run, and it writes to the profile rather than the repo.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

FEED_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
OUT = Path(__file__).resolve().parent.parent / "gyrfalcon" / "pricing" / "catalog.json"

# Providers worth shipping offline. The excluded tail is overwhelmingly
# re-exports of these same models through aggregators, priced identically;
# a user on one of those can still `pricing refresh` or use the overlay.
KEEP_PROVIDERS = {
    "openai", "azure", "anthropic", "bedrock", "bedrock_converse",
    "vertex_ai-language-models", "vertex_ai-anthropic_models", "gemini",
    "github_copilot", "mistral", "deepseek", "groq", "xai", "ollama",
}


def per_1k(raw: dict, key: str) -> float | None:
    """Feed rates are per token; the catalog stores per 1K."""
    value = raw.get(key)
    if value is None:
        return None
    try:
        return round(float(value) * 1000.0, 10)
    except (TypeError, ValueError):
        return None


def trim(name: str, raw: dict) -> dict | None:
    if raw.get("mode") != "chat" or not raw.get("input_cost_per_token"):
        return None
    if raw.get("litellm_provider") not in KEEP_PROVIDERS:
        return None

    entry: dict = {
        "provider": raw.get("litellm_provider"),
        "input": per_1k(raw, "input_cost_per_token"),
        "output": per_1k(raw, "output_cost_per_token"),
    }
    for field, key in (
        ("cache_read", "cache_read_input_token_cost"),
        ("cache_write_5m", "cache_creation_input_token_cost"),
        ("cache_write_1h", "cache_creation_input_token_cost_above_1hr"),
    ):
        rate = per_1k(raw, key)
        if rate is not None:
            entry[field] = rate

    if raw.get("max_input_tokens"):
        entry["context_window"] = int(raw["max_input_tokens"])

    # Tiered pricing: several vendors charge more above a context threshold.
    tier = {}
    for field, key in (
        ("input", "input_cost_per_token_above_200k_tokens"),
        ("output", "output_cost_per_token_above_200k_tokens"),
        ("cache_read", "cache_read_input_token_cost_above_200k_tokens"),
        ("cache_write_5m", "cache_creation_input_token_cost_above_200k_tokens"),
    ):
        rate = per_1k(raw, key)
        if rate is not None:
            tier[field] = rate
    if tier:
        tier["above_input_tokens"] = 200_000
        entry["tier"] = tier

    # Copilot bills GitHub AI Credits, which are dollar-denominated and derived
    # from token usage at these same rates (§19.10). Marking it here keeps the
    # distinction in data rather than in a special case at the call site.
    if raw.get("litellm_provider") == "github_copilot":
        entry["billing"] = "credits"

    return entry


def main() -> int:
    print(f"fetching {FEED_URL}")
    with urllib.request.urlopen(FEED_URL, timeout=120) as response:  # noqa: S310
        feed = json.loads(response.read().decode("utf-8"))

    models = {}
    for name, raw in feed.items():
        if not isinstance(raw, dict):
            continue
        entry = trim(name, raw)
        if entry:
            models[name] = entry

    catalog = {
        "source": FEED_URL,
        "models": dict(sorted(models.items())),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(catalog, indent=1, sort_keys=False) + "\n",
                   encoding="utf-8")
    size_kb = OUT.stat().st_size / 1024
    print(f"wrote {len(models)} models to {OUT} ({size_kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
