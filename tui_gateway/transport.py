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

    def start(self) -> None:
        logger.debug("Beginning of start")
        self._running = True

    def stop(self) -> None:
        logger.debug("Beginning of stop")
        self._running = False

    def send(self, message: str, flush: bool = True) -> None:
        """Thread-safe send: agent runs in a background thread, so we must
        schedule the coroutine on the event loop with run_coroutine_threadsafe.

        flush=False (hot streaming path) schedules and returns immediately. Waiting
        on the future would stall the agent thread for a full event-loop round trip
        per token, so it could not read the next chunk off the LLM stream until the
        previous one had been framed and written.
        """
        logger.debug("Beginning of send")
        import asyncio
        if not (self._loop and self._loop.is_running()):
            logger.warning("WebSocketTransport.send: no running event loop")
            return

        future = asyncio.run_coroutine_threadsafe(
            self._ws.send_text(message), self._loop
        )

        if not flush:
            future.add_done_callback(self._log_send_failure)
            return

        try:
            future.result(timeout=10)
        except Exception as e:
            logger.warning(f"WebSocketTransport.send failed: {e}")

    @staticmethod
    def _log_send_failure(future) -> None:
        """Surface errors from fire-and-forget sends, which nobody awaits."""
        try:
            future.result()
        except Exception as e:
            logger.warning(f"WebSocketTransport.send failed: {e}")

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
