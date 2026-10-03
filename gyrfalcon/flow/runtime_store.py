"""Tenant-scoped persistence for visual flow definitions and runtime visits."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from copy import deepcopy
from typing import Any, Mapping

from sqlalchemy import String, and_, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from gyrfalcon.db.engine import Database, open_database
from gyrfalcon.db.migrations import ensure_schema
from gyrfalcon.db.schema import (
    AUTH_MEMBERSHIP_ATTRIBUTES, AUTH_ORGS, AUTH_USERS,
    FLOW_DT_ATTRS, FLOW_DT_DEFINITIONS, FLOW_DT_DEFINITION_VERSIONS,
    FLOW_DT_DEPLOYMENTS, FLOW_DT_NOTIF, FLOW_RT_ATTRS, FLOW_RT_EVENTS,
    FLOW_RT_NODE_STATUSES, FLOW_RT_NOTIF, FLOW_RT_STATUSES, SESSIONS,
    SESSION_MESSAGES,
)
from gyrfalcon.db.scope import Scope

_UNSET = object()
_TERMINAL = frozenset({"completed", "failed", "cancelled", "crashed"})


def _id() -> str:
    return uuid.uuid4().hex


def _row(result) -> dict[str, Any] | None:
    row = result.mappings().first()
    return dict(row) if row is not None else None


def _hash_input(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class FlowRuntimeStore:
    """One tenant and environment's visual flow data.

    The daemon's cross-tenant queue operations explicitly use Scope.system.
    Other methods always include tenant and environment in their predicates.
    Pass an existing Database to share its transaction with session writers.
    """

    def __init__(self, db: Database | None = None, tenant_id: str = "local",
                 environment_id: str = "default", *, migrate: bool = False):
        if not tenant_id or not environment_id:
            raise ValueError("tenant_id and environment_id are required")
        self.db = db or open_database()
        self._owns_db = db is None
        self.tenant_id = tenant_id
        self.environment_id = environment_id
        if migrate:
            ensure_schema(self.db)

    def close(self) -> None:
        if self._owns_db:
            self.db.close()

    def _scope(self, table):
        return and_(table.c.tenant_id == self.tenant_id,
                    table.c.environment_id == self.environment_id)

    def _tenant(self, table):
        return table.c.tenant_id == self.tenant_id

    # Design documents -----------------------------------------------------

    def create_definition(self, name: str, draft: dict, user_id: str, *,
                          definition_id: str | None = None,
                          enabled: bool = True) -> dict:
        now = time.time()
        values = dict(id=definition_id or _id(), tenant_id=self.tenant_id,
                      user_id=user_id, name=name, enabled=enabled, draft=draft, created_at=now,
                      updated_at=now)
        with self.db.connect() as conn:
            conn.connection.execute(insert(FLOW_DT_DEFINITIONS).values(**values))
        return values

    def get_definition(self, definition_id: str) -> dict | None:
        t = FLOW_DT_DEFINITIONS
        with self.db.connect() as conn:
            return _row(conn.connection.execute(select(t).where(
                self._tenant(t), t.c.id == definition_id)))

    def list_definitions(self) -> list[dict]:
        t = FLOW_DT_DEFINITIONS
        with self.db.connect() as conn:
            rows = conn.connection.execute(select(t).where(self._tenant(t)).order_by(t.c.name))
            return [dict(r) for r in rows.mappings()]

    def save_draft(self, definition_id: str, draft: dict, *, name: str | None = None) -> dict:
        t = FLOW_DT_DEFINITIONS
        changes = dict(draft=draft, updated_at=time.time())
        if name is not None:
            changes["name"] = name
        with self.db.connect() as conn:
            row = _row(conn.connection.execute(update(t).where(
                self._tenant(t), t.c.id == definition_id).values(**changes).returning(t)))
        if row is None:
            raise KeyError(definition_id)
        return row

    def rename_definition(self, definition_id: str, name: str) -> dict:
        """Change a flow's display name while keeping its stable ID."""
        t = FLOW_DT_DEFINITIONS
        with self.db.connect() as conn:
            row = _row(conn.connection.execute(update(t).where(
                self._tenant(t), t.c.id == definition_id).values(
                    name=name, updated_at=time.time()).returning(t)))
        if row is None:
            raise KeyError(definition_id)
        return row

    def set_definition_enabled(self, definition_id: str, enabled: bool) -> dict:
        t = FLOW_DT_DEFINITIONS
        with self.db.connect() as conn:
            row = _row(conn.connection.execute(update(t).where(
                self._tenant(t), t.c.id == definition_id).values(
                    enabled=enabled, updated_at=time.time()).returning(t)))
        if row is None:
            raise KeyError(definition_id)
        return row

    def delete_definition(self, definition_id: str) -> bool:
        """Delete an unused draft; published versions or deployments restrict it."""
        t = FLOW_DT_DEFINITIONS
        with self.db.connect() as conn:
            published = conn.connection.execute(select(func.count()).select_from(
                FLOW_DT_DEFINITION_VERSIONS).where(
                    FLOW_DT_DEFINITION_VERSIONS.c.tenant_id == self.tenant_id,
                    FLOW_DT_DEFINITION_VERSIONS.c.definition_id == definition_id)).scalar_one()
            deployed = conn.connection.execute(select(func.count()).select_from(
                FLOW_DT_DEPLOYMENTS).where(
                    FLOW_DT_DEPLOYMENTS.c.tenant_id == self.tenant_id,
                    FLOW_DT_DEPLOYMENTS.c.definition_id == definition_id)).scalar_one()
            if published or deployed:
                raise ValueError("Published or deployed flows cannot be deleted")
            conn.connection.execute(FLOW_DT_ATTRS.delete().where(
                FLOW_DT_ATTRS.c.tenant_id == self.tenant_id,
                FLOW_DT_ATTRS.c.definition_id == definition_id))
            result = conn.connection.execute(t.delete().where(self._tenant(t),
                t.c.id == definition_id))
            return result.rowcount == 1

    def replace_draft_attrs(self, definition_id: str, attrs: list[dict]) -> None:
        t = FLOW_DT_ATTRS
        with self.db.connect() as conn:
            if self.get_definition(definition_id) is None:
                raise KeyError(definition_id)
            conn.connection.execute(t.delete().where(self._tenant(t), t.c.definition_id == definition_id))
            if attrs:
                conn.connection.execute(insert(t), [dict(tenant_id=self.tenant_id,
                    definition_id=definition_id, **attr) for attr in attrs])

    def list_draft_attrs(self, definition_id: str) -> list[dict]:
        t = FLOW_DT_ATTRS
        with self.db.connect() as conn:
            rows = conn.connection.execute(select(t).where(self._tenant(t),
                t.c.definition_id == definition_id).order_by(t.c.scope_path, t.c.name))
            return [dict(r) for r in rows.mappings()]

    def publish_definition(self, definition_id: str, graph: dict,
                           attribute_snapshot: Any, notification_snapshot: Any,
                           published_by: str) -> dict:
        t, d = FLOW_DT_DEFINITION_VERSIONS, FLOW_DT_DEFINITIONS
        now = time.time()
        with self.db.connect() as conn:
            parent = _row(conn.connection.execute(select(d.c.id).where(
                self._tenant(d), d.c.id == definition_id).with_for_update()))
            if parent is None:
                raise KeyError(definition_id)
            version = conn.connection.execute(select(func.coalesce(func.max(t.c.version), 0) + 1).where(
                self._tenant(t), t.c.definition_id == definition_id)).scalar_one()
            values = dict(tenant_id=self.tenant_id, definition_id=definition_id,
                          version=version, graph=graph,
                          attribute_snapshot=attribute_snapshot,
                          notification_snapshot=notification_snapshot,
                          published_at=now, published_by=published_by)
            conn.connection.execute(insert(t).values(**values))
            conn.connection.execute(update(d).where(self._tenant(d),
                d.c.id == definition_id).values(published=True, updated_at=now))
            return values

    def unpublish_definition(self, definition_id: str) -> dict | None:
        t = FLOW_DT_DEFINITIONS
        with self.db.connect() as conn:
            return _row(conn.connection.execute(update(t).where(
                self._tenant(t), t.c.id == definition_id).values(
                    published=False, updated_at=time.time()).returning(t)))

    def get_version(self, definition_id: str, version: int) -> dict | None:
        t = FLOW_DT_DEFINITION_VERSIONS
        with self.db.connect() as conn:
            return _row(conn.connection.execute(select(t).where(self._tenant(t),
                t.c.definition_id == definition_id, t.c.version == version)))

    def list_versions(self, definition_id: str) -> list[dict]:
        t = FLOW_DT_DEFINITION_VERSIONS
        with self.db.connect() as conn:
            rows = conn.connection.execute(select(t).where(self._tenant(t),
                t.c.definition_id == definition_id).order_by(t.c.version.desc()))
            return [dict(r) for r in rows.mappings()]

    def create_notification_template(self, name: str, channel: str,
                                     recipient_kind: str, recipient_ref: str,
                                     user_id: str, **fields) -> dict:
        now = time.time()
        values = dict(id=_id(), tenant_id=self.tenant_id, user_id=user_id,
                      name=name, channel=channel, recipient_kind=recipient_kind,
                      recipient_ref=recipient_ref, config={}, created_at=now,
                      updated_at=now, **fields)
        with self.db.connect() as conn:
            conn.connection.execute(insert(FLOW_DT_NOTIF).values(**values))
        return values

    def get_notification_template(self, template_id: str) -> dict | None:
        t = FLOW_DT_NOTIF
        with self.db.connect() as conn:
            return _row(conn.connection.execute(select(t).where(self._tenant(t), t.c.id == template_id)))

    def list_notification_templates(self) -> list[dict]:
        t = FLOW_DT_NOTIF
        with self.db.connect() as conn:
            rows = conn.connection.execute(select(t).where(self._tenant(t)).order_by(t.c.name))
            return [dict(r) for r in rows.mappings()]

    def update_notification_template(self, template_id: str, **fields) -> dict:
        allowed = {"name", "channel", "recipient_kind", "recipient_ref",
                   "agent_id", "subject_template", "body_template", "config"}
        if not fields or set(fields) - allowed:
            raise ValueError("invalid notification template fields")
        t = FLOW_DT_NOTIF
        fields["updated_at"] = time.time()
        with self.db.connect() as conn:
            row = _row(conn.connection.execute(update(t).where(self._tenant(t),
                t.c.id == template_id).values(**fields).returning(t)))
        if row is None:
            raise KeyError(template_id)
        return row

    def delete_notification_template(self, template_id: str) -> bool:
        t = FLOW_DT_NOTIF
        with self.db.connect() as conn:
            result = conn.connection.execute(t.delete().where(self._tenant(t),
                t.c.id == template_id))
            return result.rowcount == 1

    def create_deployment(self, *, name: str, short_name: str, definition_id: str,
                          version: int, user_id: str, schedule: str | None = None,
                          input_schema: dict | None = None, parameters: dict | None = None,
                          allowed_service_account_ids: list[str] | None = None,
                          paused: bool = False, next_run_at: float | None = None) -> dict:
        t = FLOW_DT_DEPLOYMENTS
        now = time.time()
        values = dict(id=_id(), tenant_id=self.tenant_id, user_id=user_id,
                      name=name, short_name=short_name, definition_id=definition_id,
                      version=version, schedule=schedule, input_schema=input_schema or {},
                      parameters=parameters or {},
                      allowed_service_account_ids=allowed_service_account_ids or [],
                      paused=int(paused), next_run_at=next_run_at,
                      created_at=now, updated_at=now)
        with self.db.connect() as conn:
            conn.connection.execute(insert(t).values(**values))
        return values

    def get_deployment(self, deployment_id: str | None = None, *,
                       short_name: str | None = None) -> dict | None:
        if (deployment_id is None) == (short_name is None):
            raise ValueError("provide exactly one deployment_id or short_name")
        t = FLOW_DT_DEPLOYMENTS
        criterion = t.c.id == deployment_id if deployment_id is not None else t.c.short_name == short_name
        with self.db.connect() as conn:
            return _row(conn.connection.execute(select(t).where(self._tenant(t), criterion)))

    def list_deployments(self) -> list[dict]:
        t = FLOW_DT_DEPLOYMENTS
        with self.db.connect() as conn:
            rows = conn.connection.execute(select(t).where(self._tenant(t)).order_by(t.c.name))
            return [dict(r) for r in rows.mappings()]

    def update_deployment(self, deployment_id: str, **fields) -> dict:
        allowed = {"name", "short_name", "definition_id", "version", "schedule",
                   "input_schema", "parameters", "allowed_service_account_ids",
                   "paused", "next_run_at"}
        if not fields or set(fields) - allowed:
            raise ValueError("invalid deployment fields")
        t = FLOW_DT_DEPLOYMENTS
        fields["updated_at"] = time.time()
        with self.db.connect() as conn:
            row = _row(conn.connection.execute(update(t).where(self._tenant(t),
                t.c.id == deployment_id).values(**fields).returning(t)))
        if row is None:
            raise KeyError(deployment_id)
        return row

    def delete_deployment(self, deployment_id: str) -> bool:
        """Delete a deployment while preserving historical runs.

        Runs keep their deployment id in ``trigger_ref`` for audit/display, but
        the optional FK column must be cleared before removing the deployment.
        """
        t = FLOW_DT_DEPLOYMENTS
        with self.db.connect() as conn:
            conn.connection.execute(update(FLOW_RT_STATUSES).where(
                FLOW_RT_STATUSES.c.tenant_id == self.tenant_id,
                FLOW_RT_STATUSES.c.deployment_id == deployment_id,
            ).values(deployment_id=None))
            result = conn.connection.execute(t.delete().where(self._tenant(t),
                t.c.id == deployment_id))
            return result.rowcount == 1

    def list_due_deployments(self, now: float | None = None) -> list[dict]:
        # A daemon may scan every tenant. Scope.system makes that intentional.
        Scope.system(reason="daemon scans due visual flow deployments across tenants")
        t = FLOW_DT_DEPLOYMENTS
        due = time.time() if now is None else now
        with self.db.connect() as conn:
            rows = conn.connection.execute(select(t).where(t.c.paused == 0,
                t.c.next_run_at <= due).order_by(t.c.next_run_at))
            return [dict(r) for r in rows.mappings()]

    def claim_due_deployment(self, deployment_id: str, due_at: float,
                             next_run_at: float | None) -> dict | None:
        t = FLOW_DT_DEPLOYMENTS
        with self.db.connect() as conn:
            return _row(conn.connection.execute(update(t).where(self._tenant(t),
                t.c.id == deployment_id, t.c.paused == 0,
                t.c.next_run_at == due_at).values(next_run_at=next_run_at,
                paused=int(next_run_at is None), updated_at=time.time()).returning(t)))

    # Runtime status and work queue --------------------------------------

    def create_run(self, definition_id: str, version: int, inputs: Any,
                   trigger: str, *, user_id: str = "local", deployment_id: str | None = None,
                   caller_key: str | None = None, trigger_ref: str | None = None,
                   run_id: str | None = None) -> dict:
        definition = self.get_definition(definition_id)
        if definition is None:
            raise KeyError(definition_id)
        if not definition.get("enabled", True):
            raise ValueError("This flow is disabled")
        if not definition.get("published", False):
            raise ValueError("This flow is unpublished")
        t = FLOW_RT_STATUSES
        now = time.time()
        digest = _hash_input(inputs)
        values = dict(id=run_id or _id(), tenant_id=self.tenant_id,
                      environment_id=self.environment_id, user_id=user_id,
                      definition_id=definition_id, version=version,
                      deployment_id=deployment_id, trigger_kind=trigger,
                      trigger_ref=trigger_ref, caller_key=caller_key,
                      input_hash=digest, parameters=inputs, state="queued",
                      context={}, loop_state={}, created_at=now, updated_at=now)
        with self.db.connect() as conn:
            if caller_key is None:
                conn.connection.execute(insert(t).values(**values))
            else:
                stmt = pg_insert(t).values(**values).on_conflict_do_nothing(
                    index_elements=[t.c.tenant_id, t.c.caller_key]).returning(t)
                created = _row(conn.connection.execute(stmt))
                if created is not None:
                    return created
                prior = _row(conn.connection.execute(select(t).where(
                    self._tenant(t), t.c.caller_key == caller_key)))
                if prior is None:
                    raise RuntimeError("idempotency conflict could not be resolved")
                if (prior["input_hash"] != digest or prior["definition_id"] != definition_id
                    or prior["version"] != version or prior["deployment_id"] != deployment_id):
                    raise ValueError("idempotency key already used with different invocation")
                return prior
        return values

    def get_run(self, run_id: str) -> dict | None:
        t = FLOW_RT_STATUSES
        with self.db.connect() as conn:
            return _row(conn.connection.execute(select(t).where(self._scope(t), t.c.id == run_id)))

    def list_runs(self, *, limit: int = 100, state: str | None = None) -> list[dict]:
        t = FLOW_RT_STATUSES
        stmt = select(t).where(self._scope(t)).order_by(t.c.created_at.desc()).limit(limit)
        if state is not None:
            stmt = stmt.where(t.c.state == state)
        with self.db.connect() as conn:
            return [dict(r) for r in conn.connection.execute(stmt).mappings()]

    def list_run_page(self, *, limit: int = 100, offset: int = 0,
                      state: str | None = None, user_id: str | None = None,
                      search: str | None = None) -> tuple[list[dict], int]:
        """Page through runs with readable flow, user, organization, and BU labels."""
        t, d, u, o, a = FLOW_RT_STATUSES, FLOW_DT_DEFINITIONS, AUTH_USERS, AUTH_ORGS, AUTH_MEMBERSHIP_ATTRIBUTES
        source = t.outerjoin(d, and_(d.c.tenant_id == t.c.tenant_id,
                                     d.c.id == t.c.definition_id))
        source = source.outerjoin(u, u.c.id == t.c.user_id)
        source = source.outerjoin(o, o.c.id == t.c.tenant_id)
        source = source.outerjoin(a, and_(a.c.tenant_id == t.c.tenant_id,
                                           a.c.user_id == t.c.user_id))
        criteria = [self._scope(t)]
        if state:
            criteria.append(t.c.state == state)
        if user_id:
            criteria.append(t.c.user_id == user_id)
        if search and search.strip():
            pattern = f"%{search.strip()}%"
            criteria.append(or_(t.c.id.ilike(pattern), t.c.state.ilike(pattern),
                d.c.name.ilike(pattern), u.c.display_name.ilike(pattern),
                u.c.email.ilike(pattern), o.c.name.ilike(pattern),
                a.c.business_unit.ilike(pattern), t.c.parameters.cast(String).ilike(pattern)))
        with self.db.connect() as conn:
            total = conn.connection.execute(select(func.count()).select_from(source)
                .where(*criteria)).scalar_one()
            rows = conn.connection.execute(select(t, d.c.name.label("flow_name"),
                u.c.display_name.label("user_name"), u.c.email.label("user_email"),
                o.c.name.label("organization_name"),
                a.c.business_unit.label("business_unit")).select_from(source).where(*criteria)
                .order_by(t.c.created_at.desc()).limit(max(1, min(limit, 500)))
                .offset(max(0, offset)))
            return [dict(row) for row in rows.mappings()], int(total)

    def retry_run(self, run_id: str) -> dict | None:
        """Requeue a failed run from its persisted failed-node execution frame."""
        r, n = FLOW_RT_STATUSES, FLOW_RT_NODE_STATUSES
        now = time.time()
        with self.db.connect() as conn:
            run = _row(conn.connection.execute(select(r).where(self._scope(r),
                r.c.id == run_id).with_for_update()))
            if run is None or run["state"] not in {"failed", "crashed"}:
                return None
            frames = (run.get("context") or {}).get("frames") or []
            failed_path = run.get("current_node_path")
            failed_visit = _row(conn.connection.execute(select(n).where(
                self._scope(n), n.c.run_id == run_id, n.c.node_path == failed_path,
                n.c.state.in_(("failed", "crashed"))).order_by(n.c.visit_seq.desc()).limit(1)))
            if not frames or failed_visit is None:
                raise ValueError("This run has no saved failed node to retry")
            checkpoint = failed_visit.get("context_snapshot") or {}
            restored_context = deepcopy(checkpoint.get("run_context", run.get("context") or {}))
            restored_loop_state = checkpoint.get("loop_state", run.get("loop_state") or {})
            if failed_visit.get("context_snapshot"):
                restored_context["_retry_checkpoint"] = {
                    "node_path": failed_path,
                    "activity_context": {
                        key: deepcopy(checkpoint.get(key))
                        for key in ("incoming_value", "inputs", "outputs",
                                    "flow_attributes", "attributes")
                    },
                }
            return _row(conn.connection.execute(update(r).where(self._scope(r),
                r.c.id == run_id, r.c.state.in_(("failed", "crashed"))).values(
                    state="queued", error=None, result=None, finished_at=None,
                    context=restored_context, loop_state=restored_loop_state,
                    lease_owner=None, lease_until=None, updated_at=now).returning(r)))

    def delete_run(self, run_id: str) -> bool | None:
        """Delete a terminal run and trajectory while retaining detached agent chats."""
        r, n = FLOW_RT_STATUSES, FLOW_RT_NODE_STATUSES
        with self.db.connect() as conn:
            run = _row(conn.connection.execute(select(r).where(self._scope(r),
                r.c.id == run_id).with_for_update()))
            if run is None:
                return None
            if run["state"] not in _TERMINAL:
                return False
            visit_ids = select(n.c.id).where(self._scope(n), n.c.run_id == run_id)
            conn.connection.execute(FLOW_RT_NOTIF.delete().where(self._scope(FLOW_RT_NOTIF),
                FLOW_RT_NOTIF.c.run_id == run_id))
            conn.connection.execute(update(SESSION_MESSAGES).where(
                SESSION_MESSAGES.c.tenant_id == self.tenant_id,
                SESSION_MESSAGES.c.environment_id == self.environment_id,
                SESSION_MESSAGES.c.flow_node_status_id.in_(visit_ids)).values(flow_node_status_id=None))
            conn.connection.execute(update(SESSIONS).where(
                SESSIONS.c.tenant_id == self.tenant_id,
                SESSIONS.c.environment_id == self.environment_id,
                SESSIONS.c.flow_run_status_id == run_id).values(
                    flow_run_status_id=None, flow_node_status_id=None))
            conn.connection.execute(FLOW_RT_EVENTS.delete().where(self._scope(FLOW_RT_EVENTS),
                FLOW_RT_EVENTS.c.run_id == run_id))
            conn.connection.execute(FLOW_RT_ATTRS.delete().where(self._scope(FLOW_RT_ATTRS),
                FLOW_RT_ATTRS.c.run_id == run_id))
            conn.connection.execute(n.delete().where(self._scope(n), n.c.run_id == run_id))
            conn.connection.execute(r.delete().where(self._scope(r), r.c.id == run_id))
            return True

    def list_claimable_runs(self, *, limit: int = 100) -> list[dict]:
        t = FLOW_RT_STATUSES
        now = time.time()
        with self.db.connect() as conn:
            rows = conn.connection.execute(select(t).where(self._scope(t),
                t.c.state == "queued", or_(t.c.lease_until.is_(None),
                t.c.lease_until < now)).order_by(t.c.created_at).limit(limit))
            return [dict(r) for r in rows.mappings()]

    def claim_run(self, run_id: str, worker_id: str, *, lease_seconds: float = 90) -> dict | None:
        t = FLOW_RT_STATUSES
        now = time.time()
        with self.db.connect() as conn:
            return _row(conn.connection.execute(update(t).where(self._scope(t),
                t.c.id == run_id, t.c.state == "queued",
                or_(t.c.lease_until.is_(None), t.c.lease_until < now))
                .values(state="running", lease_owner=worker_id,
                        lease_until=now + lease_seconds, started_at=func.coalesce(t.c.started_at, now),
                        updated_at=now).returning(t)))

    def claim_next_run(self, worker_id: str, *, lease_seconds: float = 90,
                       environment_id: str | None = None) -> dict | None:
        Scope.system(reason="daemon atomically claims queued visual flow work across tenants")
        t = FLOW_RT_STATUSES
        now = time.time()
        with self.db.connect() as conn:
            criteria = [t.c.state == "queued",
                        or_(t.c.lease_until.is_(None), t.c.lease_until < now)]
            if environment_id is not None:
                criteria.append(t.c.environment_id == environment_id)
            candidate = _row(conn.connection.execute(select(t).where(*criteria)
                .order_by(t.c.created_at).with_for_update(skip_locked=True).limit(1)))
            if candidate is None:
                return None
            return _row(conn.connection.execute(update(t).where(t.c.id == candidate["id"])
                .values(state="running", lease_owner=worker_id,
                        lease_until=now + lease_seconds,
                        started_at=func.coalesce(t.c.started_at, now),
                        updated_at=now).returning(t)))

    def renew_lease(self, run_id: str, worker_id: str, *, lease_seconds: float = 90) -> bool:
        t = FLOW_RT_STATUSES
        now = time.time()
        with self.db.connect() as conn:
            result = conn.connection.execute(update(t).where(self._scope(t),
                t.c.id == run_id, t.c.lease_owner == worker_id,
                t.c.state == "running").values(lease_until=now + lease_seconds,
                updated_at=now))
            return result.rowcount == 1

    def update_run(self, run_id: str, *, state: str | object = _UNSET,
                   context: Any = _UNSET, loop_state: Any = _UNSET,
                   current_node_path: str | None | object = _UNSET,
                   result: Any = _UNSET, error: Any = _UNSET,
                   lease_owner: str | None | object = _UNSET,
                   lease_until: float | None | object = _UNSET) -> dict:
        t = FLOW_RT_STATUSES
        changes = {key: value for key, value in locals().items()
                   if key in {"state", "context", "loop_state", "current_node_path",
                              "result", "error", "lease_owner", "lease_until"}
                   and value is not _UNSET}
        if state in _TERMINAL:
            changes["finished_at"] = time.time()
            changes.setdefault("lease_owner", None)
            changes.setdefault("lease_until", None)
        changes["updated_at"] = time.time()
        with self.db.connect() as conn:
            row = _row(conn.connection.execute(update(t).where(self._scope(t),
                t.c.id == run_id).values(**changes).returning(t)))
        if row is None:
            raise KeyError(run_id)
        return row

    def mark_crashed(self, run_id: str, error: Any) -> dict | None:
        t, n = FLOW_RT_STATUSES, FLOW_RT_NODE_STATUSES
        now = time.time()
        with self.db.connect() as conn:
            run = _row(conn.connection.execute(update(t).where(self._scope(t),
                t.c.id == run_id, t.c.state == "running")
                .values(state="crashed", error=error, lease_owner=None,
                        lease_until=None, finished_at=now, updated_at=now).returning(t)))
            if run is not None:
                conn.connection.execute(update(n).where(self._scope(n),
                    n.c.run_id == run_id, n.c.state == "running")
                    .values(state="crashed", error=error,
                            finished_at=now, updated_at=now))
                conn.connection.execute(update(SESSIONS).where(
                    SESSIONS.c.tenant_id == self.tenant_id,
                    SESSIONS.c.environment_id == self.environment_id,
                    SESSIONS.c.flow_run_status_id == run_id,
                    SESSIONS.c.run_status.in_(("created", "queued", "running")),
                ).values(run_status="crashed", run_error=json.dumps(error),
                         ended_at=now, last_active=now))
            return run

    def list_stale_running(self, now: float | None = None) -> list[dict]:
        Scope.system(reason="daemon reconciles expired visual flow worker leases across tenants")
        t = FLOW_RT_STATUSES
        cutoff = time.time() if now is None else now
        with self.db.connect() as conn:
            rows = conn.connection.execute(select(t).where(t.c.state == "running",
                or_(t.c.lease_until.is_(None), t.c.lease_until < cutoff))
                .order_by(t.c.lease_until))
            return [dict(r) for r in rows.mappings()]

    def list_expired_waits(self, now: float | None = None) -> list[dict]:
        Scope.system(reason="daemon finds expired notification waits across tenants")
        n, r = FLOW_RT_NODE_STATUSES, FLOW_RT_STATUSES
        due = time.time() if now is None else now
        with self.db.connect() as conn:
            rows = conn.connection.execute(select(n).join(r, and_(
                r.c.tenant_id == n.c.tenant_id,
                r.c.environment_id == n.c.environment_id,
                r.c.id == n.c.run_id)).where(n.c.state == "waiting",
                r.c.state == "waiting", n.c.wait_deadline <= due)
                .order_by(n.c.wait_deadline))
            return [dict(r) for r in rows.mappings()]

    # Visits, events, attributes, delivery --------------------------------

    def create_node_visit(self, run_id: str, node_path: str, node_id: str,
                          node_kind: str, *, input_value: Any = None,
                          context_snapshot: Any = None,
                          state: str = "running", attempt: int | None = None,
                          visit_id: str | None = None) -> dict:
        n, r = FLOW_RT_NODE_STATUSES, FLOW_RT_STATUSES
        now = time.time()
        with self.db.connect() as conn:
            parent = _row(conn.connection.execute(select(r.c.id).where(self._scope(r),
                r.c.id == run_id).with_for_update()))
            if parent is None:
                raise KeyError(run_id)
            seq = conn.connection.execute(select(func.coalesce(func.max(n.c.visit_seq), 0) + 1).where(
                self._scope(n), n.c.run_id == run_id)).scalar_one()
            if attempt is None:
                previous = _row(conn.connection.execute(select(n.c.attempt, n.c.state).where(
                    self._scope(n), n.c.run_id == run_id, n.c.node_path == node_path)
                    .order_by(n.c.visit_seq.desc()).limit(1)))
                attempt = previous["attempt"] + 1 if previous and previous["state"] in {"failed", "crashed"} else 1
            values = dict(id=visit_id or _id(), tenant_id=self.tenant_id,
                          environment_id=self.environment_id, run_id=run_id,
                          node_path=node_path, node_id=node_id, node_kind=node_kind,
                          visit_seq=seq, attempt=attempt, state=state,
                          context_snapshot=context_snapshot,
                          input_value=input_value, started_at=now, updated_at=now)
            conn.connection.execute(insert(n).values(**values))
            return values

    def update_node_visit(self, visit_id: str, **fields) -> dict:
        allowed = {"state", "input_value", "context_snapshot", "output_value", "transient", "error", "wait_deadline",
                   "timeout_transient",
                   "response_value", "response_user_id", "ai_session_id",
                   "attempt"}
        if not fields or set(fields) - allowed:
            raise ValueError("invalid node visit fields")
        n = FLOW_RT_NODE_STATUSES
        if fields.get("state") in _TERMINAL:
            fields["finished_at"] = time.time()
        fields["updated_at"] = time.time()
        with self.db.connect() as conn:
            row = _row(conn.connection.execute(update(n).where(self._scope(n),
                n.c.id == visit_id).values(**fields).returning(n)))
        if row is None:
            raise KeyError(visit_id)
        return row

    def list_node_visits(self, run_id: str) -> list[dict]:
        n = FLOW_RT_NODE_STATUSES
        with self.db.connect() as conn:
            rows = conn.connection.execute(select(n).where(self._scope(n),
                n.c.run_id == run_id).order_by(n.c.visit_seq))
            return [dict(r) for r in rows.mappings()]

    def create_visit_session(self, run_id: str, visit_id: str, source: str,
                             agent_id: str | None = None,
                             model: str | None = None) -> str:
        """Create a session and set the visit's reciprocal ID atomically."""
        n, r, s = FLOW_RT_NODE_STATUSES, FLOW_RT_STATUSES, SESSIONS
        now = time.time()
        with self.db.connect() as conn:
            visit = _row(conn.connection.execute(select(n).where(self._scope(n),
                n.c.id == visit_id, n.c.run_id == run_id).with_for_update()))
            if visit is None:
                raise KeyError(visit_id)
            if visit["ai_session_id"] is not None:
                return visit["ai_session_id"]
            run = _row(conn.connection.execute(select(r.c.user_id).where(self._scope(r),
                r.c.id == run_id)))
            if run is None:
                raise KeyError(run_id)
            session_id = _id()
            conn.connection.execute(insert(s).values(
                id=session_id, source=source, agent_id=agent_id, model=model,
                started_at=now, last_active=now, user_id=run["user_id"],
                tenant_id=self.tenant_id, environment_id=self.environment_id,
                owner_user_id=run["user_id"],
                flow_run_status_id=run_id, flow_node_status_id=visit_id,
                run_status="created",
            ))
            conn.connection.execute(update(n).where(self._scope(n),
                n.c.id == visit_id).values(ai_session_id=session_id,
                updated_at=now))
            return session_id

    def update_visit_session(self, run_id: str, visit_id: str, state: str,
                             error: Any = None) -> None:
        now = time.time()
        with self.db.connect() as conn:
            stmt = update(SESSIONS).where(
                SESSIONS.c.tenant_id == self.tenant_id,
                SESSIONS.c.environment_id == self.environment_id,
                SESSIONS.c.flow_run_status_id == run_id,
                SESSIONS.c.flow_node_status_id == visit_id,
            ).values(run_status=state,
                     run_error=json.dumps(error) if error is not None else None,
                     ended_at=now if state in _TERMINAL else None,
                     last_active=now)
            conn.connection.execute(stmt)

    def get_waiting_visit(self, run_id: str) -> dict | None:
        n = FLOW_RT_NODE_STATUSES
        with self.db.connect() as conn:
            return _row(conn.connection.execute(select(n).where(self._scope(n),
                n.c.run_id == run_id, n.c.state == "waiting")
                .order_by(n.c.visit_seq.desc()).limit(1)))

    def accept_response(self, visit_id: str, value: Any, responder_user_id: str,
                        transient: str) -> dict | None:
        """First valid responder wins. Caller must authorize recipient membership."""
        n, r = FLOW_RT_NODE_STATUSES, FLOW_RT_STATUSES
        now = time.time()
        with self.db.connect() as conn:
            visit = _row(conn.connection.execute(update(n).where(self._scope(n),
                n.c.id == visit_id, n.c.state == "waiting",
                or_(n.c.wait_deadline.is_(None), n.c.wait_deadline > now))
                .values(state="completed", response_value=value,
                        response_user_id=responder_user_id, transient=transient,
                        updated_at=now, finished_at=now).returning(n)))
            if visit is None:
                return None
            run_update = conn.connection.execute(update(r).where(self._scope(r),
                r.c.id == visit["run_id"], r.c.state == "waiting")
                .values(state="queued", updated_at=now))
            if run_update.rowcount != 1:
                raise RuntimeError("waiting visit has no waiting run")
            conn.connection.execute(update(SESSIONS).where(
                SESSIONS.c.tenant_id == self.tenant_id,
                SESSIONS.c.environment_id == self.environment_id,
                SESSIONS.c.flow_run_status_id == visit["run_id"],
                SESSIONS.c.flow_node_status_id == visit_id,
            ).values(run_status="queued", ended_at=None, last_active=now))
            return visit

    def accept_agent_reply(self, visit_id: str, value: Any,
                           responder_user_id: str) -> dict | None:
        """Resume an agent Activity with a human turn; the agent chooses output later."""
        n, r = FLOW_RT_NODE_STATUSES, FLOW_RT_STATUSES
        now = time.time()
        with self.db.connect() as conn:
            visit = _row(conn.connection.execute(update(n).where(self._scope(n),
                n.c.id == visit_id, n.c.node_kind.in_(("python", "agent", "a2a")),
                n.c.state == "waiting")
                .values(state="completed", response_value=value,
                        response_user_id=responder_user_id,
                        updated_at=now, finished_at=now).returning(n)))
            if visit is None:
                return None
            changed = conn.connection.execute(update(r).where(self._scope(r),
                r.c.id == visit["run_id"], r.c.state == "waiting")
                .values(state="queued", updated_at=now))
            if changed.rowcount != 1:
                raise RuntimeError("waiting agent visit has no waiting run")
            conn.connection.execute(update(SESSIONS).where(
                SESSIONS.c.tenant_id == self.tenant_id,
                SESSIONS.c.environment_id == self.environment_id,
                SESSIONS.c.flow_run_status_id == visit["run_id"],
                SESSIONS.c.flow_node_status_id == visit_id,
            ).values(run_status="queued", ended_at=None, last_active=now))
            return visit

    def expire_wait(self, visit_id: str, timeout_transient: str) -> dict | None:
        n, r = FLOW_RT_NODE_STATUSES, FLOW_RT_STATUSES
        now = time.time()
        with self.db.connect() as conn:
            visit = _row(conn.connection.execute(update(n).where(self._scope(n),
                n.c.id == visit_id, n.c.state == "waiting", n.c.wait_deadline <= now)
                .values(state="completed", transient=timeout_transient,
                        updated_at=now, finished_at=now).returning(n)))
            if visit is None:
                return None
            run_update = conn.connection.execute(update(r).where(self._scope(r),
                r.c.id == visit["run_id"], r.c.state == "waiting")
                .values(state="queued", updated_at=now))
            if run_update.rowcount != 1:
                raise RuntimeError("waiting visit has no waiting run")
            conn.connection.execute(update(SESSIONS).where(
                SESSIONS.c.tenant_id == self.tenant_id,
                SESSIONS.c.environment_id == self.environment_id,
                SESSIONS.c.flow_run_status_id == visit["run_id"],
                SESSIONS.c.flow_node_status_id == visit_id,
            ).values(run_status="queued", ended_at=None, last_active=now))
            return visit

    def record_event(self, run_id: str, event_type: str, payload: Any = None,
                     *, visit_id: str | None = None) -> dict:
        e = FLOW_RT_EVENTS
        values = dict(id=_id(), tenant_id=self.tenant_id,
                      environment_id=self.environment_id, run_id=run_id,
                      visit_id=visit_id, event_type=event_type,
                      payload=payload or {}, created_at=time.time())
        with self.db.connect() as conn:
            conn.connection.execute(insert(e).values(**values))
        return values

    def list_events(self, run_id: str) -> list[dict]:
        e = FLOW_RT_EVENTS
        with self.db.connect() as conn:
            rows = conn.connection.execute(select(e).where(self._scope(e),
                e.c.run_id == run_id).order_by(e.c.created_at, e.c.id))
            return [dict(r) for r in rows.mappings()]

    def list_recent_events(self, *, limit: int = 100, offset: int = 0,
                           event_type: str | None = None) -> tuple[list[dict], int]:
        """Page through this tenant's visual runtime events."""
        e = FLOW_RT_EVENTS
        criteria = [self._scope(e)]
        if event_type:
            criteria.append(e.c.event_type.ilike(f"%{event_type}%"))
        with self.db.connect() as conn:
            total = conn.connection.execute(select(func.count()).select_from(e)
                .where(*criteria)).scalar_one()
            rows = conn.connection.execute(select(e).where(*criteria)
                .order_by(e.c.created_at.desc(), e.c.id.desc())
                .limit(max(1, min(limit, 500))).offset(max(0, offset)))
            return [dict(row) for row in rows.mappings()], int(total)

    def get_attrs(self, run_id: str, scope_path: str) -> dict[str, Any]:
        a = FLOW_RT_ATTRS
        with self.db.connect() as conn:
            rows = conn.connection.execute(select(a.c.name, a.c.value).where(
                self._scope(a), a.c.run_id == run_id, a.c.scope_path == scope_path))
            return {row.name: row.value for row in rows}

    def put_attrs(self, run_id: str, scope_path: str, values: Mapping[str, Any]) -> None:
        a = FLOW_RT_ATTRS
        now = time.time()
        with self.db.connect() as conn:
            for name, value in values.items():
                stmt = pg_insert(a).values(tenant_id=self.tenant_id,
                    environment_id=self.environment_id, run_id=run_id,
                    scope_path=scope_path, name=name, value=value, updated_at=now)
                conn.connection.execute(stmt.on_conflict_do_update(
                    index_elements=[a.c.tenant_id, a.c.environment_id,
                                    a.c.run_id, a.c.scope_path, a.c.name],
                    set_={"value": value, "updated_at": now}))

    def record_delivery(self, *, run_id: str, visit_id: str, message_id: str,
                        recipient_kind: str, recipient_ref: str, channel: str,
                        state: str, attempt: int = 1, provider_id: str | None = None,
                        error: Any = None) -> dict:
        t = FLOW_RT_NOTIF
        now = time.time()
        values = dict(id=_id(), tenant_id=self.tenant_id,
                      environment_id=self.environment_id, run_id=run_id,
                      visit_id=visit_id, message_id=message_id,
                      recipient_kind=recipient_kind, recipient_ref=recipient_ref,
                      channel=channel, state=state, attempt=attempt,
                      provider_id=provider_id, error=error,
                      created_at=now, updated_at=now)
        with self.db.connect() as conn:
            conn.connection.execute(insert(t).values(**values))
        return values

    def list_deliveries(self, visit_id: str) -> list[dict]:
        t = FLOW_RT_NOTIF
        with self.db.connect() as conn:
            rows = conn.connection.execute(select(t).where(self._scope(t),
                t.c.visit_id == visit_id).order_by(t.c.created_at))
            return [dict(r) for r in rows.mappings()]

    def list_recipient_notifications(self, user_id: str,
                                     roles: list[str] | tuple[str, ...] = (),
                                     *, limit: int = 100) -> list[dict]:
        """Read only messages addressed to this user or one of their roles."""
        t, m = FLOW_RT_NOTIF, SESSION_MESSAGES
        recipients = [and_(t.c.recipient_kind == "user", t.c.recipient_ref == user_id)]
        if roles:
            recipients.append(and_(t.c.recipient_kind.in_(("group", "role")),
                                   t.c.recipient_ref.in_(tuple(roles))))
        n = FLOW_RT_NODE_STATUSES
        stmt = select(t, m.c.content, m.c.message_kind, m.c.channel, m.c.direction,
                      m.c.subject, m.c.sender, m.c.recipients, m.c.body_html,
                      m.c.created_at.label("message_created_at"),
                      n.c.node_id, n.c.node_path, n.c.run_id.label("flow_run_id")).join(
                          m, and_(m.c.tenant_id == t.c.tenant_id,
                                  m.c.environment_id == t.c.environment_id,
                                  m.c.id == t.c.message_id)).join(
                          n, and_(n.c.tenant_id == t.c.tenant_id,
                                  n.c.environment_id == t.c.environment_id,
                                  n.c.id == t.c.visit_id)).where(
                              self._scope(t), n.c.state == "waiting", or_(*recipients))
        with self.db.connect() as conn:
            rows = conn.connection.execute(stmt.order_by(m.c.created_at.desc()).limit(limit))
            return [dict(row) for row in rows.mappings()]
