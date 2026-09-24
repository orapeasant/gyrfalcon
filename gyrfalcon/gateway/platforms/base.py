"""Platform adapter contract.

An adapter's whole job is to translate: platform events in as `MessageEvent`s,
agent replies out through `send`. It holds no agent, no session state and — on
purpose — no access-control decision. Authorisation, rate limiting, queueing,
control commands and toolset selection all live in the runner, once, so a new
adapter cannot forget one of them. (Adapters may consult
`PlatformConfig.allow` to drop strangers cheaply *before* doing work, but the
runner re-checks every event regardless.)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from gyrfalcon.gateway.config import PlatformConfig


@dataclass
class SendResult:
    """Result of sending a message."""
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None


@dataclass
class SessionSource:
    """Identifies where a conversation is happening and who is in it."""
    platform: str
    chat_id: str
    chat_name: str = ""
    user_id: str = ""
    user_name: str = ""
    thread_id: str = ""
    chat_type: str = ""
    #: Workspace / server / tenant on the platform (Slack `team_id`). Kept so
    #: identity can later map it to an org without re-plumbing every adapter.
    guild_id: str = ""
    message_id: str = ""
    is_bot: bool = False

    @property
    def session_key(self) -> str:
        """`platform:chat:thread` — a thread is a conversation (spec D1).

        Deliberately not keyed on the user: two people in one thread share it,
        and a new thread is a fresh context.
        """
        return f"{self.platform}:{self.chat_id}:{self.thread_id}"

    def route_candidates(self) -> list[str]:
        """Names a routing-rule pattern is matched against."""
        names = []
        if self.chat_name:
            names += [f"#{self.chat_name}", self.chat_name]
        if self.chat_id:
            names.append(self.chat_id)
        return names


@dataclass
class MessageEvent:
    """One inbound message, already filtered down to something addressed to us."""
    source: SessionSource
    text: str
    #: Platform's id for this delivery. Lets the runner drop redeliveries.
    event_id: str = ""


@dataclass(frozen=True)
class ApprovalInteraction:
    """Someone answered an approval question through a platform control.

    The adapter reports *who* clicked; whether they are allowed to decide is the
    runner's call, as with every other authorisation in the gateway.
    """
    request_id: str
    approved: bool
    user_id: str
    chat_id: str = ""
    platform: str = ""


#: What an adapter reports back about a message, for platforms that can show it
#: (a reaction, a typing indicator). The runner never depends on it working.
QUEUED, WORKING, DONE, FAILED = "queued", "working", "done", "failed"

MessageCallback = Callable[[MessageEvent], Awaitable[None]]


class BasePlatformAdapter(ABC):
    """Abstract base class for platform adapters."""

    def __init__(self, config: PlatformConfig):
        self.config = config
        self._message_callback: Optional[MessageCallback] = None
        self._decision_callback: Optional[Callable[[ApprovalInteraction], Awaitable[None]]] = None

    @abstractmethod
    async def connect(self) -> bool:
        """Connect to the platform. Returns True on success."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Graceful disconnection."""
        ...

    @abstractmethod
    async def send(
        self, chat_id: str, content: str,
        reply_to: str | None = None, metadata: dict | None = None
    ) -> SendResult:
        """Send a message to a chat."""
        ...

    @abstractmethod
    async def get_chat_info(self, chat_id: str) -> dict:
        """Get information about a chat/channel."""
        ...

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Platform identifier."""
        ...

    def secrets(self) -> list[str]:
        """Credential values this adapter holds, so the runner can scrub them
        from anything it is about to post. Pattern-based scrubbing catches
        well-known token shapes; this catches whatever shape the platform uses."""
        return []

    def on_message(self, callback: MessageCallback) -> None:
        """Register the runner's inbound handler."""
        self._message_callback = callback

    def on_decision(self, callback: Callable[[ApprovalInteraction], Awaitable[None]]) -> None:
        """Register the runner's handler for approval answers."""
        self._decision_callback = callback

    async def settle_approval(self, request_id: str, text: str) -> None:
        """Record the outcome where the question was asked. Best effort."""
        return None

    async def dispatch(self, event: MessageEvent) -> None:
        """Hand an inbound event to the runner. Adapters call this, never the agent."""
        if self._message_callback is not None:
            await self._message_callback(event)

    async def edit_message(self, chat_id: str, message_id: str, content: str) -> bool:
        """Edit an existing message (for streaming updates)."""
        return False

    async def ask_approval(self, request: Any, source: SessionSource) -> bool:
        """Put a dangerous tool call to the people in this conversation.

        The default asks in plain text and relies on `!approve` / `!deny`, so
        every adapter can do this from the day it is written; a platform with
        interactive controls (Slack's buttons) overrides it. Returns whether the
        question was delivered — False means nobody was asked, which the broker
        treats as a refusal rather than leaving the turn hanging.
        """
        result = await self.send(
            source.chat_id,
            f"*Approval needed* — {request.summary()}\n"
            + (f"```\n{request.command}\n```\n" if request.command else "")
            + "Reply `!approve` to allow it, or `!deny` to refuse.",
            reply_to=source.thread_id or source.message_id or None,
            metadata={"source": source},
        )
        return bool(result.success)

    async def acknowledge(self, event: MessageEvent, state: str) -> None:
        """Show progress on the user's message (reaction, typing). Best effort."""
        return None
