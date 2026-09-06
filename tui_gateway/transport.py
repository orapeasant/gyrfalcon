"""Transport abstraction — stdio and WebSocket."""

from __future__ import annotations

import sys
import json
import threading
from abc import ABC, abstractmethod
from typing import Callable, Optional
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("transport")



class BaseTransport(ABC):
    """Transport abstraction for JSON-RPC communication."""

    @abstractmethod
    def start(self) -> None:
        logger.debug("Beginning of start")
        ...

    @abstractmethod
    def stop(self) -> None:
        logger.debug("Beginning of stop")
        ...

    @abstractmethod
    def send(self, message: str, flush: bool = True) -> None:
        logger.debug("Beginning of send")
        ...

    @abstractmethod
    def on_message(self, callback: Callable[[str], None]) -> None:
        logger.debug("Beginning of on_message")
        ...


class StdioTransport(BaseTransport):
    """Newline-delimited JSON-RPC over stdin/stdout."""

    def __init__(self):
        self._callback: Optional[Callable[[str], None]] = None
        self._running = False
        self._write_lock = threading.Lock()

    def start(self) -> None:
        """Read loop on stdin."""
        logger.debug("Beginning of start")
        self._running = True
        while self._running:
            try:
                line = sys.stdin.readline()
                if not line:
                    break
                line = line.strip()
                if line and self._callback:
                    self._callback(line)
            except (EOFError, KeyboardInterrupt):
                break

    def stop(self) -> None:
        logger.debug("Beginning of stop")
        self._running = False

    def send(self, message: str, flush: bool = True) -> None:
        with self._write_lock:
            sys.stdout.write(message + "\n")
            if flush:
                sys.stdout.flush()

    def on_message(self, callback: Callable[[str], None]) -> None:
        logger.debug("Beginning of on_message")
        self._callback = callback


class WebSocketTransport(BaseTransport):
    """JSON-RPC over WebSocket (for dashboard integration)."""

    # Bounded so a stalled client cannot grow the queue without limit.
    _QUEUE_MAXSIZE = 10_000

    def __init__(self, websocket):
        self._ws = websocket
        self._callback: Optional[Callable[[str], None]] = None
        self._running = False
        # Capture the running event loop at construction time (we're inside an
        # async FastAPI handler, so get_running_loop() is safe here).
        import asyncio
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        # Every frame goes through one queue drained by a single task. ws.send_text
        # must never run concurrently: permessage-deflate keeps one stateful zlib
        # compressor per connection, so interleaved sends emit a corrupt stream and
        # the browser fails with "incorrect header check".
        self._queue: Optional[asyncio.Queue] = asyncio.Queue(self._QUEUE_MAXSIZE) if self._loop else None
        self._drain_task = self._loop.create_task(self._drain()) if self._loop else None

    async def _drain(self) -> None:
        """Serialize all outbound frames onto the socket, in enqueue order."""
        import asyncio
        while True:
            try:
                message, done = await self._queue.get()
            except asyncio.CancelledError:
                raise
            if message is None:      # shutdown sentinel
                if done:
                    done.set()
                return
            try:
                await self._ws.send_text(message)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"WebSocketTransport.send failed: {e}")
            finally:
                if done:
                    done.set()

    def start(self) -> None:
        logger.debug("Beginning of start")
        self._running = True

    def stop(self) -> None:
        logger.debug("Beginning of stop")
        self._running = False
        if self._drain_task and self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._drain_task.cancel)

    def send(self, message: str, flush: bool = True) -> None:
        """Thread-safe send: the agent runs in a background thread, so frames are
        handed to the event loop's drain task rather than written here.

        flush=False (hot streaming path) returns as soon as the frame is queued.
        Blocking per token would stall the agent thread for a full event-loop round
        trip, so it could not read the next chunk off the LLM stream until the
        previous one had been written.
        """
        logger.debug("Beginning of send")
        if not (self._loop and self._loop.is_running() and self._queue is not None):
            logger.warning("WebSocketTransport.send: no running event loop")
            return

        # Waiting from inside the loop would block the very task that drains the
        # queue, so on-loop callers always enqueue and return.
        import asyncio
        try:
            on_event_loop = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            on_event_loop = False

        done = threading.Event() if (flush and not on_event_loop) else None

        def _enqueue() -> None:
            try:
                self._queue.put_nowait((message, done))
            except Exception as e:   # QueueFull — client cannot keep up
                logger.warning(f"WebSocketTransport.send dropped a frame: {e}")
                if done:
                    done.set()

        self._loop.call_soon_threadsafe(_enqueue)

        if done and not done.wait(timeout=10):
            logger.warning("WebSocketTransport.send timed out waiting for delivery")

    def on_message(self, callback: Callable[[str], None]) -> None:
        logger.debug("Beginning of on_message")
        self._callback = callback

    async def receive_loop(self) -> None:
        """Async receive loop for WebSocket."""
        self._running = True
        while self._running:
            try:
                data = await self._ws.receive_text()
                if self._callback:
                    self._callback(data)
            except Exception:
                break
