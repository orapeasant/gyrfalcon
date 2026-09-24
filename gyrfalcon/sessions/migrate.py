"""Copy the legacy SQLite session store into the shared database.

Spec: §19.7.

Two properties matter more than speed here:

* **Idempotent.** A session already present is skipped, so an interrupted run
  is resumed by running it again rather than by reasoning about what got half
  copied.
* **Non-destructive.** The legacy file is opened read-only and never modified.
  If anything about the copy is wrong, the original is still the record.

Legacy rows have no tenant, so they are filed under the scope of whoever runs
the command — on a single-user install, `local`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from gyrfalcon.db.scope import Scope, current_scope
from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("sessions.migrate")


@dataclass
class MigrationReport:
    sessions_copied: int = 0
    sessions_skipped: int = 0
    messages_copied: int = 0
    source: str = ""
    errors: list[str] = None            # type: ignore[assignment]

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def legacy_path() -> Path:
    from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home

    return Path(get_gyrfalcon_home()) / "sessions.db"


def migrate(source: Optional[Path] = None, store=None,
            scope: Optional[Scope] = None) -> MigrationReport:
    """Copy sessions and messages from the legacy file into the shared store."""
    source = Path(source) if source else legacy_path()
    report = MigrationReport(source=str(source))
    if not source.is_file():
        report.errors.append(f"no legacy store at {source}")
        return report

    from gyrfalcon.sessions.store import get_session_store

    store = store or get_session_store()
    scope = scope or current_scope()

    # Read-only: `mode=ro` fails loudly rather than creating an empty file if
    # the path is wrong, which a plain connect() would do silently.
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sessions = conn.execute("SELECT * FROM sessions").fetchall()
        for row in sessions:
            session_id = row["id"]
            if store.get_session(session_id, scope=scope) is not None:
                report.sessions_skipped += 1
                continue
            try:
                store.create_session(
                    session_id=session_id,
                    source=row["source"],
                    model=row["model"],
                    system_prompt=row["system_prompt"],
                    agent_id=row["agent_id"],
                    parent_session_id=row["parent_session_id"],
                    title=row["title"],
                    scope=scope,
                )
                messages = conn.execute(
                    "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
                    (session_id,),
                ).fetchall()
                for message in messages:
                    store.append_message(
                        session_id,
                        message["role"],
                        content=message["content"],
                        tool_call_id=message["tool_call_id"],
                        tool_calls=message["tool_calls"],
                        tool_name=message["tool_name"],
                        reasoning=message["reasoning"],
                        scope=scope,
                    )
                    report.messages_copied += 1
                report.sessions_copied += 1
            except Exception as err:
                # One bad session must not abandon the rest of the copy.
                report.errors.append(f"{session_id}: {err}")
    finally:
        conn.close()
    return report
