"""Browser sessions for the dashboard.

Spec: §17.11 step 8.

Server-side rather than a signed cookie: the cookie carries only an opaque id,
so revoking a session is deleting a row rather than waiting out an expiry, and
nothing about the user travels through the browser.

**Process-local, deliberately, and a limitation to know about.** Sessions live
in memory, so two dashboard processes behind a load balancer will not share
them and a restart logs everyone out. That is acceptable for the single-box
dashboard this serves today; a shared deployment wants these rows in the
database next to `auth_api_keys`, which is a small change to this module and
nothing else.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

COOKIE_NAME = "gyrfalcon_session"
DEFAULT_TTL = 12 * 3600.0


@dataclass
class BrowserSession:
    session_id: str
    user_id: str
    org_id: str
    created_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    ttl: float = DEFAULT_TTL

    @property
    def expired(self) -> bool:
        return (time.time() - self.last_seen_at) > self.ttl


class SessionStore:
    def __init__(self, ttl: float = DEFAULT_TTL):
        self._sessions: dict[str, BrowserSession] = {}
        self._pending: dict[str, object] = {}
        self._lock = threading.Lock()
        self.ttl = ttl

    # -- login attempts (state between redirect and callback) ---------------
    def remember_attempt(self, attempt) -> None:
        with self._lock:
            self._prune_attempts()
            self._pending[attempt.state] = attempt

    def take_attempt(self, state: str):
        """Consume an attempt. Single-use: a `state` that could be replayed
        would defeat the CSRF protection it exists to provide."""
        with self._lock:
            self._prune_attempts()
            return self._pending.pop(state, None)

    def _prune_attempts(self) -> None:
        for key in [s for s, a in self._pending.items() if a.expired()]:
            self._pending.pop(key, None)

    # -- sessions ------------------------------------------------------------
    def create(self, user_id: str, org_id: str) -> BrowserSession:
        session = BrowserSession(secrets.token_urlsafe(32), user_id, org_id,
                                 ttl=self.ttl)
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    def get(self, session_id: Optional[str]) -> Optional[BrowserSession]:
        if not session_id:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if session.expired:
                self._sessions.pop(session_id, None)
                return None
            session.last_seen_at = time.time()
            return session

    def destroy(self, session_id: Optional[str]) -> None:
        if not session_id:
            return
        with self._lock:
            self._sessions.pop(session_id, None)

    def destroy_user(self, user_id: str) -> int:
        """Sign a person out everywhere — for deactivation or a lost laptop."""
        with self._lock:
            gone = [s for s, v in self._sessions.items() if v.user_id == user_id]
            for s in gone:
                self._sessions.pop(s, None)
        return len(gone)


_STORE: Optional[SessionStore] = None
_STORE_LOCK = threading.Lock()


def get_session_store() -> SessionStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = SessionStore()
        return _STORE
