"""Sessions, messages and per-call token usage, over the shared SQL layer.

Spec: §19.7.

Session history, messages, and per-call token usage share PostgreSQL storage.
One usage row per LLM call supports cost history and replay.

Three rules from `CLAUDE.md` hold this together and are easy to break:
statements live in `db/sql.py`, every read is a builder taking a `Scope`, and
schema changes go through `db/migrations.py`. Nothing here composes SQL.
"""

from __future__ import annotations

import time
import uuid
import json
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
        *,
        flow_run_status_id: Optional[str] = None,
        flow_node_status_id: Optional[str] = None,
    ) -> str:
        if flow_node_status_id and not flow_run_status_id:
            raise ValueError("a flow node visit requires its flow run status ID")
        scope = scope or current_scope()
        tenant_id, user_id = _owner(scope)
        session_id = session_id or _new_id()
        now = time.time()
        with self._db.connect() as conn:
            conn.execute(sql.insert_session(), (
                session_id, source, agent_id, model, parent_session_id, title,
                system_prompt, now, None, "created", None, now,
                0, 0, 0, 0, 0, 0.0, user_id, tenant_id, user_id,
                flow_run_status_id, flow_node_status_id,
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

        The children are removed explicitly so every relationship is clear
        and the operation is atomic.
        """
        scope = scope or current_scope()
        get, get_params = sql.get_session(scope)
        statement, params = sql.delete_session(scope)
        with self._db.connect() as conn:
            if not conn.fetchone(get, (*get_params, session_id)):
                return                       # not visible: not ours to delete
            conn.execute(sql.DELETE_SESSION_MESSAGES, (session_id,))
            conn.execute(sql.DELETE_SESSION_USAGE, (session_id,))
            conn.execute(sql.DELETE_SESSION_ROUTES, (session_id,))
            conn.execute(sql.DELETE_SESSION_CONTEXTS, (session_id,))
            conn.execute(sql.DELETE_SESSION_PROMPT_SNAPSHOTS, (session_id,))
            conn.execute(sql.DELETE_SESSION_PARTICIPANTS, (session_id,))
            conn.execute(statement, (*params, session_id))

    def ensure_session(self, session_id: str, model: str = "",
                       source: str = "", scope: Optional[Scope] = None) -> bool:
        """Create the session row if it is not already there.

        Usage rows can be recorded when the caller has an existing
        conversation id that has not yet been persisted in this database.

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
        *,
        flow_node_status_id: Optional[str] = None,
        message_kind: Optional[str] = None,
        channel: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> str:
        scope = scope or current_scope()
        tenant_id, user_id = _owner(scope)
        message_id = _new_id()
        seq_sql, seq_params = sql.next_message_seq(scope)
        with self._db.connect() as conn:
            session_sql, session_params = sql.get_session(scope)
            session = conn.fetchone(session_sql, (*session_params, session_id))
            if session is None:
                raise KeyError(f"Session {session_id!r} is unavailable")
            owner_visit = session["flow_node_status_id"]
            if flow_node_status_id is not None and flow_node_status_id != owner_visit:
                raise ValueError("Message node visit does not match its session")
            row = conn.fetchone(seq_sql, (*seq_params, session_id))
            seq = int(row["next"]) if row else 0
            conn.execute(sql.insert_session_message(), (
                message_id, session_id, seq, role, content, tool_call_id,
                tool_calls, tool_name, reasoning, time.time(),
                user_id, tenant_id, owner_visit,
                message_kind or ("agent_turn" if owner_visit else "chat"),
                channel or "chat", direction,
            ))
            touch, touch_params = sql.touch_session(scope)
            conn.execute(touch, (time.time(), *touch_params, session_id))
        return message_id

    def get_messages(self, session_id: str,
                     scope: Optional[Scope] = None) -> list[dict]:
        scope = scope or current_scope()
        statement, params = sql.session_messages(scope)
        with self._db.connect() as conn:
            rows = conn.fetchall(statement, (*params, session_id))
        return [dict(r) for r in rows]

    def append_flow_message(
        self, session_id: str, visit_id: str, *, content: str,
        message_kind: str, channel: str, direction: str,
        subject: str | None = None, sender: str | None = None,
        recipients: dict | list | None = None, body_html: str | None = None,
        external_message_id: str | None = None, in_reply_to: str | None = None,
        headers: dict | None = None, attachment_refs: list | None = None,
        scope: Optional[Scope] = None,
    ) -> str:
        """Write a flow Notification or response with email-capable metadata."""
        scope = scope or current_scope()
        tenant_id, user_id = _owner(scope)
        message_id = _new_id()
        session_sql, session_params = sql.get_session(scope)
        seq_sql, seq_params = sql.next_message_seq(scope)
        with self._db.connect() as conn:
            session = conn.fetchone(session_sql, (*session_params, session_id))
            if session is None or session["flow_node_status_id"] != visit_id:
                raise ValueError("Message must belong to its flow node session")
            row = conn.fetchone(seq_sql, (*seq_params, session_id))
            seq = int(row["next"]) if row else 0
            conn.execute(
                sql.insert_flow_session_message(),
                (message_id, session_id, seq, "notification", content, time.time(),
                 user_id, tenant_id, visit_id, message_kind, channel, direction,
                 subject, sender, json.dumps(recipients) if recipients is not None else None,
                 body_html, external_message_id, in_reply_to,
                 json.dumps(headers) if headers is not None else None,
                 json.dumps(attachment_refs) if attachment_refs is not None else None),
            )
            touch, touch_params = sql.touch_session(scope)
            conn.execute(touch, (time.time(), *touch_params, session_id))
        return message_id

    # -- search -------------------------------------------------------------

    def search_messages(self, query: str, limit: int = 10,
                        scope: Optional[Scope] = None) -> list[dict]:
        """Full-text search across message content.

        Uses PostgreSQL full text search over message content.
        """
        scope = scope or current_scope()
        match = "to_tsvector('simple', coalesce(ai_session_messages.content, '')) @@ plainto_tsquery('simple', ?)"
        statement, params = sql.search_messages(scope, match)
        try:
            with self._db.connect() as conn:
                rows = conn.fetchall(statement, (*params, query, limit))
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

    def tokenomics_report(
        self,
        start_at: float,
        end_at: float,
        grain: str = "day",
        dimension: str = "none",
        pivot: str = "none",
        scope: Optional[Scope] = None,
    ) -> list[dict]:
        """Read scoped, aggregated per-call usage for the Tokenomics report."""
        scope = scope or current_scope()
        period = sql.date_bucket(grain, "u.created_at")
        statement, params = sql.tokenomics_report(scope, period, dimension, pivot)
        with self._db.connect() as conn:
            rows = conn.fetchall(statement, (*params, start_at, end_at))
        return [dict(row) for row in rows]


_store: Optional[SessionStore] = None


def get_session_store() -> SessionStore:
    """The process-wide store. Cheap to call; the database is opened once."""
    global _store
    if _store is None:
        _store = SessionStore()
    return _store
