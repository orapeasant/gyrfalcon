"""PostgreSQL storage for the optional mail intake sample."""

from __future__ import annotations

import threading
import uuid
from typing import Any, Optional

from gyrfalcon.db import open_database
from gyrfalcon.db.migrations import ensure_schema


class MailDB:
    def __init__(self):
        self._db = open_database()
        ensure_schema(self._db)

    def execute(self, statement: str, params: tuple = ()) -> Any:
        with self._db.connect() as conn:
            return conn.execute(statement, params)

    def fetchone(self, statement: str, params: tuple = ()) -> Optional[dict]:
        with self._db.connect() as conn:
            row = conn.fetchone(statement, params)
        return dict(row) if row else None

    def fetchall(self, statement: str, params: tuple = ()) -> list[dict]:
        with self._db.connect() as conn:
            return [dict(row) for row in conn.fetchall(statement, params)]

    def close(self) -> None:
        self._db.close()


_singleton_lock = threading.Lock()
_singleton: Optional[MailDB] = None


def get_mail_db() -> MailDB:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = MailDB()
    return _singleton


def new_id() -> str:
    return uuid.uuid4().hex
