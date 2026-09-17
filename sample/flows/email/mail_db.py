"""Mail database — a second, standalone SQLite database for the email intake
pipeline (poll -> store -> classify -> dispatch).

Deliberately **not** the flow engine's `flow.db`: that database's schema is
owned by `gyrfalcon.flow.db.migrations` and is versioned as one unit with
`flow_runs`/`flow_events`/`flow_deployments`. Mail has its own lifecycle
(threading, classification, routing) that has nothing to do with run
orchestration, so it gets its own file — `<GYRFALCON_HOME>/email/mail.db` —
and its own tiny forward-only migration list, following the same pattern
(`CREATE TABLE IF NOT EXISTS` is not enough once the shape can change under a
live database — see `flow/db/migrations.py` for the reasoning this copies).

Three tables:

* `emails` — one durable row per message, with thread linkage resolved at
  ingest time so a reply/forward is filed under its parent thread instead of
  becoming a new, disconnected conversation.
* `email_classifications` — zero or more tags per email (`email_id`, `label`),
  because one email may legitimately be "invoice" AND "urgent".
* `classification_flow_map` — one row per label, naming the flow to invoke
  when that label is assigned. A label with no row (or a disabled row) is
  simply not routed — that is a config gap, not an error, and is logged as one.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home

SCHEMA_VERSION = 1


def default_db_path() -> Path:
    d = get_gyrfalcon_home() / "email"
    d.mkdir(parents=True, exist_ok=True)
    return d / "mail.db"


_DDL = """
CREATE TABLE IF NOT EXISTS emails (
    id              TEXT PRIMARY KEY,      -- Message-ID (or synthesized fallback)
    account         TEXT NOT NULL,
    folder          TEXT NOT NULL,
    uid             INTEGER,
    thread_id       TEXT NOT NULL,         -- id of the root message in this thread
    in_reply_to     TEXT,                  -- raw In-Reply-To header
    references_ids  TEXT,                  -- JSON list, the raw References header split
    from_addr       TEXT,
    to_addr         TEXT,
    subject         TEXT,
    date_header     TEXT,
    body_text       TEXT,
    raw_headers     TEXT,                  -- JSON dict of the headers we kept
    direction       TEXT NOT NULL DEFAULT 'inbound',  -- inbound | reply | forward
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_emails_thread ON emails(thread_id);
CREATE INDEX IF NOT EXISTS idx_emails_created ON emails(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_emails_account ON emails(account, folder);

CREATE TABLE IF NOT EXISTS email_classifications (
    email_id     TEXT NOT NULL,
    label        TEXT NOT NULL,
    confidence   REAL,
    source       TEXT NOT NULL DEFAULT 'rule',   -- 'rule' | 'llm' | 'manual'
    created_at   REAL NOT NULL,
    PRIMARY KEY (email_id, label),
    FOREIGN KEY (email_id) REFERENCES emails(id)
);
CREATE INDEX IF NOT EXISTS idx_classif_label ON email_classifications(label);

CREATE TABLE IF NOT EXISTS classification_flow_map (
    label        TEXT PRIMARY KEY,
    flow_name    TEXT NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    parameters   TEXT NOT NULL DEFAULT '{}',   -- JSON, merged into the flow's call
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS email_dispatches (
    id           TEXT PRIMARY KEY,
    email_id     TEXT NOT NULL,
    label        TEXT NOT NULL,
    flow_name    TEXT NOT NULL,
    run_id       TEXT,
    status       TEXT NOT NULL,   -- 'dispatched' | 'skipped' | 'error'
    detail       TEXT,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dispatch_email ON email_dispatches(email_id);

CREATE TABLE IF NOT EXISTS mail_schema_version (
    version      INTEGER PRIMARY KEY,
    applied_at   REAL NOT NULL
);
"""


class MailDB:
    """Thread-safe wrapper around one SQLite connection, mirroring the
    concurrency model `flow/db/sqlite.py` already uses: one connection,
    `check_same_thread=False`, guarded by a lock — because the poller, the
    classifier, and the dispatcher all run on worker threads."""

    def __init__(self, path: Optional[Path | str] = None):
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA busy_timeout=30000")
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(_DDL)
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM mail_schema_version"
            ).fetchone()
            if int(row["v"]) < SCHEMA_VERSION:
                self._conn.execute(
                    "INSERT INTO mail_schema_version (version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, time.time()),
                )

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock, self._conn:
            yield self._conn.cursor()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_singleton_lock = threading.Lock()
_singleton: Optional[MailDB] = None


def get_mail_db() -> MailDB:
    """Process-wide singleton, same idiom as `flow.events.get_event_log()`."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = MailDB()
    return _singleton


def new_id() -> str:
    return uuid.uuid4().hex
