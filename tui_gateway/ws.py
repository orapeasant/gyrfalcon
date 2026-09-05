"""WebSocket transport for TUI gateway — handles ws:// connections."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class WebSocketSession:
    """Represents a single WebSocket client connection."""

    def __init__(self, ws: Any, session_id: str) -> None:
        self.ws = ws
        self.session_id = session_id
        self._send_lock = asyncio.Lock()

    async def send(self, message: dict[str, Any]) -> None:
        """Send a JSON-RPC message to the client."""
        async with self._send_lock:
            try:
                await self.ws.send(json.dumps(message))
            except Exception as e:
                logger.warning("WebSocket send error for %s: %s", self.session_id, e)

    async def send_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Send a server-initiated event (notification)."""
        await self.send({
            "jsonrpc": "2.0",
            "method": "event",
            "params": {"type": event_type, **data},
        })


class WebSocketTransport:
    """WebSocket server transport for the TUI gateway.

    Handles multiple concurrent WebSocket connections, routing JSON-RPC
    messages to the gateway server handler.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9120,
        handler: Optional[Callable] = None,
    ) -> None:
        self.host = host
        self.port = port
        self._handler = handler
        self._sessions: dict[str, WebSocketSession] = {}
        self._server: Optional[Any] = None

    @property
    def connected_count(self) -> int:
        return len(self._sessions)

    async def start(self) -> None:
        """Start the WebSocket server."""
        try:
            import websockets
        except ImportError:
            logger.error("websockets package not installed — ws transport unavailable")
            return

        self._server = await websockets.serve(
            self._handle_connection,
            self.host,
            self.port,
        )
        logger.info("WebSocket transport listening on ws://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        """Stop the WebSocket server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        # Close all sessions
        for session in list(self._sessions.values()):
            try:
                await session.ws.close()
            except Exception:
                pass
        self._sessions.clear()

    async def broadcast(self, event_type: str, data: dict[str, Any], session_id: Optional[str] = None) -> None:
        """Broadcast an event to all connected clients (or a specific session)."""
        targets = (
            [self._sessions[session_id]]
            if session_id and session_id in self._sessions
            else list(self._sessions.values())
        )
        for session in targets:
            await session.send_event(event_type, data)

    async def _handle_connection(self, ws: Any, path: str = "/") -> None:
        """Handle a new WebSocket connection."""
        import uuid
        session_id = str(uuid.uuid4())
        session = WebSocketSession(ws, session_id)
        self._sessions[session_id] = session

        logger.info("WebSocket client connected: %s", session_id)

        # Send welcome
        await session.send({
            "jsonrpc": "2.0",
            "method": "connected",
            "params": {"session_id": session_id},
        })

        try:
            async for raw_message in ws:
                try:
                    message = json.loads(raw_message)
                    if self._handler:
                        response = await self._handler(message, session_id)
                        if response:
                            await session.send(response)
                except json.JSONDecodeError:
                    await session.send({
                        "jsonrpc": "2.0",
                        "error": {"code": -32700, "message": "Parse error"},
                        "id": None,
                    })
                except Exception as e:
                    logger.exception("Error handling WebSocket message")
                    await session.send({
                        "jsonrpc": "2.0",
                        "error": {"code": -32603, "message": str(e)},
                        "id": None,
                    })
        except Exception:
            pass
        finally:
            del self._sessions[session_id]
            logger.info("WebSocket client disconnected: %s", session_id)
