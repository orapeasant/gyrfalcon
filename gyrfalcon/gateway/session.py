"""Gateway session management — routing, lifecycle, credential pools."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from gyrfalcon.gyrfalcon_state import SessionDB
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("session")



@dataclass
class GatewaySession:
    """A gateway-managed agent session bound to a platform conversation."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    platform: str = ""
    platform_user_id: str = ""
    platform_channel_id: str = ""
    model: str = ""
    toolset: str = "default"
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    message_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def age_hours(self) -> float:
        return (time.time() - self.created_at) / 3600

    @property
    def idle_minutes(self) -> float:
        return (time.time() - self.last_active) / 60

    def touch(self) -> None:
        """Update last_active timestamp."""
        logger.debug("Beginning of touch")
        self.last_active = time.time()
        self.message_count += 1


class SessionRouter:
    """Routes incoming platform messages to the correct gateway session."""

    def __init__(self, session_db: SessionDB, ttl_hours: int = 24) -> None:
        self._db = session_db
        self._ttl_hours = ttl_hours
        self._active: dict[str, GatewaySession] = {}
        # platform_key -> session_id mapping
        self._routing_table: dict[str, str] = {}

    def _platform_key(self, platform: str, user_id: str, channel_id: str) -> str:
        """Generate a unique key for a platform conversation."""
        logger.debug("Beginning of _platform_key")
        return f"{platform}:{user_id}:{channel_id}"

    def get_or_create_session(
        self,
        platform: str,
        user_id: str,
        channel_id: str,
        model: str = "",
        toolset: str = "default",
    ) -> GatewaySession:
        """Find existing session or create a new one for this conversation."""
        logger.debug("Beginning of get_or_create_session")
        key = self._platform_key(platform, user_id, channel_id)

        # Check for existing active session
        if key in self._routing_table:
            session_id = self._routing_table[key]
            session = self._active.get(session_id)
            if session and session.age_hours < self._ttl_hours:
                session.touch()
                return session
            # Expired — remove
            self._routing_table.pop(key, None)
            self._active.pop(session_id, None)

        # Create new session
        session = GatewaySession(
            platform=platform,
            platform_user_id=user_id,
            platform_channel_id=channel_id,
            model=model,
            toolset=toolset,
        )
        self._active[session.id] = session
        self._routing_table[key] = session.id

        # Persist in SessionDB
        self._db.create_session(session.id, title=f"{platform}:{channel_id[:8]}")

        return session

    def get_session(self, session_id: str) -> Optional[GatewaySession]:
        """Get a session by ID."""
        logger.debug("Beginning of get_session")
        return self._active.get(session_id)

    def end_session(self, session_id: str) -> None:
        """End and remove a session."""
        logger.debug("Beginning of end_session")
        session = self._active.pop(session_id, None)
        if session:
            key = self._platform_key(session.platform, session.platform_user_id, session.platform_channel_id)
            self._routing_table.pop(key, None)

    def cleanup_expired(self) -> int:
        """Remove expired sessions. Returns count of removed sessions."""
        logger.debug("Beginning of cleanup_expired")
        now = time.time()
        expired = [
            sid for sid, s in self._active.items()
            if (now - s.last_active) / 3600 > self._ttl_hours
        ]
        for sid in expired:
            self.end_session(sid)
        return len(expired)

    @property
    def active_count(self) -> int:
        return len(self._active)

    def list_active(self) -> list[GatewaySession]:
        """List all active sessions."""
        logger.debug("Beginning of list_active")
        return list(self._active.values())


@dataclass
class CredentialPool:
    """Pool of API credentials for rotation (avoid rate limits)."""
    credentials: list[dict[str, str]] = field(default_factory=list)
    _index: int = 0
    _usage: dict[int, int] = field(default_factory=dict)

    def add(self, credential: dict[str, str]) -> None:
        """Add a credential to the pool."""
        logger.debug("Beginning of add")
        self.credentials.append(credential)

    def next(self) -> Optional[dict[str, str]]:
        """Get the next credential in rotation."""
        logger.debug("Beginning of next")
        if not self.credentials:
            return None
        cred = self.credentials[self._index % len(self.credentials)]
        self._usage[self._index % len(self.credentials)] = self._usage.get(
            self._index % len(self.credentials), 0
        ) + 1
        self._index += 1
        return cred

    @property
    def size(self) -> int:
        return len(self.credentials)
