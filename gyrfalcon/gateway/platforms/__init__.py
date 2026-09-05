"""Platform adapter abstract base class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class SendResult:
    """Result of sending a message."""
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None


@dataclass
class SessionSource:
    """Identifies the source of a gateway session."""
    platform: str
    chat_id: str
    chat_name: str = ""
    user_id: str = ""
    user_name: str = ""
    thread_id: str = ""
    chat_type: str = ""
    guild_id: str = ""
    message_id: str = ""
    is_bot: bool = False


class BasePlatformAdapter(ABC):
    """Abstract base class for platform adapters."""

    def __init__(self, config: dict):
        self.config = config
        self._message_callback = None

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

    def on_message(self, callback) -> None:
        """Register message callback."""
        self._message_callback = callback

    async def edit_message(
        self, chat_id: str, message_id: str, content: str
    ) -> bool:
        """Edit an existing message (for streaming updates)."""
        return False

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Platform identifier."""
        ...
