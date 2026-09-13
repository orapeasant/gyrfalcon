"""Session persistence — SQLite with WAL mode and FTS5 full-text search."""

import json
import sqlite3
import time
import random
import uuid
from pathlib import Path
from typing import Any, Optional

from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home
from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("state")

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT,
    user_id TEXT,
    agent_id TEXT,
    model TEXT,
    parent_session_id TEXT,
    title TEXT,
    system_prompt TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    last_active REAL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    cost REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    reasoning TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);

CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content, session_id UNINDEXED, role UNINDEXED,
    content='messages', content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content, session_id, role)
    VALUES (new.id, new.content, new.session_id, new.role);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, session_id, role)
    VALUES ('delete', old.id, old.content, old.session_id, old.role);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, session_id, role)
    VALUES ('delete', old.id, old.content, old.session_id, old.role);
    INSERT INTO messages_fts(rowid, content, session_id, role)
    VALUES (new.id, new.content, new.session_id, new.role);
END;
"""

MAX_RETRIES = 15


def _retry_execute(conn: sqlite3.Connection, sql: str, params: tuple = (), many: bool = False):
    """Application-level retry with random jitter."""
    logger.debug("Beginning of _retry_execute")
    for attempt in range(MAX_RETRIES):
        try:
            if many:
                conn.executemany(sql, params)
            else:
                return conn.execute(sql, params)
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < MAX_RETRIES - 1:
                time.sleep(random.uniform(0.02, 0.15))
            else:
                raise
    return None


class SessionDB:
    """SQLite session store with FTS5 full-text search."""

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            db_path = get_gyrfalcon_home() / "sessions.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._write_count = 0
        self._init_db()

    def _init_db(self) -> None:
        # check_same_thread=False allows connection to be used across threads
        # This is safe with WAL mode and proper transaction handling
        logger.debug("Beginning of _init_db")
        self._conn = sqlite3.connect(str(self.db_path), timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA_SQL)
        try:
            self._conn.executescript(_FTS_SQL)
        except sqlite3.OperationalError:
            pass  # FTS5 not available
        try:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN agent_id TEXT")
        except sqlite3.OperationalError:
            pass  # already has the column
        # Sessions predate identity, so every existing row has user_id NULL —
        # the column was written as None and never read (§17.1). Claim them
        # for the single-user `local` principal so the same filter applies to
        # old and new rows alike, instead of NULL meaning "visible to nobody".
        self._conn.execute(
            "UPDATE sessions SET user_id = 'local' WHERE user_id IS NULL"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_user "
            "ON sessions(user_id, last_active DESC)"
        )
        self._conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._init_db()
        return self._conn

    def _maybe_checkpoint(self) -> None:
        logger.debug("Beginning of _maybe_checkpoint")
        self._write_count += 1
        if self._write_count % 50 == 0:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.OperationalError:
                pass

    @staticmethod
    def _owner(user_id: str | None = None) -> str:
        """Whose sessions we are talking about (§17.11 step 4).

        Defaults to the current principal, which is `local` on a single-user
        install — so the filter is always present and always correct rather
        than being applied only when someone remembers to pass an id.
        """
        if user_id is not None:
            return user_id
        from gyrfalcon.identity import require_principal

        return require_principal().user_id

    @staticmethod
    def _visible(user_id: str | None) -> tuple[str, tuple]:
        """(clause, params) restricting a query to one user's sessions.

        An operator sees the whole install; §17.6 keeps "may I see it" and
        "may I act on it" as separate questions, and listing sessions is the
        former.
        """
        from gyrfalcon.identity import require_principal

        if user_id is None:
            who = require_principal()
            if who.is_operator:
                return "", ()
            return "user_id = ?", (who.user_id,)
        return "user_id = ?", (user_id,)

    def create_session(
        self,
        session_id: str | None = None,
        source: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        user_id: str | None = None,
        parent_session_id: str | None = None,
        title: str | None = None,
        agent_id: str | None = None,
    ) -> str:
        logger.debug("Beginning of create_session")
        if session_id is None:
            session_id = str(uuid.uuid4())
        now = time.time()
        _retry_execute(
            self.conn,
            """INSERT INTO sessions (id, source, user_id, agent_id, model, parent_session_id, title,
               system_prompt, started_at, last_active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, source, self._owner(user_id), agent_id, model, parent_session_id,
             title, system_prompt, now, now),
        )
        self.conn.commit()
        self._maybe_checkpoint()
        logger.debug(f"Created session {session_id}")
        return session_id

    def end_session(self, session_id: str) -> None:
        logger.debug("Beginning of end_session")
        now = time.time()
        _retry_execute(
            self.conn,
            "UPDATE sessions SET ended_at = ?, last_active = ? WHERE id = ?",
            (now, now, session_id),
        )
        self.conn.commit()
        self._maybe_checkpoint()

    def delete_session(self, session_id: str, user_id: str | None = None) -> None:
        """Deleting is acting, not seeing: refuse anything not visible."""
        if self.get_session(session_id, user_id) is None:
            return
        self._delete_session_unchecked(session_id)

    def _delete_session_unchecked(self, session_id: str) -> None:
        logger.debug("Beginning of delete_session")
        _retry_execute(self.conn, "DELETE FROM messages WHERE session_id = ?", (session_id,))
        _retry_execute(self.conn, "DELETE FROM sessions WHERE id = ?", (session_id,))
        self.conn.commit()
        self._maybe_checkpoint()

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str | None = None,
        tool_calls: list | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        reasoning: str | None = None,
    ) -> None:
        logger.debug("Beginning of append_message")
        now = time.time()
        tool_calls_json = json.dumps(tool_calls) if tool_calls else None
        _retry_execute(
            self.conn,
            """INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id,
               tool_name, reasoning, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, role, content, tool_calls_json, tool_call_id, tool_name, reasoning, now),
        )
        _retry_execute(
            self.conn,
            "UPDATE sessions SET last_active = ? WHERE id = ?",
            (now, session_id),
        )
        self.conn.commit()
        self._maybe_checkpoint()

    def get_messages(self, session_id: str, user_id: str | None = None) -> list[dict]:
        """Messages of a session the caller may see.

        Visibility follows the session, checked here rather than left to each
        caller: this returns the full text of someone's conversation, and it
        was the one read on this class with no ownership filter at all — so
        anyone who knew a session id could read another tenant's messages,
        including through `GET /api/sessions/{id}/messages`.
        """
        logger.debug("Beginning of get_messages")
        if self.get_session(session_id, user_id) is None:
            return []
        cursor = _retry_execute(
            self.conn,
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        )
        rows = cursor.fetchall() if cursor else []
        return [dict(row) for row in rows]

    def get_messages_as_conversation(self, session_id: str,
                                     user_id: str | None = None) -> list[dict]:
        """Get messages in OpenAI conversation format."""
        logger.debug("Beginning of get_messages_as_conversation")
        messages = self.get_messages(session_id, user_id)
        result = []
        for msg in messages:
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

    def get_session(self, session_id: str, user_id: str | None = None) -> Optional[dict]:
        logger.debug("Beginning of get_session")
        clause, params = self._visible(user_id)
        where = f"WHERE id = ? AND {clause}" if clause else "WHERE id = ?"
        cursor = _retry_execute(
            self.conn, f"SELECT * FROM sessions {where}", (session_id, *params)
        )
        row = cursor.fetchone() if cursor else None
        return dict(row) if row else None

    def list_sessions(
        self, limit: int = 50, offset: int = 0, source: str | None = None,
        user_id: str | None = None,
    ) -> list[dict]:
        logger.debug("Beginning of list_sessions")
        clause, params = self._visible(user_id)
        clauses = [c for c in (clause, "source = ?" if source else "") if c]
        values = [*params] + ([source] if source else [])
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        cursor = _retry_execute(
            self.conn,
            f"SELECT * FROM sessions {where} ORDER BY last_active DESC LIMIT ? OFFSET ?",
            (*values, limit, offset),
        )
        rows = cursor.fetchall() if cursor else []
        return [dict(row) for row in rows]

    def search_messages(self, query: str, limit: int = 10,
                        user_id: str | None = None) -> list[dict]:
        """Full-text search across messages.

        Scoped through the joined session: message text is the most sensitive
        thing in this database, so an unfiltered FTS query would be the widest
        possible leak.
        """
        logger.debug("Beginning of search_messages")
        clause, params = self._visible(user_id)
        extra = f" AND s.{clause}" if clause else ""
        try:
            cursor = _retry_execute(
                self.conn,
                f"""SELECT m.*, s.title as session_title
                   FROM messages_fts fts
                   JOIN messages m ON m.id = fts.rowid
                   JOIN sessions s ON s.id = m.session_id
                   WHERE messages_fts MATCH ?{extra}
                   ORDER BY rank LIMIT ?""",
                (query, *params, limit),
            )
            rows = cursor.fetchall() if cursor else []
            return [dict(row) for row in rows]
        except sqlite3.OperationalError:
            return []

    def search_sessions(self, query: str, limit: int = 10,
                        user_id: str | None = None) -> list[dict]:
        """Search sessions by title."""
        logger.debug("Beginning of search_sessions")
        clause, params = self._visible(user_id)
        extra = f" AND {clause}" if clause else ""
        cursor = _retry_execute(
            self.conn,
            f"SELECT * FROM sessions WHERE title LIKE ?{extra} "
            f"ORDER BY last_active DESC LIMIT ?",
            (f"%{query}%", *params, limit),
        )
        rows = cursor.fetchall() if cursor else []
        return [dict(row) for row in rows]

    def update_token_counts(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        cost: float = 0.0,
    ) -> None:
        logger.debug("Beginning of update_token_counts")
        _retry_execute(
            self.conn,
            """UPDATE sessions SET
               input_tokens = input_tokens + ?,
               output_tokens = output_tokens + ?,
               cache_read_tokens = cache_read_tokens + ?,
               cache_write_tokens = cache_write_tokens + ?,
               reasoning_tokens = reasoning_tokens + ?,
               cost = cost + ?
               WHERE id = ?""",
            (input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
             reasoning_tokens, cost, session_id),
        )
        self.conn.commit()

    def update_session_title(self, session_id: str, title: str) -> None:
        logger.debug("Beginning of update_session_title")
        _retry_execute(
            self.conn,
            "UPDATE sessions SET title = ? WHERE id = ?",
            (title, session_id),
        )
        self.conn.commit()

    def get_meta(self, key: str) -> Optional[str]:
        logger.debug("Beginning of get_meta")
        cursor = _retry_execute(
            self.conn, "SELECT value FROM state_meta WHERE key = ?", (key,)
        )
        row = cursor.fetchone() if cursor else None
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        logger.debug("Beginning of set_meta")
        _retry_execute(
            self.conn,
            "INSERT OR REPLACE INTO state_meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.conn.commit()

    def get_analytics(self, days: int = 30) -> list[dict]:
        """Get daily usage analytics."""
        logger.debug("Beginning of get_analytics")
        since = time.time() - (days * 86400)
        cursor = _retry_execute(
            self.conn,
            """SELECT
                date(started_at, 'unixepoch') as day,
                COUNT(*) as sessions,
                SUM(input_tokens) as input_tokens,
                SUM(output_tokens) as output_tokens,
                SUM(cache_read_tokens) as cache_read_tokens,
                SUM(reasoning_tokens) as reasoning_tokens,
                SUM(cost) as cost
               FROM sessions
               WHERE started_at > ?
               GROUP BY day
               ORDER BY day""",
            (since,),
        )
        rows = cursor.fetchall() if cursor else []
        return [dict(row) for row in rows]

    def close(self) -> None:
        logger.debug("Beginning of close")
        if self._conn:
            self._conn.close()
            self._conn = None
