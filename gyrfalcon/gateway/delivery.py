"""Outbound delivery — sending a message nobody asked for, right now.

Spec 18-slack.md §3.6. Everything else in the gateway is a *reply*: a message
arrives, a turn runs, an answer goes back through the adapter that delivered it.
This is the other direction — a scheduled job finishing at 6am, a flow wanting to
say something — where there is no inbound message and no adapter in hand, only a
target written in a config file.

It exists because three separate callers already wanted it and none could have
it: `scheduler`'s jobs carry a `deliver` field that nothing read,
`tools/send_message_tool.py` calls an `agent_context.gateway` that nothing
implements, and flows have no way to report. One router answers all three.

Callers are on other threads — the scheduler ticks in its own, agent tools run in
the turn executor — while adapters are coroutines belonging to the gateway's
event loop. `deliver_threadsafe` is the bridge, and it is the only correct way in
from outside that loop.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional

from gyrfalcon.gateway.redact import redact_secrets
from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("gateway.delivery")

#: Target meaning "don't send anywhere" — the scheduler's default, where output
#: is written to disk and that is all.
LOCAL = "local"

DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    detail: str = ""

    def __bool__(self) -> bool:
        return self.ok


def parse_target(target: str) -> Optional[tuple[str, str]]:
    """`"slack:C0123"` -> `("slack", "C0123")`. None for local/blank/malformed.

    A leading `@` or `#` on the destination is tolerated and dropped: people
    write `slack:#ops` because that is how the channel is named in Slack, but
    the API wants the bare id.
    """
    if not target:
        return None
    platform, sep, chat = target.strip().partition(":")
    platform, chat = platform.strip().lower(), chat.strip().lstrip("@#")
    # `local` is reserved, with or without a destination after it: it is the
    # scheduler's "write to disk and stop", never the name of a platform.
    if platform == LOCAL or not sep or not platform or not chat:
        return None
    return platform, chat


class DeliveryRouter:
    """Sends to a named platform destination on behalf of something off-loop."""

    def __init__(self, adapters: dict[str, Any], loop: asyncio.AbstractEventLoop):
        self._adapters = adapters
        self._loop = loop

    def describe_targets(self) -> list[str]:
        return sorted(self._adapters)

    async def deliver(self, target: str, text: str) -> DeliveryResult:
        """Send `text` to `target`. Never raises — the caller is usually a
        background job whose own work already succeeded."""
        parsed = parse_target(target)
        if parsed is None:
            return DeliveryResult(False, f"not a deliverable target: {target!r}")
        platform, chat_id = parsed

        adapter = self._adapters.get(platform)
        if adapter is None:
            known = ", ".join(self.describe_targets()) or "none"
            return DeliveryResult(False, f"platform {platform!r} is not connected (connected: {known})")
        if not (text or "").strip():
            return DeliveryResult(False, "nothing to send")

        try:
            result = await adapter.send(chat_id, redact_secrets(text, adapter.secrets()))
        except Exception as exc:
            logger.exception(f"Delivery to {target} raised")
            return DeliveryResult(False, type(exc).__name__)
        if not result.success:
            return DeliveryResult(False, result.error or "send failed")
        return DeliveryResult(True, result.message_id or "")

    def deliver_threadsafe(self, target: str, text: str,
                           timeout: float = DEFAULT_TIMEOUT_SECONDS) -> DeliveryResult:
        """`deliver`, callable from any thread that is not the gateway's loop."""
        if not self._loop.is_running():
            return DeliveryResult(False, "gateway is not running")
        try:
            future = asyncio.run_coroutine_threadsafe(self.deliver(target, text), self._loop)
            return future.result(timeout=timeout)
        except TimeoutError:
            return DeliveryResult(False, f"timed out after {timeout:.0f}s")
        except Exception as exc:
            logger.exception(f"Delivery to {target} could not be scheduled")
            return DeliveryResult(False, type(exc).__name__)


#: The running gateway's router, when there is one. A module-level handle
#: because the callers (a scheduler thread, a tool inside an agent turn) have no
#: path to the runner object and should not have to grow one.
_router: Optional[DeliveryRouter] = None


def set_router(router: Optional[DeliveryRouter]) -> None:
    global _router
    _router = router


def get_router() -> Optional[DeliveryRouter]:
    return _router


def deliver_from_anywhere(target: str, text: str) -> DeliveryResult:
    """Best-effort delivery from a background thread.

    The honest failure matters here: the scheduler only ticks inside the gateway
    process, but a job can also be triggered by hand from the CLI, where no
    gateway — and so no adapter — exists. Saying that plainly is better than a
    silent no-op, which is what this whole area used to be.
    """
    router = get_router()
    if router is None:
        return DeliveryResult(False, "no gateway is running, so there is nothing to deliver through")
    return router.deliver_threadsafe(target, text)
