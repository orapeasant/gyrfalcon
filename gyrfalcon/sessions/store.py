"""Sessions, messages and per-call token usage, over the shared SQL layer.

Spec: §19.7.

Why this exists beside `gyrfalcon_state.py` rather than replacing it outright:

* **CLIENT mode keeps its local SQLite file.** `db/__init__.py` already forces
  SQLite in CLIENT mode, so a single-user install gains tenancy columns it
  never has to think about, and nothing about a laptop install needs a server.
* **`session_usage` is the new capability.** The previous store kept only
  running session totals (`SET x = x + ?`), so "where did the money go in this
  session" was not merely unqueried — it was never written down. One row per
  LLM call is what makes per-call cost history, and the §19.9 replay, possible.

Three rules from `CLAUDE.md` hold this together and are easy to break:
statements live in `db/sql.py`, every read is a builder taking a `Scope`, and
schema changes go through `db/migrations.py`. Nothing here composes SQL.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

from gyrfalcon.db import open_database, resolve_target, sql
from gyrfalcon.db.migrations import ensure_schema
from gyrfalcon.db.scope import Scope, current_scope
from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("sessions.store")


def _new_id() -> str:
    return uuid.uuid4().hex


def _owner(scope: Scope) -> tuple[str, str]:
    """(tenant_id, user_id) a write belongs to.

    A `Scope.system()` has no tenant by design — it is the cross-tenant *read*
    escape hatch. Filing a conversation under it would have no coherent
    meaning, so this refuses rather than writing the string "None".
    """
    if scope.tenant_id is None:
        raise ValueError(
            "a session belongs to a tenant; a system scope cannot create one"
        )
    return scope.tenant_id, scope.user_id or "local"


class SessionStore:
    """Conversations and their token spend, scoped to the caller."""

    def __init__(self, db_path=None, backend: Optional[str] = None,
                 dsn: Optional[str] = None):
        self.backend, resolved, self.dsn = resolve_target(db_path, backend, dsn)
        self._db = open_database(backend=self.backend, path=resolved,
                                 dsn=self.dsn)
        self.schema_version = ensure_schema(self._db)

    @property
    def dialect(self):
        return self._db.dialect

    def close(self) -> None:
        self._db.close()

    # -- sessions -----------------------------------------------------------

    def create_session(
        self,
        session_id: Optional[str] = None,
        source: Optional[str] = None,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        agent_id: Optional[str] = None,
        parent_session_id: Optional[str] = None,
        title: Optional[str] = None,
        scope: Optional[Scope] = None,
    ) -> str:
        scope = scope or current_scope()
        tenant_id, user_id = _owner(scope)
        session_id = session_id or _new_id()
        now = time.time()
        with self._db.connect() as conn:
            conn.execute(sql.insert_session(), (
                session_id, source, agent_id, model, parent_session_id, title,
                system_prompt, now, None, now,
                0, 0, 0, 0, 0, 0.0, user_id, tenant_id,
            ))
        return session_id

    def get_session(self, session_id: str,
                    scope: Optional[Scope] = None) -> Optional[dict]:
        scope = scope or current_scope()
        statement, params = sql.get_session(scope)
        with self._db.connect() as conn:
            row = conn.fetchone(statement, (*params, session_id))
        return dict(row) if row else None

    def list_sessions(self, limit: int = 50, offset: int = 0,
                      scope: Optional[Scope] = None) -> list[dict]:
        scope = scope or current_scope()
        statement, params = sql.list_sessions(scope)
        with self._db.connect() as conn:
            rows = conn.fetchall(statement, (*params, limit, offset))
        return [dict(r) for r in rows]

    def end_session(self, session_id: str,
                    scope: Optional[Scope] = None) -> None:
        scope = scope or current_scope()
        statement, params = sql.end_session(scope)
        with self._db.connect() as conn:
            conn.execute(statement, (time.time(), *params, session_id))

    def update_title(self, session_id: str, title: str,
                     scope: Optional[Scope] = None) -> None:
        scope = scope or current_scope()
        statement, params = sql.update_session_title(scope)
        with self._db.connect() as conn:
            conn.execute(statement, (title, *params, session_id))

    def delete_session(self, session_id: str,
                       scope: Optional[Scope] = None) -> None:
        """Delete a session and everything hanging off it.

        The children are removed explicitly rather than by `ON DELETE CASCADE`:
        SQLite enforces foreign keys only when `PRAGMA foreign_keys` is on, so
        relying on the declaration would silently leave orphaned message rows —
        which is to say, leave the conversation text behind after a delete.
        """
        scope = scope or current_scope()
        get, get_params = sql.get_session(scope)
        statement, params = sql.delete_session(scope)
        with self._db.connect() as conn:
            if not conn.fetchone(get, (*get_params, session_id)):
                return                       # not visible: not ours to delete
            conn.execute("DELETE FROM session_messages WHERE session_id = ?",
                         (session_id,))
            conn.execute("DELETE FROM session_usage WHERE session_id = ?",
                         (session_id,))
            conn.execute(statement, (*params, session_id))

    def ensure_session(self, session_id: str, model: str = "",
                       source: str = "", scope: Optional[Scope] = None) -> bool:
        """Create the session row if it is not already there.

        Usage rows are recorded from the agent loop, which may be driving a
        conversation whose `sessions` row lives in the legacy SQLite store.
        Without this, per-call usage would accumulate against a session id
        that has no row here, and every later join would drop it.

        Returns True if a row was created.
        """
        if self.get_session(session_id, scope=scope) is not None:
            return False
        self.create_session(session_id=session_id, model=model, source=source,
                            scope=scope)
        return True

    # -- messages -----------------------------------------------------------

    def append_message(
        self,
        session_id: str,
        role: str,
        content: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        tool_calls: Optional[str] = None,
        tool_name: Optional[str] = None,
        reasoning: Optional[str] = None,
        scope: Optional[Scope] = None,
    ) -> str:
        scope = scope or current_scope()
        tenant_id, user_id = _owner(scope)
        message_id = _new_id()
        seq_sql, seq_params = sql.next_message_seq(scope)
        with self._db.connect() as conn:
            row = conn.fetchone(seq_sql, (*seq_params, session_id))
            seq = int(row["next"]) if row else 0
            conn.execute(sql.insert_session_message(), (
                message_id, session_id, seq, role, content, tool_call_id,
                tool_calls, tool_name, reasoning, time.time(),
                user_id, tenant_id,
            ))
        return message_id

    def get_messages(self, session_id: str,
                     scope: Optional[Scope] = None) -> list[dict]:
        scope = scope or current_scope()
        statement, params = sql.session_messages(scope)
        with self._db.connect() as conn:
            rows = conn.fetchall(statement, (*params, session_id))
        return [dict(r) for r in rows]

    # -- search -------------------------------------------------------------

    def search_messages(self, query: str, limit: int = 10,
                        scope: Optional[Scope] = None) -> list[dict]:
        """Full-text search across message content.

        Uses whichever index the backend has (FTS5, `tsvector`, or none), so
        the same call works on SQLite and PostgreSQL and degrades to `LIKE`
        rather than returning an empty list on a backend without either.
        """
        scope = scope or current_scope()
        dialect = self.dialect
        match = dialect.fulltext_match("session_messages", "content")
        statement, params = sql.search_messages(scope, match)
        term = dialect.fulltext_term(query)
        try:
            with self._db.connect() as conn:
                rows = conn.fetchall(statement, (*params, term, limit))
            return [dict(r) for r in rows]
        except Exception as err:
            # A malformed query reaching the matcher must not 500 a search box.
            logger.warning("session search failed (%s); returning nothing", err)
            return []

    def search_sessions(self, query: str, limit: int = 10,
                        scope: Optional[Scope] = None) -> list[dict]:
        scope = scope or current_scope()
        statement, params = sql.search_sessions_by_title(scope)
        with self._db.connect() as conn:
            rows = conn.fetchall(statement, (*params, f"%{query}%", limit))
        return [dict(r) for r in rows]

    # -- usage --------------------------------------------------------------

    def record_usage(
        self,
        session_id: str,
        usage: Any,
        model: str = "",
        provider: str = "",
        cost_usd: float = 0.0,
        cache_ttl: str = "5m",
        catalog_version: str = "",
        scope: Optional[Scope] = None,
    ) -> str:
        """Record one LLM call and fold it into the session's totals.

        `usage` is a `gyrfalcon.tokenomics.usage.Usage` — already normalized,
        so the three input figures do not overlap and can be summed across
        providers (§19.6).
        """
        scope = scope or current_scope()
        tenant_id, user_id = _owner(scope)
        usage_id = _new_id()
        now = time.time()

        seq_sql, seq_params = sql.next_usage_seq(scope)
        add_sql, add_params = sql.add_session_tokens(scope)
        with self._db.connect() as conn:
            row = conn.fetchone(seq_sql, (*seq_params, session_id))
            seq = int(row["next"]) if row else 0
            conn.execute(sql.insert_session_usage(), (
                usage_id, session_id, seq, now, provider, model,
                usage.uncached_input_tokens, usage.cache_read_tokens,
                usage.cache_write_tokens, cache_ttl, usage.output_tokens,
                usage.reasoning_tokens, cost_usd, catalog_version,
                user_id, tenant_id,
            ))
            conn.execute(add_sql, (
                usage.uncached_input_tokens, usage.cache_read_tokens,
                usage.cache_write_tokens, usage.output_tokens,
                usage.reasoning_tokens, cost_usd, now, *add_params, session_id,
            ))
        return usage_id

    def get_usage(self, session_id: str,
                  scope: Optional[Scope] = None) -> list[dict]:
        """Every recorded call for one session, in order."""
        scope = scope or current_scope()
        statement, params = sql.session_usage_rows(scope)
        with self._db.connect() as conn:
            rows = conn.fetchall(statement, (*params, session_id))
        return [dict(r) for r in rows]

    def usage_by_model(self, days: int = 30,
                       scope: Optional[Scope] = None) -> list[dict]:
        scope = scope or current_scope()
        statement, params = sql.usage_by_model(scope)
        since = time.time() - days * 86400
        with self._db.connect() as conn:
            rows = conn.fetchall(statement, (*params, since))
        return [dict(r) for r in rows]


_store: Optional[SessionStore] = None


def get_session_store() -> SessionStore:
    """The process-wide store. Cheap to call; the database is opened once."""
    global _store
    if _store is None:
        _store = SessionStore()
    return _store
