"""The database abstraction — three small ABCs and nothing else.

Spec: §15.5.

Deliberately *not* an ORM (§15.3). There are no row objects, no identity map,
no lazy loading, no query builder. A `Connection` executes SQL text with bound
parameters and hands back mappings; a `Dialect` answers the handful of
questions where SQLite and PostgreSQL genuinely disagree (§15.4). Everything
else — what a transition means, when a run is final — stays in the stores.

The absence worth noticing: there is no `placeholder()` and no `rewrite()`.
Setting `pg8000.dbapi.paramstyle = "qmark"` makes `?` native on both backends
(§15.3a), so a single SQL string is executable as-is against either. That is
what makes `sql.py` viable as one shared catalog instead of two.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ContextManager, Iterable, Mapping, Optional, Sequence


class Dialect(ABC):
    """Backend-specific SQL, isolated to the few places it actually differs."""

    #: "sqlite" | "postgres"
    name: str = ""

    #: Whether `SELECT ... FOR UPDATE SKIP LOCKED` is available. False on
    #: SQLite, whose single write lock makes the clause both unavailable and
    #: unnecessary (§15.6).
    supports_skip_locked: bool = False

    @abstractmethod
    def insert_ignore(self, table: str, columns: Sequence[str], conflict: str) -> str:
        """An INSERT that silently does nothing when `conflict` already exists.

        SQLite spells this `INSERT OR IGNORE`; PostgreSQL spells it
        `ON CONFLICT (...) DO NOTHING`. Same intent, incompatible syntax —
        which is exactly why it lives behind a method (§15.4).
        """

    @abstractmethod
    def type_map(self) -> dict[str, str]:
        """Neutral column type -> this backend's spelling.

        Keys are the vocabulary `schema.py` is written in: TEXT, INTEGER,
        REAL, BOOL. Notably REAL is 4-byte on PostgreSQL, so it must map to
        DOUBLE PRECISION there — every timestamp in this schema is a float
        epoch, and 4-byte floats lose second-level resolution on 2026 epochs.
        """

    @abstractmethod
    def on_connect(self, raw_conn: Any) -> None:
        """Per-connection setup (SQLite PRAGMAs; statement timeout on PG)."""

    def row_factory(self, cursor: Any, row: Any) -> Mapping[str, Any]:
        """Adapt a driver row into a mapping keyed by column name.

        SQLite does this natively via `sqlite3.Row`; pg8000 returns plain
        lists, so the column names come off `cursor.description`.
        """
        raise NotImplementedError

    @abstractmethod
    def column_names(self, conn: "Connection", table: str) -> list[str]:
        """Columns of `table`, or [] if it does not exist.

        Migrations need to ask what shape they are looking at, and the two
        backends disagree completely on how to ask (`PRAGMA table_info` vs
        `information_schema`).
        """

    @abstractmethod
    def migration_lock(self, db: "Database") -> ContextManager[None]:
        """Serialize schema migration across *processes*.

        Held around the whole migration, not inside it, because the thing that
        must be serialized is the read-modify-write as a unit: check the
        version, apply, record it. Two workers booting together will otherwise
        both read version N, both apply migration N+1, and the second will
        either collide on the version row or — for a migration that rebuilds a
        table — race on the DROP/RENAME.

        This is §15.6's read-modify-write hazard showing up in the migrator
        itself. SQLite's write lock does *not* cover it: the version read is a
        SELECT and takes no lock at all.
        """


class Connection(ABC):
    """A unit of work. Commits on clean exit, rolls back on exception.

    Re-entrant: the stores nest calls (`record_transition` emits an event
    while already holding a connection), so only the outermost exit commits.
    """

    @abstractmethod
    def __enter__(self) -> "Connection": ...

    @abstractmethod
    def __exit__(self, exc_type, exc, tb) -> None: ...

    @abstractmethod
    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any: ...

    @abstractmethod
    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[Mapping[str, Any]]: ...

    @abstractmethod
    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[Mapping[str, Any]]: ...

    @abstractmethod
    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None: ...

    @abstractmethod
    def executescript(self, statements: Iterable[str]) -> None:
        """Run DDL. Separate from `execute` because PostgreSQL will not accept
        multiple statements in one parameterized call the way SQLite's
        `executescript` does."""


class Database(ABC):
    """A configured backend. `connect()` yields a `Connection` context manager."""

    @property
    @abstractmethod
    def dialect(self) -> Dialect: ...

    @abstractmethod
    def connect(self) -> Connection: ...

    @abstractmethod
    def close(self) -> None: ...
