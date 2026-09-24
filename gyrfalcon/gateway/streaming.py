"""Live replies — edit one message as the answer is produced.

Spec 18-slack.md §3.4. Without this a chat platform shows nothing for the whole
turn and then a wall of text; with it the reply grows in place and tool activity
is visible while it happens.

The delicate part is the thread boundary. `AIAgent` fires its callbacks from the
worker thread running the turn, while every platform call is a coroutine that
must run on the event loop. Rather than scheduling a cross-thread call per token
— thousands of them, each racing the last — the worker only appends to a buffer
under a lock, and a pump task on the event loop renders whatever it finds at a
fixed interval. So the platform sees one edit per interval no matter how fast
the model produces tokens, which is also what its rate limits require.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable, Optional

from gyrfalcon.gateway.platforms.base import MessageEvent
from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("gateway.streaming")

#: Slack's chat.update is rate limited per channel (tier 3, ~50/minute), so an
#: edit per second is already at the edge. This leaves headroom.
DEFAULT_INTERVAL_SECONDS = 1.5

#: Don't post a placeholder for the first few characters — a short answer would
#: be posted and immediately finished, costing two API calls to say one thing.
MIN_FIRST_RENDER_CHARS = 40


class StreamingReply:
    """One in-progress reply: buffer written by the worker, rendered by the loop."""

    def __init__(
        self,
        adapter: Any,
        event: MessageEvent,
        *,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        transform: Optional[Callable[[str], str]] = None,
    ):
        self._adapter = adapter
        self._event = event
        self._interval = interval
        #: Applied to every outgoing render — credential scrubbing, in practice.
        #: Partial text is still text, and a token can appear mid-stream.
        self._transform = transform or (lambda t: t)
        self._lock = threading.Lock()
        self._parts: list[str] = []
        self._tool: Optional[str] = None
        self._message_id: Optional[str] = None
        self._rendered = ""
        self._pump: Optional[asyncio.Task] = None
        self._stopped = False

    # -- worker thread --------------------------------------------------------

    def on_delta(self, chunk: str) -> None:
        """Model produced text. Called from the turn's worker thread."""
        if chunk:
            with self._lock:
                self._parts.append(chunk)

    def on_tool(self, name: str, _args: Any = None, phase: str = "start") -> None:
        """A tool started or finished. Called from the turn's worker thread."""
        with self._lock:
            self._tool = name if phase == "start" else None

    # -- event loop -----------------------------------------------------------

    @property
    def started(self) -> bool:
        """Whether anything has actually been posted yet."""
        return self._message_id is not None

    def begin(self) -> None:
        self._pump = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while not self._stopped:
                await asyncio.sleep(self._interval)
                await self._render(final=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed live update must never take the turn down: the final
            # reply is delivered by the caller regardless.
            logger.debug("streaming pump failed", exc_info=True)

    def _snapshot(self, final: bool) -> str:
        with self._lock:
            text = "".join(self._parts)
            tool = self._tool
        if tool and not final:
            text = f"{text}\n\n_…running `{tool}`_" if text.strip() else f"_…running `{tool}`_"
        return text.strip()

    async def _render(self, final: bool) -> None:
        text = self._snapshot(final)
        if not text or text == self._rendered:
            return
        if self._message_id is None:
            if not final and len(text) < MIN_FIRST_RENDER_CHARS:
                return
            result = await self._adapter.send(
                self._event.source.chat_id,
                self._transform(text),
                reply_to=self._anchor,
                metadata={"source": self._event.source},
            )
            if not result.success:
                logger.debug(f"streaming: first post failed ({result.error})")
                return
            self._message_id = result.message_id
        else:
            if not await self._adapter.edit_message(
                self._event.source.chat_id, self._message_id, self._transform(text)
            ):
                return
        self._rendered = text

    @property
    def _anchor(self) -> Optional[str]:
        src = self._event.source
        return src.thread_id or src.message_id or None

    async def finish(self, final_text: str) -> bool:
        """Stop pumping and settle on `final_text`.

        Returns whether the reply was fully delivered here. False means nothing
        was posted (or the last edit failed) and the caller should send it the
        ordinary way — the answer must arrive even if live updating did not work.
        """
        self._stopped = True
        if self._pump is not None:
            self._pump.cancel()
            try:
                await self._pump
            except (asyncio.CancelledError, Exception):
                pass

        if self._message_id is None:
            return False

        text = (final_text or "").strip() or self._snapshot(final=True)
        if not text:
            return False

        # The streamed text is one message; a long answer needs more. Settle the
        # message being edited to the first part and post the rest after it.
        from gyrfalcon.gateway.platforms._mrkdwn import split_message

        chunks = split_message(text)
        if not await self._adapter.edit_message(
            self._event.source.chat_id, self._message_id, self._transform(chunks[0])
        ):
            return False
        for chunk in chunks[1:]:
            result = await self._adapter.send(
                self._event.source.chat_id, self._transform(chunk),
                reply_to=self._anchor, metadata={"source": self._event.source},
            )
            if not result.success:
                return False
        self._rendered = text
        return True

    async def abandon(self) -> None:
        """Give up without settling — the turn failed or was interrupted."""
        self._stopped = True
        if self._pump is not None:
            self._pump.cancel()
            try:
                await self._pump
            except (asyncio.CancelledError, Exception):
                pass
