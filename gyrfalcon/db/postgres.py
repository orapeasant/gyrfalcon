"""PostgreSQL backend — the multi-writer option.

Spec: §15.3a (driver), §15.4 (what differs), §15.6 (what multi-writer changes).

Driver is pg8000: pure Python, no libpq, no build step, and — the reason it
was chosen over psycopg — it supports `paramstyle = "qmark"`, so the `?`
placeholders in `sql.py` execute unchanged on both backends. There is no
placeholder rewriting anywhere in this package, and therefore none to get
subtly wrong.

Concurrency model, and the real difference from `sqlite.py`: SQLite uses one
connection guarded by an in-process lock. That is meaningless here — the whole
point of PostgreSQL is that several *processes* write concurrently — so this
uses a pool, with one connection checked out per thread for the duration of a
transaction. Nested `connect()` calls on the same thread reuse that
connection, so a store method that writes a row and then emits an event still
commits once.

What this module does NOT fix: the three single-writer assumptions in §15.6
(`_reconcile_crashed` marking a peer's live runs as Crashed, unclaimed due
deployments, and read-modify-write races). Those are §15.11 step 6, and they
are a prerequisite for running two gyrfalcon processes against one database —
see §17.9.
"""

from __future__ import annotations

import queue
import threading
from contextlib import contextmanager
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import unquote, urlparse

from gyrfalcon.db.base import Connection, Database, Dialect

#: Arbitrary but fixed key identifying "the gyrfalcon flow schema".
MIGRATION_LOCK_KEY = 7_233_119_004


class PostgresDialect(Dialect):
    name = "postgres"
    supports_skip_locked = True
    supports_fulltext = True

    def fulltext_ddl(self, table: str, column: str) -> list[str]:
        """A GIN index over `to_tsvector`.

        No shadow table and no triggers: PostgreSQL computes the vector from
        the row itself, so there is nothing to keep in sync and nothing that
        can drift. `'simple'` rather than `'english'` deliberately — message
        text is frequently code and identifiers, which a stemmer mangles.
        """
        return [
            f"CREATE INDEX IF NOT EXISTS {table}_{column}_fts "
            f"ON {table} USING GIN (to_tsvector('simple', coalesce({column}, '')))"
        ]

    def fulltext_match(self, table: str, column: str) -> str:
        return (f"to_tsvector('simple', coalesce({table}.{column}, '')) "
                f"@@ plainto_tsquery('simple', ?)")

    def fulltext_term(self, query: str) -> str:
        """`plainto_tsquery` already treats its input as plain words."""
        return query

    def date_bucket(self, grain: str, expression: str) -> str:
        if grain not in {"day", "week", "month", "quarter"}:
            raise ValueError(f"unsupported date grain: {grain}")
        if grain == "day":
            return f"to_char(to_timestamp({expression}) AT TIME ZONE 'UTC', 'YYYY-MM-DD')"
        fmt = "IYYY-\"W\"IW" if grain == "week" else "YYYY-MM" if grain == "month" else "YYYY-\"Q\"Q"
        return (
            f"to_char(date_trunc('{grain}', to_timestamp({expression}) "
            f"AT TIME ZONE 'UTC'), '{fmt}')"
        )


    def __init__(self, statement_timeout_ms: int = 30000):
        self.statement_timeout_ms = statement_timeout_ms

    def insert_ignore(self, table: str, columns: Sequence[str], conflict: str) -> str:
        cols = ", ".join(columns)
        vals = ",".join("?" * len(columns))
        return (
            f"INSERT INTO {table} ({cols}) VALUES ({vals}) "
            f"ON CONFLICT ({conflict}) DO NOTHING"
        )

    def type_map(self) -> dict[str, str]:
        return {
            "TEXT": "TEXT",
            "INTEGER": "BIGINT",
            # Not REAL: PostgreSQL's REAL is 4-byte. Every timestamp in this
            # schema is a float epoch, and 4-byte floats cannot represent a
            # 2026 epoch to the second — they round to ~128-second buckets.
            "REAL": "DOUBLE PRECISION",
            # Not BOOLEAN: the stores write `int(paused)` and read `bool(row[...])`,
            # which round-trips through SMALLINT unchanged. Using BOOLEAN would
            # make the two backends disagree about what comes back.
            "BOOL": "SMALLINT",
        }

    def on_connect(self, raw_conn: Any) -> None:
        cur = raw_conn.cursor()
        cur.execute(f"SET statement_timeout = {int(self.statement_timeout_ms)}")
        raw_conn.commit()

    def row_factory(self, cursor: Any, row: Any) -> Mapping[str, Any]:
        return {d[0]: v for d, v in zip(cursor.description, row)}

    def column_names(self, conn, table: str) -> list[str]:
        rows = conn.fetchall(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ? "
            "ORDER BY ordinal_position",
            (table,),
        )
        return [r["column_name"] for r in rows]

    @contextmanager
    def migration_lock(self, db):
        """A session-scoped advisory lock, on its own connection.

        Deliberately not `pg_advisory_xact_lock`: that would have to be taken
        inside a transaction, and because `Connection` is re-entrant the whole
        migration would then collapse into that one transaction — losing the
        one-transaction-per-migration property. A dedicated connection keeps
        the lock outside, where it belongs.
        """
        raw = db._acquire()
        try:
            cur = raw.cursor()
            cur.execute("SELECT pg_advisory_lock(?)", (MIGRATION_LOCK_KEY,))
            raw.commit()
            try:
                yield
            finally:
                cur = raw.cursor()
                cur.execute("SELECT pg_advisory_unlock(?)", (MIGRATION_LOCK_KEY,))
                raw.commit()
        finally:
            db._release(raw)


