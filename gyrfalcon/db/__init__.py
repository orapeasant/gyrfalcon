"""PostgreSQL persistence for flows, identity, navigation, and sessions."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from gyrfalcon.db.base import Connection, Database, Dialect

__all__ = ["Connection", "Database", "Dialect", "StoreSettings", "open_database", "resolve_target", "store_settings"]


@dataclass(frozen=True)
class StoreSettings:
    backend: str = "postgres"
    dsn: str = ""
    pool_min_size: int = 1
    pool_max_size: int = 10
    statement_timeout_ms: int = 30000


def store_settings() -> StoreSettings:
    from gyrfalcon.config import cfg_get

    def integer(key: str, default: int) -> int:
        try:
            return int(cfg_get(f"flow.store.{key}", default))
        except (ValueError, TypeError):
            return default

    backend = (os.environ.get("GYRFALCON_DB_BACKEND") or cfg_get("flow.store.backend", "postgres")).lower()
    if backend not in {"postgres", "postgresql", "pg"}:
        raise ValueError("PostgreSQL is the only supported database backend")
    return StoreSettings(
        dsn=str(os.environ.get("GYRFALCON_DB_DSN") or cfg_get("flow.store.dsn", "")),
        pool_min_size=integer("pool_min_size", 1),
        pool_max_size=integer("pool_max_size", 10),
        statement_timeout_ms=integer("statement_timeout_ms", 30000),
    )


def resolve_target(
    db_path: Optional[Path | str] = None,
    backend: Optional[str] = None,
    dsn: Optional[str] = None,
) -> tuple[str, None, str]:
    if db_path is not None:
        raise ValueError("File-backed databases are no longer supported")
    if backend is not None and backend.lower() not in {"postgres", "postgresql", "pg"}:
        raise ValueError("PostgreSQL is the only supported database backend")
    target = dsn or store_settings().dsn
    if not target:
        raise ValueError("PostgreSQL DSN is required (flow.store.dsn or GYRFALCON_DB_DSN)")
    return "postgres", None, target


def _require_pg8000() -> Any:
    try:
        import pg8000.dbapi
    except ImportError as exc:
        raise RuntimeError("PostgreSQL requires pg8000") from exc
    pg8000.dbapi.paramstyle = "qmark"
    return pg8000.dbapi


def open_database(
    backend: Optional[str] = None,
    path: Optional[Path | str] = None,
    settings: Optional[StoreSettings] = None,
    **options: Any,
) -> Database:
    if path is not None:
        raise ValueError("File-backed databases are no longer supported")
    if backend is not None and backend.lower() not in {"postgres", "postgresql", "pg"}:
        raise ValueError("PostgreSQL is the only supported database backend")
    cfg = settings or store_settings()
    dsn = str(options.pop("dsn", "") or cfg.dsn)
    if not dsn:
        raise ValueError("PostgreSQL DSN is required (flow.store.dsn or GYRFALCON_DB_DSN)")
    from gyrfalcon.db.postgres import PostgresDatabase
    return PostgresDatabase(
        dsn,
        pool_min_size=cfg.pool_min_size,
        pool_max_size=cfg.pool_max_size,
        statement_timeout_ms=cfg.statement_timeout_ms,
    )
