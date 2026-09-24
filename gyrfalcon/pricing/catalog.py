"""Loading, merging and matching the price catalog.

Spec: §19.5.2.

Three layers, lowest priority first:

1. **Bundled** — `gyrfalcon/pricing/catalog.json`, checked in. Means an offline
   box still has rates, and a fresh install needs no network.
2. **Refreshed** — `<profile>/pricing/catalog.json`, written by
   `gyrfalcon pricing refresh`. Per-profile, because `GYRFALCON_HOME` selects
   the profile and nothing here may hardcode `~/.gyrfalcon`.
3. **Overlay** — `<profile>/pricing.yaml`, hand-written. Wins over both, so an
   organisation with negotiated rates is not stuck with list price.

The refreshed layer is fetched from a third-party feed. It is **data, never
code**: parsed defensively, coerced to floats, unknown keys ignored, and a
malformed entry dropped rather than raising. A failed refresh leaves the
previous catalog in place.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.pricing.rates import DEFAULT_RATES, ModelRates, Tier

logger = get_logger("pricing.catalog")

FEED_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

_BUNDLED = Path(__file__).resolve().parent / "catalog.json"

_cache: Optional[dict[str, ModelRates]] = None
_cache_meta: dict[str, Any] = {}


# ── Paths ─────────────────────────────────────────────────────────────────────

def _profile_dir() -> Path:
    from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home

    return Path(get_gyrfalcon_home())


def refreshed_path() -> Path:
    return _profile_dir() / "pricing" / "catalog.json"


def overlay_path() -> Path:
    return _profile_dir() / "pricing.yaml"


# ── Parsing ───────────────────────────────────────────────────────────────────

def _float(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _entry_to_rates(model: str, raw: Any, source: str) -> Optional[ModelRates]:
    """Build `ModelRates` from one catalog entry, or None if unusable."""
    if not isinstance(raw, dict):
        return None
    input_rate = _float(raw.get("input"))
    if input_rate is None:
        return None

    tier = None
    raw_tier = raw.get("tier")
    if isinstance(raw_tier, dict) and raw_tier.get("above_input_tokens"):
        try:
            tier = Tier(
                above_input_tokens=int(raw_tier["above_input_tokens"]),
                input=_float(raw_tier.get("input")),
                output=_float(raw_tier.get("output")),
                cache_read=_float(raw_tier.get("cache_read")),
                cache_write_5m=_float(raw_tier.get("cache_write_5m")),
            )
        except (TypeError, ValueError):
            tier = None

    context_window = None
    try:
        if raw.get("context_window"):
            context_window = int(raw["context_window"])
    except (TypeError, ValueError):
        context_window = None

    return ModelRates(
        model=model,
        provider=str(raw.get("provider") or ""),
        input=input_rate,
        output=_float(raw.get("output")) or 0.0,
        cache_read=_float(raw.get("cache_read")),
        cache_write_5m=_float(raw.get("cache_write_5m")),
        cache_write_1h=_float(raw.get("cache_write_1h")),
        context_window=context_window,
        billing="credits" if raw.get("billing") == "credits" else "tokens",
        tier=tier,
        source=source,
    )


def _load_json_layer(path: Path, source: str) -> dict[str, ModelRates]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        logger.warning("pricing: ignoring unreadable %s catalog (%s)", source, err)
        return {}

    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, dict):
        logger.warning("pricing: %s catalog has no 'models' map", source)
        return {}

    out: dict[str, ModelRates] = {}
    for model, raw in models.items():
        rates = _entry_to_rates(str(model), raw, source)
        if rates is not None:
            out[str(model).lower()] = rates
    return out


def _load_overlay() -> dict[str, ModelRates]:
    path = overlay_path()
    if not path.is_file():
        return {}
    try:
        import yaml

        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, ImportError) as err:
        logger.warning("pricing: ignoring unreadable overlay (%s)", err)
        return {}
    except Exception as err:                      # yaml.YAMLError and friends
        logger.warning("pricing: ignoring malformed overlay (%s)", err)
        return {}

    models = payload.get("models", payload) if isinstance(payload, dict) else {}
    out: dict[str, ModelRates] = {}
    if isinstance(models, dict):
        for model, raw in models.items():
            rates = _entry_to_rates(str(model), raw, "overlay")
            if rates is not None:
                out[str(model).lower()] = rates
    return out


# ── The merged catalog ────────────────────────────────────────────────────────

def load(force: bool = False) -> dict[str, ModelRates]:
    """Return the merged catalog, keyed by lowercased model id."""
    global _cache, _cache_meta
    if _cache is not None and not force:
        return _cache

    merged: dict[str, ModelRates] = {}
    merged.update(_load_json_layer(_BUNDLED, "bundled"))
    bundled_count = len(merged)
    merged.update(_load_json_layer(refreshed_path(), "refreshed"))
    after_refresh = len(merged)
    overlay = _load_overlay()
    merged.update(overlay)

    _cache = merged
    _cache_meta = {
        "bundled": bundled_count,
        "refreshed_added": after_refresh - bundled_count,
        "refreshed_applied": refreshed_path().is_file(),
        "overlay_applied": bool(overlay),
        "total": len(merged),
        "refreshed_at": _refreshed_at(),
    }
    logger.debug("pricing: %d models loaded (%s)", len(merged), _cache_meta)
    return merged


def _refreshed_at() -> Optional[float]:
    path = refreshed_path()
    try:
        return path.stat().st_mtime if path.is_file() else None
    except OSError:
        return None


def invalidate() -> None:
    """Drop the memoized catalog — called after a successful refresh."""
    global _cache
    _cache = None


def meta() -> dict[str, Any]:
    load()
    return dict(_cache_meta)


# ── Matching ──────────────────────────────────────────────────────────────────

def _candidates(model: str) -> list[str]:
    """Names to try, most specific first.

    Model ids reach us in several spellings: bare (`gpt-4o`), provider-prefixed
    (`github_copilot/gpt-4o`), and dotted-vs-dashed version suffixes
    (`claude-sonnet-4.5` vs `claude-sonnet-4-5`). Normalising here means the
    catalog does not need an entry per spelling.
    """
    name = (model or "").lower().strip()
    out = [name]
    if "/" in name:
        out.append(name.split("/", 1)[1])
    for candidate in list(out):
        swapped = candidate.replace(".", "-")
        if swapped != candidate:
            out.append(swapped)
    return out


_COPILOT_PREFIXES = ("github_copilot/", "copilot/")

# A dated-release suffix: `-20250514`, optionally with a platform tag
# such as Bedrock's `-v1:0`. Deliberately *not* `-5`, which is a
# different model rather than the same one with a release date.
_DATE_SUFFIX = re.compile(r"^-\d{6,8}([-@].*)?$")


def _as_credits(rates: ModelRates) -> ModelRates:
    """Mark rates as credit-billed without altering the numbers.

    GitHub bills AI Credits *"based on token usage"* at the published per-model
    API rates, so the rates are the vendor's — only the unit shown to the user
    changes.
    """
    return replace(rates, billing="credits")


def get_rates(model: str) -> ModelRates:
    """Rates for a model: exact match, else longest-prefix, else the default.

    Copilot-routed ids get one extra step. The upstream feed lists most
    `github_copilot/*` models with **no rates at all** (29 of 31 at the time of
    writing) — it has not caught up with Copilot's move to token billing. Rather
    than report those as free, which is the one answer guaranteed to be wrong,
    the id falls back to the underlying vendor model's rates and is marked
    credit-billed. That matches how GitHub describes the charge.
    """
    catalog = load()
    name = (model or "").lower().strip()
    via_copilot = name.startswith(_COPILOT_PREFIXES)

    for candidate in _candidates(model):
        if candidate in catalog:
            found = catalog[candidate]
            # A prefixed entry that exists but carries no real rate is the
            # feed's placeholder; keep looking at the bare vendor name.
            if found.input > 0:
                return _as_credits(found) if via_copilot else found

    # Direction 1 — the request is *more* specific than a catalog key:
    # `claude-sonnet-4-5-20250929` finds `claude-sonnet-4-5`. Longest key wins,
    # so it does not settle for the shorter, wrong `claude-sonnet-4`.
    best: Optional[ModelRates] = None
    best_len = 0
    for candidate in _candidates(model):
        for key, rates in catalog.items():
            if candidate.startswith(key) and len(key) > best_len:
                best, best_len = rates, len(key)
    if best is not None:
        return _as_credits(best) if via_copilot else best

    # Direction 2 — the request is a *family* name and the catalog only carries
    # dated releases: `claude-sonnet-4` exists solely as
    # `claude-sonnet-4-20250514`. Only a date suffix counts here. Accepting any
    # extension would let `claude-sonnet-4` match `claude-sonnet-4-5`, quoting
    # one model's price for another — the failure mode this whole module exists
    # to prevent. Earliest date wins, being the closest to the bare name.
    dated: Optional[tuple[str, ModelRates]] = None
    for candidate in _candidates(model):
        for key, rates in catalog.items():
            if not key.startswith(candidate) or key == candidate:
                continue
            if not _DATE_SUFFIX.match(key[len(candidate):]):
                continue
            if dated is None or key < dated[0]:
                dated = (key, rates)
    if dated is not None:
        logger.debug("pricing: %r resolved to dated release %r", model, dated[0])
        return _as_credits(dated[1]) if via_copilot else dated[1]

    logger.debug("pricing: no catalog entry for %r, using default rates", model)
    # Carry the requested name onto the fallback: `ModelRates` infers whether a
    # model is Anthropic from its name, and dropping it here would silently
    # price an unknown Claude model with OpenAI cache semantics (free writes).
    fallback = replace(DEFAULT_RATES, model=name or DEFAULT_RATES.model)
    return _as_credits(fallback) if via_copilot else fallback


# ── Refresh ───────────────────────────────────────────────────────────────────

def refresh(url: str = FEED_URL, timeout: float = 60.0) -> dict[str, Any]:
    """Fetch the feed and write the refreshed layer. Never raises on failure.

    Goes through `gyrfalcon.net.httpx_request` so it inherits the project's
    direct-first, proxy-fallback behaviour. Conditional on ETag, so a no-op
    refresh costs one 304.
    """
    from gyrfalcon.net import httpx_request

    target = refreshed_path()
    headers = {}
    etag_file = target.with_suffix(".etag")
    try:
        if etag_file.is_file():
            headers["If-None-Match"] = etag_file.read_text(encoding="utf-8").strip()
    except OSError:
        pass

    try:
        response = httpx_request("GET", url, headers=headers, timeout=timeout,
                                 follow_redirects=True)
    except Exception as err:
        logger.warning("pricing refresh failed (%s); keeping existing catalog", err)
        return {"ok": False, "error": str(err), "models": 0, "unchanged": False}

    if response.status_code == 304:
        return {"ok": True, "unchanged": True, "models": len(load()),
                "source": url}
    if response.status_code != 200:
        logger.warning("pricing refresh: HTTP %s", response.status_code)
        return {"ok": False, "error": f"HTTP {response.status_code}",
                "models": 0, "unchanged": False}

    try:
        feed = response.json()
    except ValueError as err:
        logger.warning("pricing refresh: feed is not JSON (%s)", err)
        return {"ok": False, "error": "invalid JSON", "models": 0,
                "unchanged": False}

    models = _convert_feed(feed)
    if not models:
        logger.warning("pricing refresh: feed produced no usable models")
        return {"ok": False, "error": "no usable models", "models": 0,
                "unchanged": False}

    payload = {
        "source": url,
        "fetched_at": time.time(),
        "models": models,
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a refresh interrupted midway must not leave a
        # truncated catalog behind for the next startup to choke on.
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
        tmp.replace(target)
        etag = response.headers.get("etag")
        if etag:
            etag_file.write_text(etag, encoding="utf-8")
    except OSError as err:
        logger.warning("pricing refresh: could not write catalog (%s)", err)
        return {"ok": False, "error": str(err), "models": 0, "unchanged": False}

    invalidate()
    return {"ok": True, "unchanged": False, "models": len(models),
            "source": url, "path": str(target)}


def _convert_feed(feed: Any) -> dict[str, dict]:
    """Third-party feed -> catalog entries. Tolerant by construction."""
    if not isinstance(feed, dict):
        return {}

    def per_1k(raw: dict, key: str) -> Optional[float]:
        value = _float(raw.get(key))
        return round(value * 1000.0, 10) if value is not None else None

    out: dict[str, dict] = {}
    for name, raw in feed.items():
        if not isinstance(raw, dict) or raw.get("mode") != "chat":
            continue
        input_rate = per_1k(raw, "input_cost_per_token")
        if not input_rate:
            continue

        entry: dict[str, Any] = {
            "provider": raw.get("litellm_provider") or "",
            "input": input_rate,
            "output": per_1k(raw, "output_cost_per_token") or 0.0,
        }
        for field, key in (
            ("cache_read", "cache_read_input_token_cost"),
            ("cache_write_5m", "cache_creation_input_token_cost"),
            ("cache_write_1h", "cache_creation_input_token_cost_above_1hr"),
        ):
            rate = per_1k(raw, key)
            if rate is not None:
                entry[field] = rate

        try:
            if raw.get("max_input_tokens"):
                entry["context_window"] = int(raw["max_input_tokens"])
        except (TypeError, ValueError):
            pass

        tier: dict[str, Any] = {}
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

        if raw.get("litellm_provider") == "github_copilot":
            entry["billing"] = "credits"

        out[str(name)] = entry
    return out
