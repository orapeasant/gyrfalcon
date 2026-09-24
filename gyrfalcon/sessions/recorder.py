"""Record one LLM call's token usage, without ever breaking the call.

Spec: §19.7.

This is the bridge between the agent loop and `session_usage`. It is written
defensively on purpose: a conversation must not fail because a cost-accounting
row could not be written. Every failure is logged and swallowed.

It is deliberately *additive* — the legacy SQLite store keeps its running
session totals exactly as before, and this records the per-call detail those
totals cannot express.
"""

from __future__ import annotations

from typing import Any, Optional

from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("sessions.recorder")

_disabled = False


def record_call(
    session_id: Optional[str],
    usage: Any,
    model: str = "",
    provider: str = "",
    cost_usd: float = 0.0,
    cache_ttl: str = "5m",
    source: str = "",
) -> None:
    """Persist one call's usage. Never raises.

    After a first hard failure this turns itself off for the process: if the
    shared database is unreachable, retrying on every single LLM call would
    turn one broken dependency into a log flood and a latency tax on each turn.
    """
    global _disabled
    if _disabled or not session_id or usage is None:
        return

    try:
        from gyrfalcon.pricing import catalog as price_catalog
        from gyrfalcon.sessions.store import get_session_store

        store = get_session_store()
        store.ensure_session(session_id, model=model, source=source)
        meta = price_catalog.meta()
        version = "refreshed" if meta.get("refreshed_applied") else "bundled"
        if meta.get("overlay_applied"):
            version += "+overlay"
        store.record_usage(
            session_id, usage, model=model, provider=provider,
            cost_usd=cost_usd, cache_ttl=cache_ttl, catalog_version=version,
        )
    except Exception as err:
        _disabled = True
        logger.warning(
            "per-call usage recording is off for this process (%s); "
            "session totals are unaffected", err,
        )


def reset_for_tests() -> None:
    """Re-enable recording after a deliberately failing test."""
    global _disabled
    _disabled = False
