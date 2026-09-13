"""Durable run store — phase 4.

Spec: §13.5 — "Phase 4 is where it becomes a *platform* — that is the expensive,
correct, do-not-skip step."

Until runs are durable, `set_state` cannot be an authoritative boundary: there is
nothing for an operator to cancel, no history to query, and nothing to recover
after a restart. Everything downstream (the API, the run graph, cancellation)
reads from here.

Storage is reached through `flow/db/` (§15): this module owns *what a
transition means* and nothing about how rows are written. The SQL it runs
lives in `flow/db/sql.py`, so the same statements serve SQLite and PostgreSQL
without a second copy to keep in step.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional

from gyrfalcon.flow import states as st
from gyrfalcon.flow.db import open_database, resolve_target, sql
from gyrfalcon.flow.db.migrations import ensure_schema
from gyrfalcon.flow.db.scope import Scope, current_scope
from gyrfalcon.flow.states import State, StateType
from gyrfalcon.identity import require_principal

#: How long an owner may go silent before its runs are considered abandoned.
#: Must comfortably exceed HEARTBEAT_SECONDS — a GC pause or a slow query
#: must not get a live run reclaimed out from under it.
STALE_OWNER_SECONDS = 90.0
HEARTBEAT_SECONDS = 20.0


def _tenant_run_cap() -> int:
    """Non-terminal runs one tenant may hold at once. 0 disables the cap.

    Off unless identity is enabled: a single-user install is its own tenant
    and there is nobody to be fair to.
    """
    from gyrfalcon.identity import identity_enabled

    if not identity_enabled():
        return 0
    try:
        from gyrfalcon.config import cfg_get

        return max(0, int(cfg_get("flow.limits.tenant_max_active_runs", 0) or 0))
    except Exception:
        return 0


def _dumps(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return json.dumps(value, default=repr)
    except Exception:
        return json.dumps(repr(value))


def _loads(raw: Optional[str]) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw


class RunStore:
    """Durable run persistence over a configured backend (§15.7).

    Thread-safety is the `Connection`'s job, not this class's: task runs
    execute on pool threads, so the store is touched concurrently, and the
    backend decides how that is made safe.
    """

    def __init__(
        self,
        db_path: Optional[Path | str] = None,
        reconcile: bool = True,
        emit_events: bool = True,
        event_log: Optional[Any] = None,
        backend: Optional[str] = None,
        dsn: Optional[str] = None,
    ):
        used_default_path = db_path is None and dsn is None
        # `db_path` is empty on PostgreSQL, where the location is the DSN and
        # reporting a file path nothing writes to would be a lie.
        self.backend, resolved, self.dsn = resolve_target(db_path, backend, dsn)
        self.db_path = resolved or ""
        self.emit_events = emit_events

        # Only a privately-constructed EventLog is ours to close; closing the
        # shared singleton just because one RunStore built on top of it was
        # torn down would break every other part of the process still using it.
        self._owns_event_log = False

        if event_log is not None:
            self._event_log = event_log
        elif not emit_events:
            self._event_log = None
        elif used_default_path:
            # The default store IS the process's event stream: reuse the
            # process-global singleton rather than a second EventLog pointed
            # at the same file. Two EventLog objects on the same file have
            # independent in-memory subscriber lists — an AutomationEngine
            # subscribed to get_event_log() would never see events emitted
            # through a *different* EventLog instance on that file, even
            # though both write to the same table.
            from gyrfalcon.flow.events import get_event_log
            self._event_log = get_event_log()
        else:
            # An explicit path (tests, an isolated profile) must not leak
            # events into whatever the process-global default points at.
            from gyrfalcon.flow.events import EventLog
            self._event_log = EventLog(
                self.db_path or None, backend=self.backend, dsn=self.dsn
            )
            self._owns_event_log = True

        #: Identifies this engine instance among everything writing to the
        #: database. A run carries its owner's id so a peer can tell "still
        #: running elsewhere" from "abandoned" (§15.6.1).
        self.instance_id = uuid.uuid4().hex
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None

        self._db = open_database(backend=self.backend, path=resolved, dsn=self.dsn)
        self.schema_version = ensure_schema(self._db)

        if reconcile:
            self._reconcile_crashed()
        # run_id -> last emitted event id, so a run's own transitions form a
        # causal chain via `follows` (§10) without a query per emission.
        self._last_event: dict[str, str] = {}

    def _reconcile_crashed(self) -> None:
        """Mark non-terminal runs left over from a previous process as Crashed.

        A fresh `RunStore()` against an existing database is, for our
        purposes, "the process starting" — nothing in *this* process can still
        be executing a run row that predates its own construction. Left alone,
        a run whose process died mid-flight (kill -9, host reboot) sits in
        RUNNING/CANCELLING/SCHEDULED forever: no run confirms it stopped, so
        nothing ever settles it. Never claim a clean outcome you did not verify
        (§9.2's CancelFinalizer principle) — Crashed, not silence.

        Safe under multiple writers (§15.6.1): a run is only claimed when no
        engine is heartbeating it — `heartbeat_at` is NULL (released on a clean
        shutdown, or never claimed) or older than STALE_OWNER_SECONDS. A peer's
        live run on another machine has a fresh heartbeat and is left alone.
        The reasoning is unchanged, only its evidence: silence for longer than
        an owner could plausibly be quiet, rather than "a store opened".
        """
        now = time.time()
        abandoned = (
            StateType.PENDING.value,
            StateType.RUNNING.value,
            StateType.CANCELLING.value,
            StateType.SCHEDULED.value,
        )
        cutoff = now - STALE_OWNER_SECONDS
        scope = Scope.system(
            reason="crash reconciliation settles abandoned runs in every tenant"
        )
        stmt, sp = sql.abandoned_runs(scope, len(abandoned))
        with self._db.connect() as conn:
            stale = conn.fetchall(stmt, (*sp, *abandoned, cutoff))
            for row in stale:
                run_id = row["id"]
                seq = (conn.fetchone(sql.NEXT_SEQ, (run_id,))["s"]) + 1
                conn.execute(
                    sql.INSERT_RUN_STATE,
                    (run_id, seq, StateType.CRASHED.value, "Crashed",
                     "interrupted: no run confirmed this state before the process restarted",
                     "{}", "ACCEPT", now),
                )
                conn.execute(
                    sql.MARK_CRASHED,
                    (StateType.CRASHED.value, "Crashed", now, now,
                     _dumps("interrupted"), run_id),
                )

    # -- writes --------------------------------------------------------------
    def create_run(
        self,
        run_id: str,
        name: str,
        kind: str,
        parameters: Optional[dict] = None,
        parent_run_id: Optional[str] = None,
        flow_run_id: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> None:
        with self._db.connect() as conn:
            self._insert_run(conn, run_id, name, kind, parameters,
                             parent_run_id, flow_run_id, tags)
        self._ensure_heartbeat()

    def _insert_run(self, conn, run_id, name, kind, parameters=None,
                    parent_run_id=None, flow_run_id=None, tags=None) -> None:
        now = time.time()
        # A child run inherits its parent's identity for free: the principal
        # rides the same contextvar the run context does, and every mechanism
        # that crosses a thread copies the whole context (§17.4). It is never
        # re-derived here, so a task cannot end up owned by someone other than
        # whoever started the flow.
        who = require_principal()
        conn.execute(
            sql.insert_run(self._db.dialect),
            (run_id, name, kind, StateType.PENDING.value, "Pending",
             _dumps(parameters or {}), parent_run_id, flow_run_id,
             _dumps(tags or []), now, now, self.instance_id, now,
             who.user_id, who.tenant_id),
        )

    def reserve_run_slot(
        self, name: str, limit: Optional[int], **kwargs: Any
    ) -> Optional[str]:
        """Count active runs and create the new run row in ONE transaction.

        Spec §15.6.3. `count_active()` followed by a decision to start is
        check-then-act: two runners both count `limit - 1`, both start, and the
        limit is exceeded. Single-process SQLite hid this; two runners make it
        wrong. Doing both inside one transaction makes the count and the row
        that invalidates it inseparable.

        Returns the reserved run id, or None if the limit is already reached.
        The row is a real PENDING run, so the engine's own `create_run` for
        that id is an INSERT-OR-IGNORE no-op and the reservation stands.
        """
        run_id = st.new_run_id()
        terminal = tuple(t.value for t in st.TERMINAL_STATES)
        scope = Scope.of()
        stmt, sp = sql.count_active_by_name(scope, len(terminal))
        tenant_stmt, tsp = sql.count_active_for_tenant(scope, len(terminal))
        tenant_cap = _tenant_run_cap()
        with self._db.connect() as conn:
            if limit is not None:
                row = conn.fetchone(stmt, (*sp, name, *terminal))
                if row["c"] >= limit:
                    return None
            # Same transaction, same reason (§15.6.3): a tenant cap checked
            # outside the insert is check-then-act all over again.
            if tenant_cap:
                row = conn.fetchone(tenant_stmt, (*tsp, *terminal))
                if row["c"] >= tenant_cap:
                    return None
            self._insert_run(conn, run_id, name, "flow", **kwargs)
        self._ensure_heartbeat()
        return run_id

    # -- ownership (§15.6.1) -------------------------------------------------
    def _ensure_heartbeat(self) -> None:
        """Start the heartbeat on first use, not at construction.

        Most RunStore instances (tests, one-shot queries) never own a run, and
        a thread per store would be pure overhead for them.
        """
        if self._heartbeat_thread is not None or self._heartbeat_stop.is_set():
            return
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True,
            name=f"flow-heartbeat-{self.instance_id[:8]}",
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(HEARTBEAT_SECONDS):
            try:
                self.touch_owned_runs()
            except Exception:
                import logging
                logging.getLogger("gyrfalcon.flow.store").warning(
                    "heartbeat failed", exc_info=True
                )

    def touch_owned_runs(self) -> None:
        """One UPDATE for every run this instance still owns."""
        with self._db.connect() as conn:
            conn.execute(sql.TOUCH_OWNED_RUNS, (time.time(), self.instance_id))

    def release_owned_runs(self) -> None:
        """Drop ownership so a peer can reclaim without waiting out the
        stale threshold. Called on clean shutdown; an unclean death is
        precisely what the threshold exists to cover."""
        with self._db.connect() as conn:
            conn.execute(sql.RELEASE_OWNED_RUNS, (self.instance_id,))

    def record_transition(
        self,
        run_id: str,
        state: State,
        orchestration: str = "ACCEPT",
    ) -> None:
        """Persist a state change and append it to the run's history."""
        now = time.time()
        with self._db.connect() as conn:
            row = conn.fetchone(sql.NEXT_SEQ, (run_id,))
            seq = (row["s"] if row else 0) + 1

            conn.execute(
                sql.INSERT_RUN_STATE,
                (run_id, seq, state.type.value, state.name, state.message,
                 _dumps(state.state_details.model_dump(mode="json")), orchestration, now),
            )

            if orchestration == "ACCEPT":
                finished = now if state.is_final() else None
                started = now if state.is_running() else None
                # A terminal run needs no owner, and leaving one behind would
                # keep it in every heartbeat UPDATE forever. A live one is
                # claimed by whoever moved it, which is what makes another
                # process able to tell running-elsewhere from abandoned.
                owner = None if state.is_final() else self.instance_id
                heartbeat = None if state.is_final() else now
                conn.execute(
                    sql.APPLY_TRANSITION,
                    (
                        state.type.value, state.name, now, started, finished,
                        _dumps(state.data) if state.is_completed() else None,
                        _dumps(str(state.exception)) if state.exception is not None else None,
                        owner, heartbeat, run_id,
                    ),
                )
                if not state.is_final():
                    self._ensure_heartbeat()

        # Emitted after the commit, deliberately: the state write is the source
        # of truth and must be durable before anything observes it.
        self._emit_transition_event(run_id, state, orchestration, now)

    def _emit_transition_event(
        self, run_id: str, state: State, orchestration: str, occurred: float
    ) -> None:
        """Emit a run-state-change event, chained via `follows` to this run's
        own previous event.

        Failure here must never break the caller that just persisted a
        transition — the state write already succeeded and is the source of
        truth; the event stream is an observability projection of it.
        """
        if not self.emit_events or self._event_log is None:
            return
        try:
            from gyrfalcon.flow.events import Event

            with self._db.connect() as conn:
                stmt, sp = sql.run_kind_and_name(
                Scope.system(reason="event emission annotates any run it is given")
            )
            row = conn.fetchone(stmt, (*sp, run_id))
            kind = row["kind"] if row else "run"
            evt = Event(
                event=f"gyrfalcon.{kind}-run.{state.name}",
                resource_id=run_id,
                payload={
                    "name": row["name"] if row else None,
                    "state_type": state.type.value,
                    "state_name": state.name,
                    "orchestration": orchestration,
                },
                follows=self._last_event.get(run_id),
            )
            self._event_log.emit(evt)
            self._last_event[run_id] = evt.id
        except Exception:
            import logging
            logging.getLogger("gyrfalcon.flow.store").warning(
                "failed to emit transition event", exc_info=True
            )

    def record_edge(self, downstream: str, upstream: str, kind: str = "data") -> None:
        with self._db.connect() as conn:
            conn.execute(sql.insert_edge(self._db.dialect), (downstream, upstream, kind))

    # -- reads ---------------------------------------------------------------
    def get_run(self, run_id: str, scope: Optional[Scope] = None) -> Optional[dict]:
        stmt, sp = sql.get_run(current_scope(scope))
        with self._db.connect() as conn:
            row = conn.fetchone(stmt, (*sp, run_id))
        return self._row_to_run(row) if row else None

    def list_runs(
        self,
        limit: int = 50,
        offset: int = 0,
        state_types: Optional[list[str]] = None,
        name: Optional[str] = None,
        kind: Optional[str] = None,
        flow_run_id: Optional[str] = None,
        scope: Optional[Scope] = None,
    ) -> tuple[list[dict], int]:
        """Filter by structured predicate. Returns (rows, total)."""
        where, params = self._build_where(
            current_scope(scope), state_types, name, kind, flow_run_id
        )
        with self._db.connect() as conn:
            rows = conn.fetchall(sql.list_runs(where), (*params, limit, offset))
            total = conn.fetchone(sql.count_runs(where), params)["c"]
        return [self._row_to_run(r) for r in rows], total

    def _build_where(self, scope, state_types, name, kind, flow_run_id) -> tuple[str, tuple]:
        """Collect this query's own filters; `compose_where` adds the scope.

        The WHERE string is never assembled here — that happens in exactly one
        place (§17.5), so a filter cannot be omitted by forgetting it.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if state_types:
            clauses.append(f"state_type IN ({sql.placeholders(len(state_types))})")
            params.extend(state_types)
        if name:
            clauses.append("name LIKE ?")
            params.append(f"%{name}%")
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if flow_run_id:
            clauses.append("flow_run_id = ?")
            params.append(flow_run_id)
        where, scope_params = sql.compose_where(scope, clauses)
        return where, (*scope_params, *params)

    def get_history(self, run_id: str, scope: Optional[Scope] = None) -> list[dict]:
        # Visibility of the history follows visibility of the run: check the
        # run first rather than scoping the child table separately, so the two
        # can never disagree about who may read what.
        if self.get_run(run_id, scope) is None:
            return []
        with self._db.connect() as conn:
            rows = conn.fetchall(sql.GET_HISTORY, (run_id,))
        return [
            {
                "seq": r["seq"],
                "state_type": r["state_type"],
                "state_name": r["state_name"],
                "message": r["message"],
                "state_details": _loads(r["state_details"]),
                "orchestration": r["orchestration"],
                "at": r["at"],
            }
            for r in rows
        ]

    def get_graph(self, run_id: str, scope: Optional[Scope] = None) -> dict:
        """Nodes and edges for one flow run, for the run-graph view."""
        stmt, sp = sql.graph_nodes(current_scope(scope))
        with self._db.connect() as conn:
            nodes = conn.fetchall(stmt, (*sp, run_id, run_id, run_id))
            ids = tuple(n["id"] for n in nodes) or ("",)
            edges = conn.fetchall(sql.edges_for(len(ids)), ids)
        return {
            "nodes": [self._row_to_run(n) for n in nodes],
            "edges": [
                {"upstream": e["upstream"], "downstream": e["downstream"], "kind": e["kind"]}
                for e in edges
            ],
        }

    def count_active(self, name: str, scope: Optional[Scope] = None) -> int:
        """Exact-name count of non-terminal runs — for concurrency limiting.

        Deliberately not `list_runs(name=...)`: that filter is a fuzzy `LIKE
        %name%` built for the search UI, which would over-count against any
        flow whose name is a substring of another's (e.g. `ingest` matching
        `ingest_v2`).
        """
        terminal = tuple(t.value for t in st.TERMINAL_STATES)
        stmt, sp = sql.count_active_by_name(current_scope(scope), len(terminal))
        with self._db.connect() as conn:
            row = conn.fetchone(stmt, (*sp, name, *terminal))
        return row["c"]

    def counts_by_state(self, scope: Optional[Scope] = None) -> dict[str, int]:
        stmt, sp = sql.counts_by_state(current_scope(scope))
        with self._db.connect() as conn:
            rows = conn.fetchall(stmt, sp)
        return {r["state_type"]: r["c"] for r in rows}

    def history_buckets(self, hours: int = 24, buckets: int = 24,
                        scope: Optional[Scope] = None) -> list[dict]:
        """Pre-bucketed aggregates, so the UI never aggregates (§11)."""
        now = time.time()
        span = hours * 3600
        width = span / max(buckets, 1)
        start = now - span
        stmt, sp = sql.runs_created_since(current_scope(scope))
        with self._db.connect() as conn:
            rows = conn.fetchall(stmt, (*sp, start))

        out = [
            {"start": start + i * width, "end": start + (i + 1) * width, "counts": {}}
            for i in range(buckets)
        ]
        for row in rows:
            idx = min(int((row["created_at"] - start) / width), buckets - 1)
            counts = out[idx]["counts"]
            counts[row["state_type"]] = counts.get(row["state_type"], 0) + 1
        return out

    # -- admin actions -------------------------------------------------------
    def request_cancel(self, run_id: str) -> Optional[dict]:
        """Move a live run to Cancelling — the two-phase commit's first phase."""
        run = self.get_run(run_id)
        if run is None:
            return None
        if run["state_type"] in (t.value for t in st.TERMINAL_STATES):
            return run
        self.record_transition(run_id, st.Cancelling(message="cancellation requested"))
        return self.get_run(run_id)

    def _row_to_run(self, row: Mapping[str, Any]) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "kind": row["kind"],
            "state_type": row["state_type"],
            "state_name": row["state_name"],
            "parameters": _loads(row["parameters"]),
            "result": _loads(row["result"]),
            "error": _loads(row["error"]),
            "parent_run_id": row["parent_run_id"],
            "flow_run_id": row["flow_run_id"],
            "tags": _loads(row["tags"]) or [],
            "retries": row["retries"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
            "is_final": row["state_type"] in (t.value for t in st.TERMINAL_STATES),
            "user_id": row["user_id"],
            "tenant_id": row["tenant_id"],
        }

    @property
    def events(self):
        """The EventLog backing this store — same database, separate connection."""
        return self._event_log

    def close(self) -> None:
        self._heartbeat_stop.set()
        thread, self._heartbeat_thread = self._heartbeat_thread, None
        if thread is not None:
            thread.join(timeout=2)
        try:
            self.release_owned_runs()
        except Exception:
            # A close racing a dropped connection must still close. The stale
            # threshold covers whatever we failed to release.
            pass
        self._db.close()
        if self._event_log is not None and self._owns_event_log:
            self._event_log.close()


_DEFAULT: Optional[RunStore] = None
_DEFAULT_LOCK = threading.Lock()


def get_store() -> RunStore:
    """Process-wide store, created lazily so importing the package is cheap."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = RunStore()
        return _DEFAULT


def set_store(store: Optional[RunStore]) -> None:
    """Swap the store — used by tests and by an embedded deployment."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = store
