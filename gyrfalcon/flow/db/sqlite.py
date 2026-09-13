"""SQLite backend — the default, and the one that needs no configuration.

Spec: §15.2. SQLite is not a dependency this design introduces; gyrfalcon has
persisted sessions through the stdlib `sqlite3` module since well before the
flow engine existed. It stays the default so a single-box install keeps
working with nothing installed and nothing to configure.

Concurrency model, preserved exactly from the pre-adapter stores: one
connection per `Database`, opened with `check_same_thread=False` and guarded
by an `RLock`. Task runs execute on pool threads, so the connection is touched
concurrently; the lock is what makes that safe. This is also precisely the
model §15.6 says does *not* generalize to multiple processes — the lock is
in-process, so it protects threads and nothing else.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from gyrfalcon.flow.db.base import Connection, Database, Dialect


class SqliteDialect(Dialect):
    name = "sqlite"
    supports_skip_locked = False

    def insert_ignore(self, table: str, columns: Sequence[str], conflict: str) -> str:
        cols = ", ".join(columns)
        vals = ",".join("?" * len(columns))
        return f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({vals})"

    def type_map(self) -> dict[str, str]:
        return {"TEXT": "TEXT", "INTEGER": "INTEGER", "REAL": "REAL", "BOOL": "INTEGER"}

    def on_connect(self, raw_conn: Any) -> None:
        # Wait for a contended write lock rather than failing immediately. Set
        # first, so it covers the journal_mode switch below.
        raw_conn.execute("PRAGMA busy_timeout=30000")
        try:
            # WAL lets readers proceed during a write, which matters because
            # the dashboard polls run state while flows are executing.
            raw_conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # Another process is switching the journal mode right now. It is a
            # database-level property, so whoever wins sets it for everyone —
            # failing to start over losing that race would be absurd.
            pass

    def row_factory(self, cursor: Any, row: Any) -> Mapping[str, Any]:
        return sqlite3.Row(cursor, row)

    def column_names(self, conn, table: str) -> list[str]:
        # PRAGMA takes no bound parameters; the table names it is called with
        # are module constants from schema.py, never user input.
        return [r["name"] for r in conn.fetchall(f"PRAGMA table_info({table})")]

    @contextmanager
    def migration_lock(self, db):
        """An OS file lock beside the database file.

        SQLite offers nothing usable here: its own locking is per-statement
        and the version check is a SELECT, so two processes booting together
        both see the old version and both migrate. `filelock` is already a
        dependency and works across processes on every platform gyrfalcon
        runs on.
        """
        from filelock import FileLock

        with FileLock(f"{db.path}.migrate.lock", timeout=120):
            yield


class SqliteConnection(Connection):
    """Re-entrant wrapper over the shared connection.

    Only the outermost `__exit__` commits: `record_transition` writes a state
    row and then emits an event through the same connection, and that pair
    must land as one commit rather than two.
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock
        self._depth = 0

    def __enter__(self) -> "SqliteConnection":
        self._lock.acquire()
        self._depth += 1
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._depth -= 1
            if self._depth == 0:
                if exc_type is None:
                    self._conn.commit()
                else:
                    self._conn.rollback()
        finally:
            self._lock.release()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, tuple(params))

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[Mapping[str, Any]]:
        return self._conn.execute(sql, tuple(params)).fetchone()

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[Mapping[str, Any]]:
        return self._conn.execute(sql, tuple(params)).fetchall()

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        self._conn.executemany(sql, [tuple(r) for r in rows])

    def executescript(self, statements: Iterable[str]) -> None:
        for stmt in statements:
            self._conn.execute(stmt)


class SqliteDatabase(Database):
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._dialect = SqliteDialect()
        self._lock = threading.RLock()
        self._raw = sqlite3.connect(self.path, check_same_thread=False)
        self._raw.row_factory = sqlite3.Row
        self._dialect.on_connect(self._raw)
        self._conn = SqliteConnection(self._raw, self._lock)

    @property
    def dialect(self) -> Dialect:
        return self._dialect

    def connect(self) -> SqliteConnection:
        return self._conn

    def close(self) -> None:
        with self._lock:
            self._raw.close()
