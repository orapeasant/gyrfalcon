"""SQLAlchemy Core engine and transaction management for PostgreSQL."""

from __future__ import annotations

import atexit
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection as SAConnection, Engine, make_url


@dataclass(frozen=True)
class StoreSettings:
    backend: str = "postgres"
    dsn: str = ""
    pool_size: int = 10
    statement_timeout_ms: int = 30000


def store_settings() -> StoreSettings:
    from gyrfalcon.config import cfg_get

    def integer(key: str, default: int) -> int:
        try:
            return max(1, int(cfg_get(f"flow.store.{key}", default)))
        except (ValueError, TypeError):
            return default

    backend = (os.environ.get("GYRFALCON_DB_BACKEND") or cfg_get("flow.store.backend", "postgres")).lower()
    if backend not in {"postgres", "postgresql", "pg"}:
        raise ValueError("PostgreSQL is the only supported database backend")
    return StoreSettings(
        dsn=str(os.environ.get("GYRFALCON_DB_DSN") or cfg_get("flow.store.dsn", "")),
        pool_size=integer("pool_max_size", 10),
        statement_timeout_ms=integer("statement_timeout_ms", 30000),
    )


def _sqlalchemy_dsn(dsn: str) -> str:
    url = make_url(dsn)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    if url.drivername != "postgresql+psycopg":
        raise ValueError("flow.store.dsn must use PostgreSQL with the Psycopg 3 driver")
    if not url.database:
        raise ValueError("flow.store.dsn must name a database")
    return url.render_as_string(hide_password=False)


class Database:
    """Owns a SQLAlchemy Engine and gives nested callers one transaction."""

    def __init__(self, dsn: str, pool_size: int = 10, statement_timeout_ms: int = 30000):
        if not dsn:
            raise ValueError("PostgreSQL DSN is required (flow.store.dsn or GYRFALCON_DB_DSN)")
        self.dsn = _sqlalchemy_dsn(dsn)
        self.engine: Engine = create_engine(
            self.dsn,
            pool_size=max(1, int(pool_size)),
            max_overflow=0,
            pool_pre_ping=True,
            future=True,
        )
        timeout = max(1, int(statement_timeout_ms))

        @event.listens_for(self.engine, "connect")
        def set_statement_timeout(dbapi_connection, _connection_record) -> None:
            with dbapi_connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, false)",
                    (str(timeout),),
                )
            dbapi_connection.commit()

        self._local = threading.local()
        self._closed = False
        self._shared = False

    @contextmanager
    def connect(self) -> Iterator["StoreConnection"]:
        if self._closed:
            raise RuntimeError("Database engine is closed")
        state = getattr(self._local, "state", None)
        if state is not None:
            state["depth"] += 1
            try:
                yield StoreConnection(state["connection"])
            except BaseException:
                state["failed"] = True
                raise
            finally:
                state["depth"] -= 1
            return

        connection = self.engine.connect()
        transaction = connection.begin()
        state = {"connection": connection, "transaction": transaction, "depth": 1, "failed": False}
        self._local.state = state
        try:
            yield StoreConnection(connection)
        except BaseException:
            state["failed"] = True
            raise
        finally:
            del self._local.state
            try:
                if state["failed"]:
                    transaction.rollback()
                else:
                    transaction.commit()
            finally:
                connection.close()

    @contextmanager
    def migration_lock(self) -> Iterator[SAConnection]:
        """Hold a session advisory lock on a dedicated pooled connection."""
        lock_connection = self.engine.connect()
        key = 7_233_119_004
        try:
            lock_connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
            lock_connection.commit()
            try:
                yield lock_connection
            finally:
                lock_connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                lock_connection.commit()
        finally:
            lock_connection.close()

    def close(self) -> None:
        # A feature store may finish while other stores are using this process's
        # shared engine. Only the process owner disposes a shared pool.
        if self._shared:
            return
        self._dispose()

    def _dispose(self) -> None:
        self._closed = True
        self.engine.dispose()


_pool_lock = threading.RLock()
_pooled_databases: dict[tuple[str, int, int], Database] = {}


def dispose_database_pools() -> None:
    """Dispose this process's shared pools during application shutdown."""
    with _pool_lock:
        databases = list(_pooled_databases.values())
        _pooled_databases.clear()
        for database in databases:
            database._dispose()


def _after_fork() -> None:
    """Replace inherited connections if an application forks after startup."""
    global _pool_lock
    _pool_lock = threading.RLock()
    for database in _pooled_databases.values():
        database.engine.dispose(close=False)
        database._local = threading.local()


atexit.register(dispose_database_pools)
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


class StoreConnection:
    """Small SQLAlchemy Core adapter for the stores' established call shape."""

    def __init__(self, connection: SAConnection):
        self.connection = connection

    @staticmethod
    def _statement(statement: str, params=()):
        from sqlalchemy import text

        if isinstance(params, dict):
            return text(statement), params
        values = tuple(params or ())
        index = 0
        def bind(_match):
            nonlocal index
            name = f"p{index}"
            index += 1
            return f":{name}"
        converted = re.sub(r"\?", bind, statement)
        if index != len(values):
            raise ValueError(f"SQL has {index} placeholders but received {len(values)} values")
        return text(converted), {f"p{i}": value for i, value in enumerate(values)}

    def execute(self, statement: str, params=()):
        query, bindings = self._statement(statement, params)
        return self.connection.execute(query, bindings)

    def fetchone(self, statement: str, params=()):
        row = self.execute(statement, params).mappings().first()
        return row

    def fetchall(self, statement: str, params=()):
        return self.execute(statement, params).mappings().all()

    def __getattr__(self, name):
        return getattr(self.connection, name)


def open_database(
    backend: str | None = None,
    path: object | None = None,
    settings: StoreSettings | None = None,
    **options: object,
) -> Database:
    """Return the shared Database for this process and connection settings.

    Feature stores may call close() when finished; shared pools are disposed
    only by dispose_database_pools() or at process exit.
    """
    if path is not None:
        raise ValueError("File-backed databases are no longer supported")
    if backend is not None and backend.lower() not in {"postgres", "postgresql", "pg"}:
        raise ValueError("PostgreSQL is the only supported database backend")
    config = settings or store_settings()
    dsn = str(options.pop("dsn", "") or config.dsn)
    if not dsn:
        raise ValueError("PostgreSQL DSN is required (flow.store.dsn or GYRFALCON_DB_DSN)")
    key = (_sqlalchemy_dsn(dsn), max(1, int(config.pool_size)),
           max(1, int(config.statement_timeout_ms)))
    with _pool_lock:
        database = _pooled_databases.get(key)
        if database is None:
            database = Database(key[0], pool_size=key[1], statement_timeout_ms=key[2])
            database._shared = True
            _pooled_databases[key] = database
        return database


def resolve_target(db_path: object | None = None, backend: str | None = None, dsn: str | None = None) -> tuple[str, None, str]:
    if db_path is not None:
        raise ValueError("File-backed databases are no longer supported")
    if backend is not None and backend.lower() not in {"postgres", "postgresql", "pg"}:
        raise ValueError("PostgreSQL is the only supported database backend")
    target = dsn or store_settings().dsn
    if not target:
        raise ValueError("PostgreSQL DSN is required (flow.store.dsn or GYRFALCON_DB_DSN)")
    return "postgres", None, target
