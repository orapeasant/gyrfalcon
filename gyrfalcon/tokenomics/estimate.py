"""What a request would cost, per model, before spending anything.

Spec: §19.5.4.

Pure computation: no LLM call is ever made here. The only network this module
can touch is Anthropic's free `count_tokens`, and only for Claude models, which
have no local tokenizer (§19.5.1).

Three things make the output honest rather than merely plausible:

* **Every token figure carries its method.** `exact`, `api`, or `unavailable` —
  a model whose tokenizer is missing reports no number at all rather than a
  guess dressed up as a count.
* **Cost is split by token class.** Uncached input, cache read and cache write
  are three different prices, and lumping them loses the entire question the
  page exists to answer.
* **Caching is quoted as a curve, not a number.** A single "cache cost" is
  meaningless: writing to cache costs *more* than not caching, and only pays off
  on reuse. So the estimate reports break-even and a cost-at-N-reuses table.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.pricing import ModelRates, break_even_calls, get_rates
from gyrfalcon.tokenizers import TokenCount, count_request, count_text, describe

logger = get_logger("tokenomics.estimate")

#: Reuse counts shown in the break-even table.
DEFAULT_REUSES: tuple[int, ...] = (1, 2, 5, 10, 50)

#: Assumed response length when the caller does not state one. Deliberately
#: explicit and overridable — output is the one number an estimator cannot know.
DEFAULT_OUTPUT_TOKENS = 500


@dataclass
class ReusePoint:
    """Cost of issuing the same prefix `calls` times, cached vs not."""

    calls: int
    uncached_cost: float
    cached_cost: float

    @property
    def saving(self) -> float:
        return self.uncached_cost - self.cached_cost


@dataclass
class ModelEstimate:
    """One row of the estimate table."""

    model: str
    provider: str = ""
    billing: str = "tokens"
    available: bool = True

    # Counting
    input_tokens: int = 0
    method: str = ""
    tokenizer: str = ""
    detail: str = ""

    # Costs, USD
    output_tokens: int = 0
    input_cost: float = 0.0
    output_cost: float = 0.0
    cache_write_cost_5m: float = 0.0
    cache_write_cost_1h: float = 0.0
    cache_read_cost: float = 0.0
    total_cost: float = 0.0

    # Caching
    break_even_calls: Optional[float] = None
    reuses: list[ReusePoint] = field(default_factory=list)

    # Context
    context_window: Optional[int] = None
    fits_context: Optional[bool] = None
    context_used_pct: Optional[float] = None

    rates_source: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["reuses"] = [
            {**asdict(point), "saving": point.saving} for point in self.reuses
        ]
        return data


@dataclass
class Estimate:
    """The whole answer for one piece of content."""

    input_chars: int
    output_tokens: int
    cache_ttl: str
    include_agent_prompt: bool
    overhead_tokens: int
    models: list[ModelEstimate]
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_chars": self.input_chars,
            "output_tokens": self.output_tokens,
            "cache_ttl": self.cache_ttl,
            "include_agent_prompt": self.include_agent_prompt,
            "overhead_tokens": self.overhead_tokens,
            "warnings": self.warnings,
            "models": [m.as_dict() for m in self.models],
        }


# ── The agent's own overhead ──────────────────────────────────────────────────

def agent_prompt_context(
    enabled_toolsets: Optional[list[str]] = None,
) -> tuple[str, list[dict]]:
    """The system prompt and tool schemas a real request would carry.

    This is what makes the default estimate reflect an actual call: for a short
    question the fixed overhead is usually far larger than the question itself.
    Both underlying functions are pure, so this costs nothing but CPU.

    Failures are swallowed to an empty overhead — a broken skills folder should
    degrade the estimate, not break the page.
    """
    system = ""
    tools: list[dict] = []
    try:
        from gyrfalcon.agent.prompt_builder import build_system_prompt

        system = build_system_prompt(
            enabled_toolsets=enabled_toolsets,
            skip_context_files=True,
            skip_memory=True,
        )
    except Exception as err:
        logger.warning("tokenomics: could not build system prompt (%s)", err)
    try:
        from gyrfalcon.model_tools import get_tool_definitions

        tools = get_tool_definitions(enabled_toolsets=enabled_toolsets,
                                     quiet_mode=True)
    except Exception as err:
        logger.warning("tokenomics: could not load tool schemas (%s)", err)
    return system, tools


# ── Per-model estimate ────────────────────────────────────────────────────────

def _reuse_table(
    rates: ModelRates,
    prefix_tokens: int,
    cache_ttl: str,
    reuses: Sequence[int],
) -> list[ReusePoint]:
    """Cost of N calls sharing one prefix, cached vs not.

    Caching is a bet: the first call costs *more*, every later one much less.
    A single figure cannot express that, so the table is the honest form.
    """
    per_1k = prefix_tokens / 1000.0
    uncached_each = per_1k * rates.input

    # What the *first* call costs, which is not the same as the write rate.
    # Anthropic bills cache creation at a premium *instead of* the input rate,
    # so the write rate is the whole first-call charge. OpenAI-shaped providers
    # have no write category at all — the first call is ordinary input, and
    # only later calls are discounted. Treating "no write charge" as "free
    # first call" would have quoted the first request at zero.
    write_rate = rates.effective_cache_write(cache_ttl)
    first_call = per_1k * (write_rate if write_rate > 0 else rates.input)
    read_each = per_1k * rates.effective_cache_read()

    points = []
    for n in reuses:
        if n < 1:
            continue
        points.append(ReusePoint(
            calls=n,
            uncached_cost=n * uncached_each,
            cached_cost=first_call + (n - 1) * read_each,
        ))
    return points


def estimate_model(
    model: str,
    text: str,
    output_tokens: int = DEFAULT_OUTPUT_TOKENS,
    system: Optional[str] = None,
    tools: Optional[Sequence[dict]] = None,
    cache_ttl: str = "5m",
    reuses: Sequence[int] = DEFAULT_REUSES,
) -> ModelEstimate:
    """Estimate one model. Never raises."""
    info = describe(model)
    rates = get_rates(model)

    counted: TokenCount = (
        count_request(model, text, system=system, tools=tools)
        if (system or tools) else count_text(model, text)
    )

    row = ModelEstimate(
        model=model,
        provider=rates.provider,
        billing=rates.billing,
        available=counted.is_known,
        input_tokens=counted.tokens,
        method=counted.method,
        tokenizer=counted.tokenizer or info.get("tokenizer", ""),
        detail=counted.detail,
        output_tokens=output_tokens,
        context_window=rates.context_window,
        rates_source=rates.source,
    )
    if not counted.is_known:
        return row

    tokens = counted.tokens
    at = rates.at(tokens)
    per_1k = tokens / 1000.0

    row.input_cost = per_1k * at.input
    row.output_cost = (output_tokens / 1000.0) * at.output
    row.cache_read_cost = per_1k * at.effective_cache_read()
    row.cache_write_cost_5m = per_1k * at.effective_cache_write("5m")
    row.cache_write_cost_1h = per_1k * at.effective_cache_write("1h")
    row.total_cost = row.input_cost + row.output_cost

    row.break_even_calls = break_even_calls(at, cache_ttl)
    row.reuses = _reuse_table(at, tokens, cache_ttl, reuses)

    if rates.context_window:
        row.fits_context = (tokens + output_tokens) <= rates.context_window
        row.context_used_pct = round(
            100.0 * (tokens + output_tokens) / rates.context_window, 2
        )
    return row


def estimate(
    text: str,
    models: Sequence[str],
    output_tokens: int = DEFAULT_OUTPUT_TOKENS,
    include_agent_prompt: bool = True,
    enabled_toolsets: Optional[list[str]] = None,
    cache_ttl: str = "5m",
    reuses: Sequence[int] = DEFAULT_REUSES,
) -> Estimate:
    """Estimate `text` across `models`.

    `include_agent_prompt` defaults on because it is what a real request sends;
    turn it off to count the bare text.
    """
    system: Optional[str] = None
    tools: Optional[list[dict]] = None
    overhead_tokens = 0
    warnings: list[str] = []

    if include_agent_prompt:
        system, tools = agent_prompt_context(enabled_toolsets)
        if not system and not tools:
            warnings.append(
                "Could not load the agent's system prompt or tools; "
                "showing the bare content only."
            )
        else:
            # Report the overhead against whichever model can count it, so the
            # UI can say "N of these tokens are fixed overhead".
            for model in models:
                probe = count_request(model, "", system=system, tools=tools)
                if probe.is_known:
                    overhead_tokens = probe.tokens
                    break

    rows = [
        estimate_model(model, text, output_tokens=output_tokens,
                       system=system, tools=tools, cache_ttl=cache_ttl,
                       reuses=reuses)
        for model in models
    ]

    if any(r.fits_context is False for r in rows):
        warnings.append(
            "Some models cannot fit this input in their context window."
        )
    if any(not r.available for r in rows):
        warnings.append(
            "Some models have no available tokenizer; those rows show no count "
            "rather than an estimate."
        )

    return Estimate(
        input_chars=len(text),
        output_tokens=output_tokens,
        cache_ttl=cache_ttl,
        include_agent_prompt=include_agent_prompt,
        overhead_tokens=overhead_tokens,
        models=rows,
        warnings=warnings,
    )
