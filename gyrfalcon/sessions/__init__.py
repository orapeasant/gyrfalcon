"""Tenant-scoped session persistence over the shared SQL layer.

Spec: `docs/spec/gyrfalcon/19-tokenomics.md` §19.7.
"""

from gyrfalcon.sessions.store import SessionStore, get_session_store

__all__ = ["SessionStore", "get_session_store"]
