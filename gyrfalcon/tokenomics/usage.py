"""One shape for token usage, whatever the provider reported.

Spec: §19.6.

The reason this module exists is a difference that is easy to miss and
impossible to recover from after the fact: **providers disagree about whether
the prompt-token figure already contains the cached tokens.**

* **Anthropic** reports `input_tokens` *exclusive* of caching. The cached
  halves arrive separately as `cache_read_input_tokens` and
  `cache_creation_input_tokens`, and the true prompt size is the sum of all
  three.
* **OpenAI** (and every OpenAI-shaped API, which includes Copilot) reports
  `prompt_tokens` *inclusive* of caching, with `prompt_tokens_details.cached_tokens`
  naming the part of it that was served from cache. The true prompt size is
  `prompt_tokens` alone, and adding the cached figure to it double-counts.

Storing both under one `input_tokens` column — which is what the code did
before this module — produces a number that means one thing on some rows and
another thing on others. Sums across providers are then quietly wrong, and no
later query can tell the two kinds of row apart.

So every provider is normalized here, once, into a shape with one invariant:

    uncached_input + cache_read + cache_write == the true prompt size

`uncached_input` is the part billed at the full input rate, `cache_read` the
part billed at the (much cheaper) cache-read rate, and `cache_write` the part
billed at a premium to *put* it in the cache. Those are three different prices,
which is precisely why they are three different fields.
"""

from __future__ import annotations

from dataclasses import dataclass

from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("tokenomics.usage")


@dataclass(frozen=True)
class Usage:
    """Normalized token usage for a single LLM call."""

    uncached_input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        """The true prompt size — what the model actually read."""
        return (
            self.uncached_input_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens


def _int(source: object, name: str) -> int:
    """Read an integer attribute (or mapping key), defaulting to 0.

    Usage payloads arrive as SDK objects, as plain dicts from a replayed
    fixture, and occasionally with an explicit `None` where a provider means
    zero — so this tolerates all three rather than making every call site do it.
    """
    if source is None:
        return 0
    if isinstance(source, dict):
        value = source.get(name)
    else:
        value = getattr(source, name, None)
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _nested(source: object, name: str) -> object:
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def _normalize_anthropic(raw: object) -> Usage:
    """Anthropic: `input_tokens` excludes both cache figures — so add them."""
    return Usage(
        uncached_input_tokens=_int(raw, "input_tokens"),
        cache_read_tokens=_int(raw, "cache_read_input_tokens"),
        cache_write_tokens=_int(raw, "cache_creation_input_tokens"),
        output_tokens=_int(raw, "output_tokens"),
        # Anthropic bills thinking inside output_tokens; it is not a separate
        # charge, so it is not added to the total anywhere. Left at 0 rather
        # than duplicating output.
        reasoning_tokens=0,
    )


def _normalize_openai(raw: object) -> Usage:
    """OpenAI-shaped: `prompt_tokens` already *includes* the cached tokens.

    Copilot is OpenAI-shaped and goes through this path too. There is no
    cache-write figure because OpenAI's automatic prefix caching does not
    charge for writes — leaving `cache_write_tokens` at 0 is the correct
    accounting, not a missing value.
    """
    prompt = _int(raw, "prompt_tokens")
    cached = _int(_nested(raw, "prompt_tokens_details"), "cached_tokens")
    # Clamp: a provider reporting more cached than prompt tokens would push
    # uncached negative and silently corrupt every later sum.
    cached = min(cached, prompt)
    return Usage(
        uncached_input_tokens=prompt - cached,
        cache_read_tokens=cached,
        cache_write_tokens=0,
        output_tokens=_int(raw, "completion_tokens"),
        reasoning_tokens=_int(
            _nested(raw, "completion_tokens_details"), "reasoning_tokens"
        ),
    )


_ANTHROPIC_PROVIDERS = frozenset({"anthropic", "claude", "bedrock", "vertex"})


def normalize(provider: str, raw_usage: object) -> Usage:
    """Return provider-independent `Usage` from a raw usage payload.

    `provider` selects the arithmetic, not the vendor: anything speaking the
    OpenAI wire shape (Copilot, OpenRouter, a local vLLM) normalizes as OpenAI,
    because what matters here is how the numbers are reported.
    """
    if raw_usage is None:
        return Usage()

    key = (provider or "").lower().strip()
    if key in _ANTHROPIC_PROVIDERS:
        usage = _normalize_anthropic(raw_usage)
    else:
        usage = _normalize_openai(raw_usage)

    logger.debug(
        "usage[%s]: uncached=%d cache_read=%d cache_write=%d out=%d reasoning=%d",
        key or "openai",
        usage.uncached_input_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
        usage.output_tokens,
        usage.reasoning_tokens,
    )
    return usage
