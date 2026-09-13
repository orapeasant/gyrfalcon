"""Events — the reactive complement to the imperative flow.

Spec: §10.

Every meaningful occurrence is an `Event`. `follows` is a causality pointer: task
state is client-local (§4.1, no server round trip), so task events can arrive
out of order relative to their flow's events, and `follows` is how a consumer
reassembles a correct timeline without relying on wall-clock arrival order.

This is also the substrate for agent observability (§10): `gyrfalcon.agent.*`
events are ordinary events, and "if an agent escalates twice in an hour, page a
human" is an ordinary automation (`automations.py`) — nothing agent-specific
needs to exist below this module.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from gyrfalcon.flow.db import open_database, resolve_target, sql
from gyrfalcon.flow.db.migrations import ensure_schema
from gyrfalcon.flow.db.scope import Scope, current_scope
from gyrfalcon.identity import require_principal


def _time_ordered_id(occurred: float) -> str:
    """A uuid-shaped, time-ordered id (the stdlib has no uuid7 before 3.14).

    Millisecond timestamp in the high bits, random low bits — sortable by id
    without needing a separate `occurred` index for causal-order scans.
    """
    ms = int(occurred * 1000) & ((1 << 48) - 1)
    rand = uuid.uuid4().int & ((1 << 80) - 1)
    combined = (ms << 80) | rand
    hex_str = f"{combined:032x}"
    return f"{hex_str[0:8]}-{hex_str[8:12]}-7{hex_str[13:16]}-{hex_str[16:20]}-{hex_str[20:32]}"


@dataclass
class RelatedResource:
    id: str
    role: str = "related"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "role": self.role, **self.extra}


@dataclass
class Event:
    event: str
    resource_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    related: list[RelatedResource] = field(default_factory=list)
    follows: Optional[str] = None
    occurred: float = field(default_factory=time.time)
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = _time_ordered_id(self.occurred)


class EventLog:
    """Durable, queryable event store, sharing the flow database (§15)."""

    def __init__(self, db_path=None, backend: Optional[str] = None,
                 dsn: Optional[str] = None):
        self.backend, resolved, self.dsn = resolve_target(db_path, backend, dsn)
        self._db = open_database(backend=self.backend, path=resolved, dsn=self.dsn)
        self.schema_version = ensure_schema(self._db)
        self._subscribers: list[Callable[[Event], None]] = []
        self._sub_lock = threading.Lock()

    def emit(self, event: Event) -> Event:
        who = require_principal()
        with self._db.connect() as conn:
            conn.execute(
                sql.INSERT_EVENT,
                (
                    event.id, event.occurred, event.event, event.resource_id,
                    json.dumps({"id": event.resource_id}),
                    json.dumps([r.to_dict() for r in event.related]),
                    json.dumps(event.payload, default=repr),
                    event.follows, who.user_id, who.tenant_id,
                ),
            )

        with self._sub_lock:
            subscribers = list(self._subscribers)
        for sub in subscribers:
            try:
                sub(event)
            except Exception:
                # Telemetry/automation consumers must never break the caller
                # that emitted the event — this fires from inside the engine.
                import logging
                logging.getLogger("gyrfalcon.flow.events").warning(
                    "event subscriber raised", exc_info=True
                )
        return event

    def subscribe(self, fn: Callable[[Event], None]) -> Callable[[], None]:
        """Register a live listener (automations, tests). Returns an unsubscribe fn."""
        with self._sub_lock:
            self._subscribers.append(fn)

        def unsubscribe() -> None:
            with self._sub_lock:
                if fn in self._subscribers:
                    self._subscribers.remove(fn)

        return unsubscribe

    def list_events(
        self,
        limit: int = 100,
        offset: int = 0,
        event_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        since: Optional[float] = None,
        scope: Optional[Scope] = None,
    ) -> tuple[list[dict], int]:
        clauses, params = [], []
        if event_type:
            clauses.append("event = ?")
            params.append(event_type)
        if resource_id:
            clauses.append("resource_id = ?")
            params.append(resource_id)
        if since is not None:
            clauses.append("occurred >= ?")
            params.append(since)
        where, scope_params = sql.compose_where(current_scope(scope), clauses)
        params = (*scope_params, *params)

        with self._db.connect() as conn:
            rows = conn.fetchall(sql.list_events(where), (*params, limit, offset))
            total = conn.fetchone(sql.count_events(where), params)["c"]
        return [self._row_to_dict(r) for r in rows], total

    def follows_chain(self, event_id: str, scope: Optional[Scope] = None) -> list[dict]:
        """Walk `follows` backward to reconstruct one causal chain."""
        chain = []
        stmt, sp = sql.get_event(current_scope(scope))
        with self._db.connect() as conn:
            current = event_id
            seen: set[str] = set()
            while current and current not in seen:
                seen.add(current)
                row = conn.fetchone(stmt, (*sp, current))
                if row is None:
                    break
                chain.append(self._row_to_dict(row))
                current = row["follows"]
        return list(reversed(chain))

    def _row_to_dict(self, row: Mapping[str, Any]) -> dict:
        return {
            "id": row["id"],
            "occurred": row["occurred"],
            "event": row["event"],
            "resource_id": row["resource_id"],
            "related": json.loads(row["related"]),
            "payload": json.loads(row["payload"]),
            "follows": row["follows"],
            "user_id": row["user_id"],
            "tenant_id": row["tenant_id"],
        }

    def close(self) -> None:
        self._db.close()


_DEFAULT: Optional[EventLog] = None
_DEFAULT_LOCK = threading.Lock()


def get_event_log() -> EventLog:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = EventLog()
        return _DEFAULT


def set_event_log(log: Optional[EventLog]) -> None:
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = log


def emit(
    event: str,
    resource_id: str,
    payload: Optional[dict] = None,
    related: Optional[list[RelatedResource]] = None,
    follows: Optional[str] = None,
) -> Event:
    """Convenience wrapper: build and emit in one call, using the process log."""
    return get_event_log().emit(
        Event(
            event=event, resource_id=resource_id, payload=payload or {},
            related=related or [], follows=follows,
        )
    )
