"""Conversation persistence on the shared PostgreSQL database."""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from gyrfalcon.db import sql
from gyrfalcon.db.scope import Scope, current_scope
from gyrfalcon.identity import require_principal
from gyrfalcon.sessions.store import SessionStore


class SessionDB(SessionStore):
    """The session API used by the agent and its interaction surfaces."""

    @staticmethod
    def _scope(user_id: str | None = None) -> Scope:
        base = current_scope()
        if user_id is None:
            return base
        return Scope(tenant_id=require_principal().tenant_id, user_id=user_id)

    @staticmethod
    def _session(row: dict | None) -> dict | None:
        if row is None:
            return None
        row["input_tokens"] = row["uncached_input_tokens"]
        row["cost"] = row["cost_usd"]
        return row

    def create_session(
        self, session_id: str | None = None, source: str | None = None,
        model: str | None = None, system_prompt: str | None = None,
        user_id: str | None = None, parent_session_id: str | None = None,
        title: str | None = None, agent_id: str | None = None,
    ) -> str:
        return super().create_session(session_id, source, model, system_prompt,
                                      agent_id, parent_session_id, title, self._scope(user_id))

    def get_session(self, session_id: str, user_id: str | None = None) -> Optional[dict]:
        return self._session(super().get_session(session_id, scope=self._scope(user_id)))

    def list_sessions(self, limit: int = 50, offset: int = 0,
                      source: str | None = None, user_id: str | None = None) -> list[dict]:
        scope = self._scope(user_id)
        if source:
            statement, params = sql.list_sessions_by_source(scope)
            with self._db.connect() as conn:
                records = conn.fetchall(statement, (*params, source, limit, offset))
            return [self._session(dict(row)) for row in records]
        return [self._session(row) for row in super().list_sessions(limit, offset, scope=scope)]

    def delete_session(self, session_id: str, user_id: str | None = None) -> None:
        super().delete_session(session_id, scope=self._scope(user_id))

    def append_message(
        self, session_id: str, role: str, content: str | None = None,
        tool_calls: list | None = None, tool_call_id: str | None = None,
        tool_name: str | None = None, reasoning: str | None = None,
    ) -> None:
        super().append_message(session_id, role, content, tool_call_id,
                               json.dumps(tool_calls) if tool_calls else None,
                               tool_name, reasoning, scope=self._scope())

    def get_messages(self, session_id: str, user_id: str | None = None) -> list[dict]:
        if self.get_session(session_id, user_id) is None:
            return []
        return super().get_messages(session_id, scope=self._scope(user_id))

    def get_messages_as_conversation(self, session_id: str,
                                     user_id: str | None = None) -> list[dict]:
        result = []
        for msg in self.get_messages(session_id, user_id):
            entry: dict[str, Any] = {"role": msg["role"]}
            if msg["content"]:
                entry["content"] = msg["content"]
            if msg["tool_calls"]:
                entry["tool_calls"] = json.loads(msg["tool_calls"])
            if msg["tool_call_id"]:
                entry["tool_call_id"] = msg["tool_call_id"]
            if msg["tool_name"]:
                entry["name"] = msg["tool_name"]
            result.append(entry)
        return result

    def search_messages(self, query: str, limit: int = 10,
                        user_id: str | None = None) -> list[dict]:
        return super().search_messages(query, limit, scope=self._scope(user_id))

    def search_sessions(self, query: str, limit: int = 10,
                        user_id: str | None = None) -> list[dict]:
        return [self._session(row) for row in super().search_sessions(query, limit, scope=self._scope(user_id))]

    def update_token_counts(
        self, session_id: str, input_tokens: int = 0, output_tokens: int = 0,
        cache_read_tokens: int = 0, cache_write_tokens: int = 0,
        reasoning_tokens: int = 0, cost: float = 0.0,
    ) -> None:
        statement, params = sql.add_session_tokens(self._scope())
        with self._db.connect() as conn:
            conn.execute(statement, (input_tokens, cache_read_tokens, cache_write_tokens,
                                     output_tokens, reasoning_tokens, cost, time.time(),
                                     *params, session_id))

    def update_session_title(self, session_id: str, title: str) -> None:
        super().update_title(session_id, title, scope=self._scope())

    def get_meta(self, key: str) -> Optional[str]:
        with self._db.connect() as conn:
            row = conn.fetchone(sql.GET_STATE_META, (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._db.connect() as conn:
            conn.execute(sql.SET_STATE_META, (key, value))

    def get_analytics(self, days: int = 30) -> list[dict]:
        statement, params = sql.session_analytics(self._scope(), self.dialect.date_bucket("day", "started_at"))
        with self._db.connect() as conn:
            return [dict(row) for row in conn.fetchall(statement, (*params, time.time() - days * 86400))]

    def count_sessions(self) -> int:
        statement, params = sql.count_sessions(self._scope())
        with self._db.connect() as conn:
            row = conn.fetchone(statement, params)
        return int(row["count"])

    def list_recent_messages(self, role: str, limit: int) -> list[dict]:
        statement, params = sql.recent_session_messages(self._scope())
        with self._db.connect() as conn:
            return [dict(row) for row in conn.fetchall(statement, (*params, role, limit))]
