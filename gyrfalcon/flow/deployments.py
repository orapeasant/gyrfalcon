"""Deployments — a persisted binding of a flow to parameters and a schedule.

Spec: §9.1.

Scoped deliberately narrow. §9.1 lists `pull_steps` (clone-this-repo-at-this-ref
code delivery) and `work_queue_name`/`job_variables` (routing to a heterogeneous
worker fleet) — both exist to decouple "what to run" from "where the code lives"
across a fleet of machines. Gyrfalcon runs as one process against one codebase;
the flow function is already importable in-process, so there is nothing to pull
and nowhere else to route to. §9.2's own table calls this shape "Runner: dev,
small prod, single box" as distinct from "Worker: heterogeneous fleets" — this
module is the Runner half, on purpose, not a cut corner.

Schedule parsing is **not reimplemented**: `gyrfalcon.scheduler.parse_schedule`
already accepts cron expressions, durations, ISO timestamps, and natural
language, with the same LLM-translation fallback the job scheduler uses. A
second parser here would drift from that one and confuse users maintaining
both. Deployments are stored alongside runs (`flow.db`) since a deployment is
meaningless without the run store that visualizes what it produced.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from gyrfalcon.flow.db import open_database, resolve_target, sql
from gyrfalcon.flow.db.migrations import ensure_schema
from gyrfalcon.flow.db.scope import Scope, current_scope
from gyrfalcon.identity import require_principal


def _internal_scope() -> Scope:
    """Bookkeeping on a deployment already selected by a scoped read.

    `advance_schedule` and `set_paused` are called with an id the caller has
    already been authorized for; re-filtering here would make an operator
    unable to pause someone else's deployment they can legitimately see.
    """
    return Scope.system(reason="schedule bookkeeping on an already-authorized deployment")


class DeploymentStore:
    def __init__(self, db_path=None, backend: Optional[str] = None,
                 dsn: Optional[str] = None):
        self.backend, resolved, self.dsn = resolve_target(db_path, backend, dsn)
        self._db = open_database(backend=self.backend, path=resolved, dsn=self.dsn)
        self.schema_version = ensure_schema(self._db)

    def create(
        self,
        name: str,
        flow_name: str,
        schedule: Optional[str] = None,
        parameters: Optional[dict] = None,
        tags: Optional[list[str]] = None,
        concurrency_limit: Optional[int] = None,
        enforce_parameter_schema: bool = False,
        paused: bool = False,
    ) -> dict:
        """Validates `flow_name` against the registry: a deployment for a flow
        that isn't (yet) imported is a silent no-op waiting to happen — the
        runner would tick forever finding nothing to start."""
        from gyrfalcon.flow.registry import get_definition
        from gyrfalcon.scheduler import next_run_iso, parse_schedule

        if get_definition(flow_name) is None:
            raise ValueError(
                f"No registered flow named {flow_name!r}. A @flow function must be "
                f"imported before a deployment can reference it."
            )

        schedule_dict = parse_schedule(schedule, use_llm=False) if schedule else None
        next_run = next_run_iso(schedule_dict) if schedule_dict and not paused else None

        deployment_id = uuid.uuid4().hex
        now = time.time()
        # The creator owns it. The runner later executes scheduled runs *as*
        # this principal rather than as the platform, so an unattended run is
        # attributable to a person (§17.4).
        who = require_principal()
        with self._db.connect() as conn:
            conn.execute(
                sql.INSERT_DEPLOYMENT,
                (
                    deployment_id, name, flow_name, schedule,
                    json.dumps(schedule_dict) if schedule_dict else None,
                    json.dumps(parameters or {}), json.dumps(tags or []),
                    concurrency_limit, int(enforce_parameter_schema), int(paused),
                    next_run, now, now, who.user_id, who.tenant_id,
                ),
            )
        return self.get(deployment_id)

    def get(self, deployment_id: str, scope: Optional[Scope] = None) -> Optional[dict]:
        stmt, sp = sql.get_deployment(current_scope(scope))
        with self._db.connect() as conn:
            row = conn.fetchone(stmt, (*sp, deployment_id))
        return self._row_to_dict(row) if row else None

    def get_by_name(self, name: str, scope: Optional[Scope] = None) -> Optional[dict]:
        stmt, sp = sql.get_deployment_by_name(current_scope(scope))
        with self._db.connect() as conn:
            row = conn.fetchone(stmt, (*sp, name))
        return self._row_to_dict(row) if row else None

    def list_all(self, scope: Optional[Scope] = None) -> list[dict]:
        stmt, sp = sql.list_deployments(current_scope(scope))
        with self._db.connect() as conn:
            rows = conn.fetchall(stmt, sp)
        return [self._row_to_dict(r) for r in rows]

    def get_due(self, now: Optional[datetime] = None,
                scope: Optional[Scope] = None) -> list[dict]:
        """Enabled deployments whose next_run_at has passed — mirrors the
        scheduler's own get_due_jobs() exactly, deliberately."""
        now = now or datetime.now(timezone.utc).astimezone()
        # The runner ticks for everyone, so this read is deliberately
        # cross-tenant; each claimed deployment is then *executed* as its own
        # owner (§17.4), which is where scoping resumes.
        scope = scope or Scope.system(reason="the runner scans due deployments for all tenants")
        due = []
        for dep in self.list_all(scope):
            if dep["paused"] or not dep["next_run_at"]:
                continue
            try:
                nxt = datetime.fromisoformat(dep["next_run_at"])
                if nxt.tzinfo is None:
                    nxt = nxt.replace(tzinfo=timezone.utc)
                if nxt <= now:
                    due.append(dep)
            except (ValueError, TypeError):
                continue
        return due

    def claim_due(self, now: Optional[datetime] = None,
                  scope: Optional[Scope] = None) -> list[dict]:
        """Due deployments this process has exclusively claimed (§15.6.2).

        `get_due()` followed by "start it" double-fires under two runners: both
        read the same due row and both launch it. The claim is a
        compare-and-swap — advance `next_run_at` only if it still holds the
        value we read — so exactly one racer's UPDATE touches a row and the
        losers see rowcount 0 and skip.

        Chosen over §15.6.2's suggested `FOR UPDATE SKIP LOCKED` because it is
        one statement that behaves identically on both backends, needs no
        dialect branch, and is therefore testable on SQLite instead of only
        under PostgreSQL. Advancing the schedule *is* the claim, so a
        deployment can no longer be advanced twice for one due slot either.
        """
        from gyrfalcon.scheduler import next_run_iso

        claimed = []
        # Only forward an explicit scope: `get_due` already defaults to the
        # cross-tenant runner scope, and passing it unconditionally would make
        # the signature harder to substitute in tests for no gain.
        due = self.get_due(now) if scope is None else self.get_due(now, scope=scope)
        for dep in due:
            previous = dep["next_run_at"]
            following = next_run_iso(dep["schedule"]) if dep["schedule"] else None
            with self._db.connect() as conn:
                if previous is None:
                    cur = conn.execute(
                        sql.CLAIM_UNSCHEDULED_DEPLOYMENT,
                        (following, time.time(), dep["id"]),
                    )
                else:
                    cur = conn.execute(
                        sql.CLAIM_DEPLOYMENT,
                        (following, time.time(), dep["id"], previous),
                    )
                won = cur.rowcount == 1
            if won:
                claimed.append(dep)
        return claimed

    def advance_schedule(self, deployment_id: str) -> None:
        """Compute and persist the next fire time after a run has been started."""
        from gyrfalcon.scheduler import next_run_iso

        dep = self.get(deployment_id, _internal_scope())
        if not dep or not dep["schedule"]:
            return
        next_run = next_run_iso(dep["schedule"])
        with self._db.connect() as conn:
            conn.execute(sql.ADVANCE_SCHEDULE, (next_run, time.time(), deployment_id))

    def set_paused(self, deployment_id: str, paused: bool) -> Optional[dict]:
        from gyrfalcon.scheduler import next_run_iso

        dep = self.get(deployment_id, _internal_scope())
        if not dep:
            return None
        next_run = None
        if not paused and dep["schedule"]:
            next_run = next_run_iso(dep["schedule"])
        with self._db.connect() as conn:
            conn.execute(sql.SET_PAUSED, (int(paused), next_run, time.time(), deployment_id))
        return self.get(deployment_id)

    def update(
        self,
        deployment_id: str,
        name: str,
        schedule: Optional[str] = None,
        parameters: Optional[dict] = None,
        tags: Optional[list[str]] = None,
        concurrency_limit: Optional[int] = None,
    ) -> Optional[dict]:
        """Edit a deployment's binding. Returns None if it no longer exists.

        Recomputes `next_run_at` from the new schedule the same way `create`
        does — an edit that changes the schedule string must not leave the
        old cadence's next-fire time in place. A paused deployment keeps
        `next_run_at` unset regardless, same as `create`.
        """
        from gyrfalcon.scheduler import next_run_iso, parse_schedule

        dep = self.get(deployment_id, _internal_scope())
        if not dep:
            return None

        schedule_dict = parse_schedule(schedule, use_llm=False) if schedule else None
        next_run = next_run_iso(schedule_dict) if schedule_dict and not dep["paused"] else None

        with self._db.connect() as conn:
            conn.execute(
                sql.UPDATE_DEPLOYMENT,
                (
                    name, schedule, json.dumps(schedule_dict) if schedule_dict else None,
                    json.dumps(parameters or {}), json.dumps(tags or []),
                    concurrency_limit, next_run, time.time(), deployment_id,
                ),
            )
        return self.get(deployment_id)

    def delete(self, deployment_id: str) -> bool:
        with self._db.connect() as conn:
            cur = conn.execute(sql.DELETE_DEPLOYMENT, (deployment_id,))
            deleted = cur.rowcount > 0
        return deleted

    def _row_to_dict(self, row: Mapping[str, Any]) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "flow_name": row["flow_name"],
            "schedule_raw": row["schedule_raw"],
            "schedule": json.loads(row["schedule"]) if row["schedule"] else None,
            "parameters": json.loads(row["parameters"]),
            "tags": json.loads(row["tags"]),
            "concurrency_limit": row["concurrency_limit"],
            "enforce_parameter_schema": bool(row["enforce_parameter_schema"]),
            "paused": bool(row["paused"]),
            "next_run_at": row["next_run_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "user_id": row["user_id"],
            "tenant_id": row["tenant_id"],
        }

    def close(self) -> None:
        self._db.close()


_DEFAULT: Optional[DeploymentStore] = None
_DEFAULT_LOCK = threading.Lock()


def get_deployment_store() -> DeploymentStore:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = DeploymentStore()
        return _DEFAULT


def set_deployment_store(store: Optional[DeploymentStore]) -> None:
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = store
