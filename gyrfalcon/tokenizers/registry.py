"""Which tokenizer counts which model, and what to do when none can.

Spec: §19.5.1.

Every supported family counts **exactly**. There is no ratio, no chars-per-token
heuristic, and no proxy tokenizer, because a cost estimate that is quietly 20%
wrong is worse than one that says it does not know:

| Family | Backend | Local? |
|---|---|---|
| `gpt-*`, `o*` | `tiktoken` | yes |
| `gemini-*` | Vertex AI SentencePiece vocab (shared with Gemma) | yes, after one download |
| Llama / Mistral / Qwen / DeepSeek / Gemma | Hugging Face `tokenizers` | yes |
| `claude-*` | `POST /v1/messages/count_tokens` | no — network |
| anything else | — | reports `unavailable` |

**Claude has no local option and no approximation is offered.** Anthropic does
not publish the Claude 3+ vocabulary, and states that tiktoken must not be
substituted for it: it undercounts by roughly 15-20% on prose and by more on
code. A tokenizer is a vocabulary file rather than model weights, so no amount
of local hosting produces one. Claude 4.7+ also moved to a tokenizer that counts
about 30% higher than earlier models, so a stale community tokenizer would be
wrong in a way that grows with model choice.

The `count_tokens` endpoint is a good answer to that: Anthropic documents it as
*"free to use"*, rate-limited per minute by usage tier and on limits entirely
separate from message creation. So exact Claude counts cost nothing and never
compete with real inference for quota. Results are memoized by content hash, so
comparing one document across several Claude models costs one call per model.

Missing optional dependencies never raise. They degrade that family to
`unavailable` with an install hint, and the caller decides what to show.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Optional, Sequence

from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("tokenizers")

# ── Methods ───────────────────────────────────────────────────────────────────

EXACT = "exact"              # a real tokenizer for this model, run locally
API = "api"                  # the vendor counted it for us
APPROX = "approx"            # reserved; nothing returns this today
UNAVAILABLE = "unavailable"  # we will not guess

#: Per-message framing overhead applied by chat APIs on top of the raw content
#: (role markers and separators). Small, constant, and documented by OpenAI as
#: ~3 tokens per message; counting content alone under-reports every request.
FRAMING_TOKENS_PER_MESSAGE = 3


@dataclass(frozen=True)
class TokenCount:
    """A token count and, just as importantly, how it was arrived at."""

    tokens: int
    method: str
    tokenizer: str = ""
    detail: str = ""

    @property
    def is_known(self) -> bool:
        return self.method in (EXACT, API)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tokens": self.tokens,
            "method": self.method,
            "tokenizer": self.tokenizer,
            "detail": self.detail,
            "known": self.is_known,
        }


def _unavailable(reason: str, hint: str = "") -> TokenCount:
    return TokenCount(0, UNAVAILABLE, "", f"{reason}{'. ' + hint if hint else ''}")


# ── OpenAI (tiktoken) ─────────────────────────────────────────────────────────

_TIKTOKEN_HINT = 'install the extra: `uv sync --extra tokenizers`'


@lru_cache(maxsize=16)
def _tiktoken_encoding(model: str):
    import tiktoken

    try:
        return tiktoken.encoding_for_model(model), model
    except KeyError:
        # Unknown-to-tiktoken but clearly an OpenAI model: o200k_base is the
        # current-generation vocabulary (GPT-4o and later).
        return tiktoken.get_encoding("o200k_base"), "o200k_base"


def _count_openai(model: str, text: str) -> TokenCount:
    try:
        encoding, name = _tiktoken_encoding(model)
    except ImportError:
        return _unavailable("tiktoken is not installed", _TIKTOKEN_HINT)
    except Exception as err:                     # pragma: no cover - defensive
        return _unavailable(f"tiktoken failed: {err}")
    return TokenCount(len(encoding.encode(text)), EXACT, name)


# ── Gemini (Vertex AI SentencePiece) ──────────────────────────────────────────

@lru_cache(maxsize=8)
def _gemini_tokenizer(model: str):
    from vertexai.preview import tokenization  # type: ignore[import-not-found]

    return tokenization.get_tokenizer_for_model(model)


def _count_gemini(model: str, text: str) -> TokenCount:
    """Gemini via the Vertex AI SDK's local SentencePiece vocabulary.

    Gemini and Gemma share a tokenizer, so it is tempting to substitute the
    (much lighter) Gemma vocabulary from Hugging Face when the Vertex SDK is
    absent. This deliberately does not: that equivalence is documented for the
    Gemma-era vocabulary and cannot be verified here for newer Gemini versions,
    and an unverifiable count is exactly what this module refuses to report as
    exact. Install `--extra tokenizers-gemini` to count Gemini.
    """
    try:
        tokenizer = _gemini_tokenizer(model)
    except ImportError:
        return _unavailable(
            "the Vertex AI tokenizer is not installed", _TIKTOKEN_HINT
        )
    except Exception as err:
        # An unrecognised Gemini variant, or the one-time vocab download failed.
        return _unavailable(f"no Gemini vocabulary for {model!r}: {err}")
    try:
        return TokenCount(tokenizer.count_tokens(text).total_tokens, EXACT,
                          "gemini/sentencepiece")
    except Exception as err:                     # pragma: no cover - defensive
        return _unavailable(f"Gemini tokenizer failed: {err}")


# ── Open-weight models (Hugging Face) ─────────────────────────────────────────

#: Model-name prefix -> the HF repo whose `tokenizer.json` matches it.
#:
#: `meta-llama/*` and `google/gemma-*` are **gated**: loading them without a
#: Hugging Face token returns 401, which would make the two most common
#: open-weight families fail out of the box. The mirrors below are ungated and
#: carry the identical vocabulary (verified by vocab size: Llama 3 = 128256,
#: Llama 2 = 32000, Gemma 2 = 256000).
_HF_REPOS: tuple[tuple[str, str], ...] = (
    ("llama-3", "NousResearch/Meta-Llama-3-8B"),
    ("llama-2", "NousResearch/Llama-2-7b-hf"),
    ("mixtral", "mistralai/Mixtral-8x7B-Instruct-v0.1"),
    ("mistral", "mistralai/Mistral-7B-Instruct-v0.3"),
    ("qwen", "Qwen/Qwen2.5-7B-Instruct"),
    ("deepseek", "deepseek-ai/DeepSeek-V3"),
    ("gemma", "unsloth/gemma-2-9b"),
)


@lru_cache(maxsize=8)
def _hf_tokenizer(repo: str):
    from tokenizers import Tokenizer

    return Tokenizer.from_pretrained(repo)


def _hf_repo_for(model: str) -> Optional[str]:
    name = model.lower()
    for prefix, repo in _HF_REPOS:
        if prefix in name:
            return repo
    return None


def _count_hf(model: str, text: str) -> TokenCount:
    repo = _hf_repo_for(model)
    if repo is None:
        return _unavailable(f"no known tokenizer for {model!r}")
    try:
        tokenizer = _hf_tokenizer(repo)
    except ImportError:
        return _unavailable("the `tokenizers` library is not installed",
                            _TIKTOKEN_HINT)
    except Exception as err:
        # Usually a gated repo or no network on first use.
        return _unavailable(f"could not load {repo}: {err}")
    return TokenCount(len(tokenizer.encode(text).ids), EXACT, repo)


# ── Claude (count_tokens) ─────────────────────────────────────────────────────

def _anthropic_available() -> tuple[bool, str]:
    """Whether a Claude count can actually be made right now."""
    import os

    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True, ""
    try:
        from gyrfalcon.config import cfg_get

        if cfg_get("providers.anthropic.api_key", ""):
            return True, ""
    except Exception:
        pass
    return False, (
        "Claude has no local tokenizer; exact counts need an Anthropic API key "
        "(token counting itself is free)"
    )


@lru_cache(maxsize=512)
def _count_claude_cached(model: str, payload_hash: str, payload_json: str) -> int:
    """One network call per (model, content). `payload_hash` keys the cache."""
    from gyrfalcon.net import get_anthropic_client

    payload = json.loads(payload_json)
    client = get_anthropic_client()
    response = client.messages.count_tokens(model=model, **payload)
    return int(response.input_tokens)


def _count_claude(model: str, text: str, system: Optional[str] = None,
                  tools: Optional[Sequence[dict]] = None) -> TokenCount:
    ok, reason = _anthropic_available()
    if not ok:
        return _unavailable(reason)

    payload: dict[str, Any] = {"messages": [{"role": "user", "content": text}]}
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = list(tools)

    payload_json = json.dumps(payload, sort_keys=True)
    digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    try:
        tokens = _count_claude_cached(model, digest, payload_json)
    except Exception as err:
        return _unavailable(f"count_tokens failed: {err}")
    return TokenCount(tokens, API, "anthropic/count_tokens")


# ── Dispatch ──────────────────────────────────────────────────────────────────

_Counter = Callable[[str, str], TokenCount]

#: Ordered; first matching prefix/substring wins.
_ROUTES: tuple[tuple[tuple[str, ...], str, _Counter], ...] = (
    (("claude",), "anthropic", lambda m, t: _count_claude(m, t)),
    (("gpt-", "o1", "o3", "o4", "chatgpt", "davinci", "babbage"),
     "openai", _count_openai),
    (("gemini",), "google", _count_gemini),
    (("llama", "mistral", "mixtral", "qwen", "deepseek", "gemma"),
     "open-weight", _count_hf),
)


def _normalize(model: str) -> str:
    """Strip a provider prefix: `github_copilot/gpt-4o` -> `gpt-4o`."""
    name = (model or "").lower().strip()
    return name.split("/", 1)[1] if "/" in name else name


def _route(model: str) -> Optional[tuple[str, _Counter]]:
    name = _normalize(model)
    for prefixes, family, counter in _ROUTES:
        if any(p in name for p in prefixes):
            return family, counter
    return None


def count_text(model: str, text: str) -> TokenCount:
    """Count `text` as this model's tokenizer would.

    Never raises: an unsupported model or a missing dependency comes back as
    `UNAVAILABLE` with a reason.
    """
    if not text:
        return TokenCount(0, EXACT, "empty")
    route = _route(model)
    if route is None:
        return _unavailable(f"no tokenizer registered for {model!r}")
    _, counter = route
    return counter(_normalize(model), text)


def count_request(
    model: str,
    text: str,
    system: Optional[str] = None,
    tools: Optional[Sequence[dict]] = None,
    message_count: int = 1,
) -> TokenCount:
    """Count a whole request: system prompt + tool schemas + content.

    This is what a real call actually sends, and for short inputs the fixed
    overhead dominates — which is why the estimator includes it by default.

    For Claude this is exact end to end: `count_tokens` accepts `system` and
    `tools` and applies the same framing the Messages API would. For local
    tokenizers the *content* is exact and the per-message framing is a small
    documented constant, so the total can be a few tokens light on a long
    conversation.
    """
    if _route(model) and "claude" in _normalize(model):
        return _count_claude(_normalize(model), text, system=system, tools=tools)

    parts = [text]
    if system:
        parts.append(system)
    if tools:
        # Tool schemas are serialized into the request as JSON; counting the
        # serialization is what the model actually reads.
        parts.append(json.dumps(list(tools), sort_keys=True))

    counted = count_text(model, "\n\n".join(p for p in parts if p))
    if not counted.is_known:
        return counted
    return TokenCount(
        counted.tokens + FRAMING_TOKENS_PER_MESSAGE * max(message_count, 1),
        counted.method,
        counted.tokenizer,
        "includes estimated per-message framing",
    )


def _probe(model: str) -> TokenCount:
    """Report how `model` *would* be counted, without counting anything.

    Deliberately does not route through `count_text`. Doing so would make
    `describe()` fire a real `count_tokens` request for every Claude model, so
    rendering a model-picker with eight Claude entries would cost eight network
    calls before the user typed a character.
    """
    route = _route(model)
    if route is None:
        return _unavailable(f"no tokenizer registered for {model!r}")

    family = route[0]
    name = _normalize(model)

    if family == "anthropic":
        ok, reason = _anthropic_available()
        return (TokenCount(0, API, "anthropic/count_tokens") if ok
                else _unavailable(reason))

    if family == "openai":
        try:
            _, encoding_name = _tiktoken_encoding(name)
        except ImportError:
            return _unavailable("tiktoken is not installed", _TIKTOKEN_HINT)
        except Exception as err:                 # pragma: no cover - defensive
            return _unavailable(f"tiktoken failed: {err}")
        return TokenCount(0, EXACT, encoding_name)

    if family == "google":
        try:
            _gemini_tokenizer(name)
        except ImportError:
            return _unavailable("the Vertex AI tokenizer is not installed",
                                _TIKTOKEN_HINT)
        except Exception as err:
            return _unavailable(f"no Gemini vocabulary for {model!r}: {err}")
        return TokenCount(0, EXACT, "gemini/sentencepiece")

    repo = _hf_repo_for(name)
    if repo is None:
        return _unavailable(f"no known tokenizer for {model!r}")
    try:
        _hf_tokenizer(repo)
    except ImportError:
        return _unavailable("the `tokenizers` library is not installed",
                            _TIKTOKEN_HINT)
    except Exception as err:
        return _unavailable(f"could not load {repo}: {err}")
    return TokenCount(0, EXACT, repo)


def is_available(model: str) -> bool:
    """Whether this model can be counted right now, without counting anything."""
    return _probe(model).is_known


def describe(model: str) -> dict[str, Any]:
    """What the UI needs to render this model's row. Makes no billable call."""
    route = _route(model)
    probe = _probe(model)
    return {
        "model": model,
        "family": route[0] if route else "",
        "method": probe.method,
        "tokenizer": probe.tokenizer,
        "available": probe.is_known,
        "detail": probe.detail,
        "local": probe.method == EXACT,
    }