def _dsn_to_kwargs(dsn: str) -> dict[str, Any]:
    """Parse `postgresql://user:pass@host:port/dbname` into pg8000 kwargs."""
    parsed = urlparse(dsn)
    if parsed.scheme not in ("postgres", "postgresql"):
        raise ValueError(
            f"flow.store.dsn must start with postgresql:// (got {parsed.scheme!r}://)"
        )
    database = (parsed.path or "").lstrip("/")
    if not database:
        raise ValueError(f"flow.store.dsn names no database: {dsn!r}")

    kwargs: dict[str, Any] = {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 5432,
        "database": database,
        "application_name": "gyrfalcon-flow",
    }
    if parsed.username:
        kwargs["user"] = unquote(parsed.username)
    if parsed.password:
        kwargs["password"] = unquote(parsed.password)
    return kwargs


class PostgresConnection(Connection):
    """Re-entrant, thread-scoped transaction over a pooled connection.

    The outermost `__enter__` on a thread checks a connection out of the pool
    and the outermost `__exit__` commits (or rolls back) and returns it.
    Nested uses on the same thread join that transaction rather than starting
    a second one — which is what makes `record_transition`'s state write and
    its event emission land together.
    """

    def __init__(self, db: "PostgresDatabase"):
        self._db = db
        self._local = threading.local()

    # -- per-thread state ----------------------------------------------------
    @property
    def _raw(self) -> Any:
        return getattr(self._local, "raw", None)

    def __enter__(self) -> "PostgresConnection":
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            self._local.raw = self._db._acquire()
        self._local.depth = depth + 1
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._local.depth -= 1
        if self._local.depth > 0:
            return
        raw = self._local.raw
        self._local.raw = None
        try:
            if exc_type is None:
                raw.commit()
            else:
                raw.rollback()
        finally:
            self._db._release(raw, broken=exc_type is not None)

    # -- statements ----------------------------------------------------------
    def _cursor(self, sql: str, params: Sequence[Any]) -> Any:
        raw = self._raw
        if raw is None:
            raise RuntimeError(
                "PostgresConnection used outside a `with db.connect()` block"
            )
        cur = raw.cursor()
        cur.execute(sql, tuple(params))
        return cur

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        return self._cursor(sql, params)

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[Mapping[str, Any]]:
        cur = self._cursor(sql, params)
        row = cur.fetchone()
        return self._db.dialect.row_factory(cur, row) if row is not None else None

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[Mapping[str, Any]]:
        cur = self._cursor(sql, params)
        factory = self._db.dialect.row_factory
        return [factory(cur, row) for row in cur.fetchall()]

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        raw = self._raw
        cur = raw.cursor()
        for row in rows:
            cur.execute(sql, tuple(row))

    def executescript(self, statements: Iterable[str]) -> None:
        raw = self._raw
        cur = raw.cursor()
        for stmt in statements:
            cur.execute(stmt)


class PostgresDatabase(Database):
    """Pooled PostgreSQL backend.

    The pool is bounded and lazy: `pool_max_size` connections at most, created
    on demand. A checkout that finds a dead connection (server restart, idle
    timeout, failed transaction) replaces it rather than handing back one that
    will raise on first use.
    """

    def __init__(
        self,
        dsn: str,
        pool_min_size: int = 1,
        pool_max_size: int = 10,
        statement_timeout_ms: int = 30000,
    ):
        from gyrfalcon.db import _require_pg8000

        self._driver = _require_pg8000()
        self.dsn = dsn
        self._kwargs = _dsn_to_kwargs(dsn)
        self._dialect = PostgresDialect(statement_timeout_ms)
        self._max = max(1, int(pool_max_size))
        self._pool: queue.LifoQueue = queue.LifoQueue(maxsize=self._max)
        self._created = 0
        self._lock = threading.Lock()
        self._closed = False
        self._conn = PostgresConnection(self)

        for _ in range(max(0, min(int(pool_min_size), self._max))):
            self._pool.put(self._connect_raw())

    # -- pool ----------------------------------------------------------------
    def _connect_raw(self) -> Any:
        raw = self._driver.connect(**self._kwargs)
        self._dialect.on_connect(raw)
        with self._lock:
            self._created += 1
        return raw

    def _healthy(self, raw: Any) -> bool:
        """Reset a pooled connection's transaction state, proving it is alive."""
        try:
            raw.rollback()
            return True
        except Exception:
            try:
                raw.close()
            except Exception:
                pass
            with self._lock:
                self._created -= 1
            return False

    def _acquire(self) -> Any:
        if self._closed:
            raise RuntimeError("PostgresDatabase is closed")
        while True:
            try:
                raw = self._pool.get_nowait()
            except queue.Empty:
                with self._lock:
                    may_create = self._created < self._max
                if may_create:
                    return self._connect_raw()
                # At capacity: wait for someone to give one back.
                raw = self._pool.get()
            if self._healthy(raw):
                return raw

    def _release(self, raw: Any, broken: bool = False) -> None:
        if self._closed:
            try:
                raw.close()
            except Exception:
                pass
            return
        if broken and not self._healthy(raw):
            return
        try:
            self._pool.put_nowait(raw)
        except queue.Full:
            try:
                raw.close()
            except Exception:
                pass
            with self._lock:
                self._created -= 1

    # -- Database ------------------------------------------------------------
    @property
    def dialect(self) -> Dialect:
        return self._dialect

    def connect(self) -> PostgresConnection:
        return self._conn

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                raw = self._pool.get_nowait()
            except queue.Empty:
                return
            try:
                raw.close()
            except Exception:
                pass
