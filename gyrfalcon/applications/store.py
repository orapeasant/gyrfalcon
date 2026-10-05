"""Persistence for application catalog records."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from typing import Any, Optional

from gyrfalcon.db import open_database, resolve_target
from gyrfalcon.db.migrations import ensure_schema
from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _row(row) -> dict[str, Any]:
    result = dict(row)
    for key in ("args", "env", "tags", "source_sessions", "config"):
        result[key] = _json_value(result.get(key))
    result["enabled"] = bool(result["enabled"])
    for key in ("created_at", "updated_at"):
        result[key] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(result[key]))
    return result


def _code(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "_", name.strip()).strip("_").upper()
    if not value:
        raise ValueError("Application code or name is required")
    return value[:64]


class ApplicationStore:
    def __init__(self, db_path=None, backend: Optional[str] = None, dsn: Optional[str] = None):
        self.backend, resolved, self.dsn = resolve_target(db_path, backend, dsn)
        self._db = open_database(backend=self.backend, path=resolved, dsn=self.dsn)
        ensure_schema(self._db)
        self._import_legacy_file()

    def _import_legacy_file(self) -> None:
        """Move old per-install JSON records into the table, then retire the file."""
        legacy_file = get_gyrfalcon_home() / "applications.json"
        if not legacy_file.exists():
            return
        try:
            payload = json.loads(legacy_file.read_text(encoding="utf-8"))
            records = payload.get("applications", []) if isinstance(payload, dict) else []
            if not isinstance(records, list):
                return
            with self._db.connect() as conn:
                existing = {row["id"] for row in conn.fetchall(
                    "SELECT id FROM fnd_applications"
                )}
            complete = True
            for index, record in enumerate(records):
                if (not isinstance(record, dict)
                        or not isinstance(record.get("name"), str)
                        or not record["name"].strip()):
                    complete = False
                    continue
                app_id = str(record.get("id") or f"legacy-{index}")
                if app_id in existing:
                    continue
                self.create({
                    "_id": app_id,
                    "code": "LEGACY_" + re.sub(r"[^A-Za-z0-9]+", "_", app_id).upper()[:48],
                    "name": record["name"], "description": record.get("description", ""),
                    "enabled": record.get("enabled", True), "command": record.get("command", ""),
                    "args": record.get("args", []), "env": record.get("env", {}),
                    "tags": record.get("tags", []), "version": record.get("version", "1.0.0"),
                    "source_sessions": record.get("source_sessions", []),
                    "config": record.get("config", {}),
                })
                existing.add(app_id)
            if complete:
                legacy_file.unlink()
        except (OSError, ValueError, TypeError):
            # Leave the source in place if its records could not be imported.
            return

    def list(self) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            return [_row(row) for row in conn.fetchall(
                "SELECT * FROM fnd_applications ORDER BY name, code"
            )]

    def get(self, application_id: str) -> Optional[dict[str, Any]]:
        with self._db.connect() as conn:
            row = conn.fetchone("SELECT * FROM fnd_applications WHERE id = ?", (application_id,))
        return _row(row) if row else None

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        app_id = str(data.get("_id") or uuid.uuid4().hex)
        code = _code(data.get("code") or data["name"])
        with self._db.connect() as conn:
            conn.execute(
                "INSERT INTO fnd_applications "
                "(id, code, name, description, enabled, command, args, env, tags, version, "
                "source_sessions, config, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (app_id, code, data["name"].strip(), data.get("description", ""),
                 bool(data.get("enabled", True)), data.get("command", ""),
                 json.dumps(data.get("args", [])), json.dumps(data.get("env", {})),
                 json.dumps(data.get("tags", [])), data.get("version", "1.0.0"),
                 json.dumps(data.get("source_sessions", [])),
                 json.dumps(data.get("config", {})), now, now),
            )
        return self.get(app_id)  # type: ignore[return-value]

    def update(self, application_id: str, data: dict[str, Any]) -> Optional[dict[str, Any]]:
        now = time.time()
        with self._db.connect() as conn:
            current = conn.fetchone("SELECT code FROM fnd_applications WHERE id = ?", (application_id,))
            if current is None:
                return None
            code = _code(data.get("code") or current["code"])
            conn.execute(
                "UPDATE fnd_applications SET code = ?, name = ?, description = ?, enabled = ?, "
                "command = ?, args = ?, env = ?, tags = ?, version = ?, source_sessions = ?, "
                "config = ?, updated_at = ? WHERE id = ?",
                (code, data["name"].strip(), data.get("description", ""),
                 bool(data.get("enabled", True)), data.get("command", ""),
                 json.dumps(data.get("args", [])), json.dumps(data.get("env", {})),
                 json.dumps(data.get("tags", [])), data.get("version", "1.0.0"),
                 json.dumps(data.get("source_sessions", [])),
                 json.dumps(data.get("config", {})), now, application_id),
            )
        return self.get(application_id)

    def delete(self, application_id: str) -> bool:
        with self._db.connect() as conn:
            row = conn.fetchone("SELECT code FROM fnd_applications WHERE id = ?", (application_id,))
            if row is None:
                return False
            if row["code"] == "FND":
                raise ValueError("The Foundation application cannot be deleted")
            for table in ("fnd_nav_functions", "fnd_nav_menus", "fnd_nav_menu_items"):
                if conn.fetchone(f"SELECT 1 FROM {table} WHERE application_id = ? LIMIT 1", (application_id,)):
                    raise ValueError("Application is assigned to navigation records")
            cursor = conn.execute("DELETE FROM fnd_applications WHERE id = ?", (application_id,))
            return bool(getattr(cursor, "rowcount", 0))


_store: Optional[ApplicationStore] = None
_store_lock = threading.Lock()


def get_application_store() -> ApplicationStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ApplicationStore()
    return _store
