"""`gyrfalcon pricing` — inspect and refresh the model price catalog.

Spec: §19.5.2. Refresh is deliberately a user action rather than something that
happens on the hot path: it reaches the network, and an estimator that silently
stalls on a slow feed is worse than one quoting last week's prices.
"""

from __future__ import annotations

from typing import Optional

from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("pricing_cmd")


def _age(timestamp: Optional[float]) -> str:
    if not timestamp:
        return "never refreshed (using the bundled snapshot)"
    import time

    days = (time.time() - timestamp) / 86400
    if days < 1:
        return "refreshed today"
    return f"refreshed {int(days)} day{'s' if int(days) != 1 else ''} ago"


def _show_model(model: str) -> None:
    from gyrfalcon.pricing import break_even_calls, get_rates

    r = get_rates(model)
    unit = "credits" if r.billing == "credits" else "USD"
    print(f"\n  {model}")
    print(f"    resolved to : {r.model}  (source: {r.source})")
    if r.provider:
        print(f"    provider    : {r.provider}")
    print(f"    billing     : {r.billing}")
    print(f"\n    per 1K tokens ({unit}):")
    print(f"      input       : {r.input:.6f}")
    print(f"      output      : {r.output:.6f}")
    print(f"      cache read  : {r.effective_cache_read():.6f}"
          f"{'' if r.cache_read is not None else '   (derived)'}")
    write = r.effective_cache_write("5m")
    if write > 0:
        print(f"      cache write : {write:.6f} (5m)   "
              f"{r.effective_cache_write('1h'):.6f} (1h)")
        n = break_even_calls(r, "5m")
        if n:
            print(f"\n    caching pays off after {n:.2f} uses of the same prefix")
    else:
        print("      cache write : free (automatic prefix caching)")
    if r.context_window:
        print(f"\n    context window: {r.context_window:,} tokens")
    if r.tier:
        print(f"    tiered above  : {r.tier.above_input_tokens:,} input tokens")
    print()


def run_pricing_cli(action: str = "show", model: Optional[str] = None) -> None:
    from gyrfalcon.pricing import catalog as cat

    if action == "refresh":
        print(f"Refreshing price catalog from {cat.FEED_URL} ...")
        result = cat.refresh()
        if not result.get("ok"):
            print(f"  failed: {result.get('error')}")
            print("  the existing catalog is unchanged.")
            return
        if result.get("unchanged"):
            print("  already up to date (304).")
            return
        print(f"  wrote {result['models']} models to {result['path']}")
        return

    meta = cat.meta()
    print("\nPrice catalog")
    print(f"  bundled models   : {meta['bundled']}")
    if meta["refreshed_applied"]:
        print(f"  refreshed layer  : +{meta['refreshed_added']} new, "
              f"{_age(meta['refreshed_at'])}")
    else:
        print(f"  refreshed layer  : {_age(meta['refreshed_at'])}")
    print(f"  local overlay    : {'applied' if meta['overlay_applied'] else 'none'}"
          f"  ({cat.overlay_path()})")
    print(f"  total models     : {meta['total']}")

    if model:
        _show_model(model)
    else:
        print("\n  Pass --model <id> to see the resolved rates for one model.")
        print("  Run `gyrfalcon pricing refresh` to update from the public feed.\n")
