"""Event publisher — broadcasts agent events to TUI/WebSocket subscribers."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("event_publisher")



class EventType(str, Enum):
    """Agent event types published to subscribers."""
    AGENT_START = "agent.start"
    AGENT_DONE = "agent.done"
    AGENT_ERROR = "agent.error"
    TOKEN_DELTA = "token.delta"
    TOKEN_DONE = "token.done"
    TOOL_START = "tool.start"
    TOOL_RESULT = "tool.result"
    TOOL_ERROR = "tool.error"
    APPROVAL_REQUEST = "approval.request"
    APPROVAL_RESPONSE = "approval.response"
    SESSION_CREATED = "session.created"
    SESSION_RESUMED = "session.resumed"
    THINKING_START = "thinking.start"
    THINKING_DONE = "thinking.done"
    STATUS_UPDATE = "status.update"
    MEMORY_UPDATE = "memory.update"
    SKILL_ACTIVATED = "skill.activated"


@dataclass
class AgentEvent:
    """A single event emitted by the agent."""
    type: EventType
    session_id: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        logger.debug("Beginning of to_json")
        return json.dumps({
            "type": self.type.value,
            "session_id": self.session_id,
            "data": self.data,
            "timestamp": self.timestamp,
        })


Subscriber = Callable[[AgentEvent], Any]


class EventPublisher:
    """Pub/sub event bus for agent events."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Subscriber]] = {}
        self._global_subscribers: list[Subscriber] = []
        self._event_queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=1000)
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def subscribe(self, event_type: Optional[EventType], callback: Subscriber) -> Callable[[], None]:
        """Subscribe to events. If event_type is None, subscribes to all events.
        Returns an unsubscribe function."""
        logger.debug("Beginning of subscribe")
        if event_type is None:
            self._global_subscribers.append(callback)
            return lambda: self._global_subscribers.remove(callback)
        else:
            key = event_type.value
            if key not in self._subscribers:
                self._subscribers[key] = []
            self._subscribers[key].append(callback)
            return lambda: self._subscribers[key].remove(callback)

    async def publish(self, event: AgentEvent) -> None:
        """Publish an event to all matching subscribers."""
        # Notify type-specific subscribers
        key = event.type.value
        for cb in self._subscribers.get(key, []):
            try:
                result = cb(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass  # Don't let subscriber errors break the publisher

        # Notify global subscribers
        for cb in self._global_subscribers:
            try:
                result = cb(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass

    def emit(self, event_type: EventType, session_id: str, **data: Any) -> None:
        """Convenience: create and queue an event (fire-and-forget from sync code)."""
        logger.debug("Beginning of emit")
        event = AgentEvent(type=event_type, session_id=session_id, data=data)
        try:
            self._event_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass  # Drop events if queue is full

    async def start(self) -> None:
        """Start the background event dispatch loop."""
        self._running = True
        self._task = asyncio.create_task(self._dispatch_loop())

    async def stop(self) -> None:
        """Stop the event dispatch loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _dispatch_loop(self) -> None:
        """Background loop that drains queued events."""
        while self._running:
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=1.0)
                await self.publish(event)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break


# Singleton
_publisher: Optional[EventPublisher] = None


def get_event_publisher() -> EventPublisher:
    """Get or create the global event publisher."""
    logger.debug("Beginning of get_event_publisher")
    global _publisher
    if _publisher is None:
        _publisher = EventPublisher()
    return _publisher
