"""Pluggable persistence for the flow engine.

Spec: §15. `open_database()` is the single construction point — nothing outside
this package should import a dialect module directly, which is what keeps "add
a third backend" (§15.10) a matter of adding one file and one branch here.

Configuration lives under `flow.store.*` (§15.7) and is read through
`store_settings()`. It governs the flow stores *only*: session history stays on
SQLite via `gyrfalcon_state.SessionDB` no matter what is set here (§15.12).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from gyrfalcon.flow.db.base import Connection, Database, Dialect
from gyrfalcon.flow.db.sqlite import SqliteDatabase, SqliteDialect

__all__ = [
    "Connection",
    "Database",
    "Dialect",
    "SqliteDatabase",
    "SqliteDialect",
    "StoreSettings",
    "default_sqlite_path",
    "open_database",
    "resolve_target",
    "store_settings",
]

SUPPORTED_BACKENDS = ("sqlite", "postgres")

#: Spellings people actually type, mapped to the canonical name.
_ALIASES = {"postgresql": "postgres", "pg": "postgres", "sqlite3": "sqlite"}


def _normalize(backend: Optional[str]) -> str:
    name = (backend or "sqlite").strip().lower()
    return _ALIASES.get(name, name)


@dataclass(frozen=True)
class StoreSettings:
    backend: str = "sqlite"
    path: str = ""
    dsn: str = ""
    pool_min_size: int = 1
    pool_max_size: int = 10
    statement_timeout_ms: int = 30000


def store_settings() -> StoreSettings:
    """Read `flow.store.*`, normalizing the backend alias set.

    `RUN_MODE` (§15.12b) gates this before anything else: **CLIENT mode
    always resolves to SQLite**, full stop, regardless of `flow.store.*` or
    any DB env var — a client is single-user by definition, and forcing
    SQLite here is what makes that true structurally rather than by
    convention someone can forget to follow. **SERVER mode** uses the
    configured backend: the `GYRFALCON_DB_BACKEND`/`GYRFALCON_DB_DSN` env
    vars (settable from `.env`) first, then `flow.store.backend`/
    `flow.store.dsn` in `config.yaml`, so a server deployment can point at
    PostgreSQL from `.env` alone. Env-before-config, not the other way
    round, is deliberate: `DEFAULT_CONFIG` (`config.py`) always persists an
    explicit `flow.store.backend: sqlite` once `config.yaml` exists at all,
    so "config first" would make that baked-in default permanently mask an
    `.env` override — the one thing this env pair exists to let you set.
    Neither set still means SQLite — SERVER mode changes what backend *can*
    be configured, not the default.

    Falls back to the SQLite defaults if config is unreadable: an unreadable
    config file should not take the flow engine down when the default backend
    needs no configuration at all.
    """
    from gyrfalcon.gyrfalcon_constants import get_run_mode

    if get_run_mode() == "CLIENT":
        return StoreSettings(backend="sqlite")

    try:
        from gyrfalcon.config import cfg_get
    except Exception:
        return StoreSettings()

    def _int(key: str, default: int) -> int:
        try:
            return int(cfg_get(f"flow.store.{key}", default))
        except (TypeError, ValueError):
            return default

    backend = os.environ.get("GYRFALCON_DB_BACKEND", "") or cfg_get("flow.store.backend", "") or "sqlite"
    dsn = os.environ.get("GYRFALCON_DB_DSN", "") or cfg_get("flow.store.dsn", "") or ""

    return StoreSettings(
        backend=_normalize(backend),
        path=str(cfg_get("flow.store.path", "") or ""),
        dsn=str(dsn),
        pool_min_size=_int("pool_min_size", 1),
        pool_max_size=_int("pool_max_size", 10),
        statement_timeout_ms=_int("statement_timeout_ms", 30000),
    )


def default_sqlite_path() -> Path:
    """`flow.db` inside the active profile.

    Resolved through `get_gyrfalcon_home()` rather than a hardcoded
    `~/.gyrfalcon`, so `GYRFALCON_HOME` keeps profiles isolated.
    """
    from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home

    return get_gyrfalcon_home() / "flow.db"


def resolve_target(
    db_path: Optional[Path | str] = None,
    backend: Optional[str] = None,
    dsn: Optional[str] = None,
) -> tuple[str, Optional[str], Optional[str]]:
    """Work out what a store should actually open.

    Returns `(backend, sqlite_path, dsn)`. Exactly one of the last two is
    meaningful: on PostgreSQL the location is the DSN, and reporting a file
    path nothing writes to would be a lie.

    Precedence, most specific first:

    1. An explicit `db_path` means SQLite, and an explicit `dsn` means
       PostgreSQL, whatever `flow.store.backend` says. That is what lets a
       test or an embedded tool pin its own database on a host configured for
       the other backend — and what makes the suite runnable against both.
    2. An explicit `backend` argument.
    3. `flow.store.backend` / `flow.store.path` / `flow.store.dsn` from config.
    4. `<GYRFALCON_HOME>/flow.db`.

    Step 3 is the one that is easy to skip: falling straight through to the
    profile default ignores a configured `flow.store.path` and silently writes
    to a different file than the operator asked for.
    """
    if backend is None:
        if db_path is not None:
            backend = "sqlite"
        elif dsn is not None:
            backend = "postgres"
        else:
            backend = store_settings().backend
    backend = _normalize(backend)

    if backend != "sqlite":
        return backend, None, dsn or store_settings().dsn
    if db_path is not None:
        return backend, str(db_path), None
    return backend, str(store_settings().path or default_sqlite_path()), None


def _require_pg8000() -> Any:
    """Import the PostgreSQL driver, or explain exactly how to install it.

    A missing driver is a configuration error, not a reason to quietly use
    something else — see the fallback note in `open_database`.
    """
    try:
        import pg8000.dbapi
    except ImportError as exc:
        raise RuntimeError(
            "flow.store.backend is 'postgres' but the pg8000 driver is not "
            "installed. Install it with `uv add pg8000` (or "
            "`pip install pg8000`), or set flow.store.backend to 'sqlite'."
        ) from exc

    # `?` placeholders on both backends, which is what lets one SQL catalog
    # serve SQLite and PostgreSQL without rewriting (§15.3a).
    pg8000.dbapi.paramstyle = "qmark"
    return pg8000.dbapi


def open_database(
    backend: Optional[str] = None,
    path: Optional[Path | str] = None,
    settings: Optional[StoreSettings] = None,
    **options: Any,
) -> Database:
    """Construct the configured backend.

    `backend=None` means "whatever `flow.store.backend` says"; passing one
    explicitly overrides config, which is how tests pin SQLite regardless of
    what the host profile is configured for.

    Misconfiguration always raises. There is deliberately no fall back to
    SQLite when PostgreSQL is asked for and unavailable: silently writing to a
    local file when the operator configured a shared database is the kind of
    "working" that gets discovered weeks later, with the data in the wrong
    place and no error to point at.
    """
    cfg = settings or store_settings()
    backend = _normalize(backend or cfg.backend)

    if backend == "sqlite":
        if path is None:
            path = cfg.path or default_sqlite_path()
        return SqliteDatabase(path)

    if backend == "postgres":
        dsn = str(options.pop("dsn", "") or cfg.dsn)
        if not dsn:
            raise ValueError(
                "flow.store.backend is 'postgres' but flow.store.dsn is empty. "
                "Set a DSN like postgresql://user:pass@host:5432/gyrfalcon."
            )
        from gyrfalcon.flow.db.postgres import PostgresDatabase

        return PostgresDatabase(
            dsn,
            pool_min_size=cfg.pool_min_size,
            pool_max_size=cfg.pool_max_size,
            statement_timeout_ms=cfg.statement_timeout_ms,
        )

    raise ValueError(
        f"Unknown flow store backend {backend!r}. "
        f"Supported: {', '.join(repr(b) for b in SUPPORTED_BACKENDS)}."
    )
