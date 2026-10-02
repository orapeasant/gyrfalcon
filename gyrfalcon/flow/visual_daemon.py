"""Process-pool dispatcher for published visual flows.

The database is the work queue. A worker opens its own connection, restores the
persisted run, and returns to the pool when it completes or hands off a wait.
"""

from __future__ import annotations

import logging
import multiprocessing
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from gyrfalcon.flow.runtime_store import FlowRuntimeStore
from gyrfalcon.identity import LOCAL, use_principal

logger = logging.getLogger(__name__)
_ACTIVE: "VisualFlowDaemon | None" = None
_WORKER_ACTIVITIES_READY = False


def _initialize_visual_worker() -> None:
    """Load flow activity code into this spawned worker process."""
    global _WORKER_ACTIVITIES_READY
    if _WORKER_ACTIVITIES_READY:
        return
    # Spawned workers do not inherit the daemon's in-memory activity registry.
    # Built-ins and user-authored flow modules must be imported in each worker.
    from gyrfalcon.flow import sample_activities  # noqa: F401
    from gyrfalcon.flow.registry import discover_flows, get_import_errors

    discover_flows()
    for filename, error in get_import_errors().items():
        logger.warning("Worker could not load flow activity module %s: %s", filename, error)
    _WORKER_ACTIVITIES_READY = True


def _execute_visual_run(tenant_id: str, environment_id: str, run_id: str) -> None:
    """Top-level process target: never inherit a parent's SQLAlchemy engine."""
    _initialize_visual_worker()
    store = FlowRuntimeStore(tenant_id=tenant_id, environment_id=environment_id)
    try:
        run = store.get_run(run_id)
        if run is None or run["state"] != "running":
            return
        version = store.get_version(run["definition_id"], run["version"])
        if version is None:
            raise ValueError("Pinned flow version is unavailable")
        from gyrfalcon.flow.visual_executor import VisualExecutor
        from gyrfalcon.flow.visual_adapters import activity_dispatcher, notification_dispatcher

        executor = VisualExecutor(
            store=store, run_id=run_id, graph=version["graph"],
            activity_dispatcher=lambda node, context, visit_id: activity_dispatcher(
                store, node, context, visit_id),
            notification_dispatcher=lambda node, context, visit_id: notification_dispatcher(
                store, node, context, visit_id, version["notification_snapshot"]),
            resolve_reference=lambda definition_id, pinned_version: store.get_version(
                definition_id, pinned_version)["graph"],
        )
        if run["user_id"] == "local" and run["tenant_id"] == "local":
            principal = LOCAL
        else:
            from gyrfalcon.auth.store import get_auth_store
            principal = get_auth_store().principal_for(
                run["user_id"], run["tenant_id"], source="flow_worker")
            if principal is None:
                raise PermissionError("The flow owner is no longer an active member of its organization")
        with use_principal(principal):
            executor.run()
    except BaseException as exc:
        logger.exception("Visual flow worker failed for %s", run_id)
        store.update_run(run_id, state="failed", error={"type": type(exc).__name__,
                                                       "message": str(exc)})
        store.record_event(run_id, "flow.failed", {"error": str(exc)})
        raise
    finally:
        store.close()


def _next_schedule_time(schedule: str) -> float | None:
    from gyrfalcon.scheduler import next_run_iso, parse_schedule

    parsed = parse_schedule(schedule, use_llm=False)
    value = next_run_iso(parsed)
    return datetime.fromisoformat(value).timestamp() if value else None


class VisualFlowDaemon:
    """Dispatches queued runs, waits, and schedules to reusable OS workers."""

    def __init__(self, *, max_workers: int = 4, poll_seconds: float = 1.0,
                 environment_id: str = "default"):
        self.max_workers = max(1, max_workers)
        self.poll_seconds = max(0.1, poll_seconds)
        self.environment_id = environment_id
        self._pool: Any = None
        self._jobs: dict[str, tuple[Any, dict]] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._worker_prefix = uuid.uuid4().hex

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._pool = multiprocessing.get_context("spawn").Pool(
                self.max_workers, initializer=_initialize_visual_worker)
            self._reconcile_expired_workers()
            self._thread = threading.Thread(target=self._loop, name="visual-flow-daemon",
                                            daemon=True)
            self._thread.start()

    def _reconcile_expired_workers(self) -> None:
        store = FlowRuntimeStore(environment_id=self.environment_id)
        try:
            for run in store.list_stale_running():
                tenant_store = FlowRuntimeStore(db=store.db,
                    tenant_id=run["tenant_id"], environment_id=run["environment_id"])
                tenant_store.mark_crashed(run["id"], {"type": "WorkerLeaseExpired",
                    "message": "Worker lease expired while the daemon was unavailable"})
                tenant_store.record_event(run["id"], "flow.crashed",
                                          {"reason": "worker_lease_expired"})
        finally:
            store.close()

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.tick()
            except Exception:
                logger.exception("Visual flow daemon tick failed")

    def tick(self) -> None:
        with self._lock:
            if self._pool is None:
                return
            system_store = FlowRuntimeStore(environment_id=self.environment_id)
            try:
                for visit in system_store.list_expired_waits():
                    tenant_store = FlowRuntimeStore(db=system_store.db,
                        tenant_id=visit["tenant_id"], environment_id=visit["environment_id"])
                    if visit.get("timeout_transient"):
                        resumed = tenant_store.expire_wait(visit["id"], visit["timeout_transient"])
                        if resumed:
                            tenant_store.record_event(visit["run_id"], "node.timeout",
                                                      {"transient": visit["timeout_transient"]},
                                                      visit_id=visit["id"])
                for deployment in system_store.list_due_deployments():
                    tenant_store = FlowRuntimeStore(db=system_store.db,
                        tenant_id=deployment["tenant_id"], environment_id=self.environment_id)
                    definition = tenant_store.get_definition(deployment["definition_id"])
                    if definition is None or not definition.get("enabled", True):
                        continue
                    try:
                        next_at = _next_schedule_time(deployment["schedule"])
                    except Exception as exc:
                        logger.error("Invalid schedule on deployment %s: %s", deployment["id"], exc)
                        continue
                    with system_store.db.connect():
                        claimed = tenant_store.claim_due_deployment(
                            deployment["id"], deployment["next_run_at"], next_at)
                        if claimed:
                            tenant_store.create_run(
                                deployment["definition_id"], deployment["version"],
                                deployment["parameters"], "schedule", user_id=deployment["user_id"],
                                deployment_id=deployment["id"],
                                caller_key=f"schedule:{deployment['id']}:{deployment['next_run_at']}",
                            )
                for run_id, (job, run) in list(self._jobs.items()):
                    tenant_store = FlowRuntimeStore(db=system_store.db,
                        tenant_id=run["tenant_id"], environment_id=run["environment_id"])
                    if job.ready():
                        try:
                            job.get()
                        except BaseException as exc:
                            tenant_store.mark_crashed(run_id, {"type": type(exc).__name__,
                                                               "message": str(exc)})
                        self._jobs.pop(run_id, None)
                    else:
                        tenant_store.renew_lease(run_id, run["lease_owner"])
                while len(self._jobs) < self.max_workers:
                    worker_id = f"{self._worker_prefix}:{uuid.uuid4().hex}"
                    run = system_store.claim_next_run(worker_id,
                                                      environment_id=self.environment_id)
                    if run is None:
                        break
                    job = self._pool.apply_async(_execute_visual_run,
                        (run["tenant_id"], run["environment_id"], run["id"]))
                    self._jobs[run["id"]] = (job, run)
            finally:
                system_store.close()

    def bounce(self) -> None:
        """Kill active workers, crash their runs, and restart the worker pool."""
        with self._lock:
            if self._pool is None:
                return
            self._pool.terminate()
            self._pool.join()
            system_store = FlowRuntimeStore(environment_id=self.environment_id)
            try:
                for run_id, (_, run) in self._jobs.items():
                    tenant_store = FlowRuntimeStore(db=system_store.db,
                        tenant_id=run["tenant_id"], environment_id=run["environment_id"])
                    tenant_store.mark_crashed(run_id, {"type": "DaemonBounce",
                                                       "message": "Worker terminated by administrator"})
                    tenant_store.record_event(run_id, "flow.crashed", {"reason": "daemon_bounce"})
            finally:
                system_store.close()
            self._jobs.clear()
            self._pool = multiprocessing.get_context("spawn").Pool(
                self.max_workers, initializer=_initialize_visual_worker)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_seconds + 2)
        with self._lock:
            if self._pool is None:
                return
            self._pool.terminate()
            self._pool.join()
            system_store = FlowRuntimeStore(environment_id=self.environment_id)
            try:
                for run_id, (_, run) in self._jobs.items():
                    tenant_store = FlowRuntimeStore(db=system_store.db,
                        tenant_id=run["tenant_id"], environment_id=run["environment_id"])
                    tenant_store.mark_crashed(run_id, {"type": "DaemonStop",
                                                       "message": "Worker terminated during daemon stop"})
            finally:
                system_store.close()
            self._pool = None
            self._jobs.clear()


def get_visual_daemon() -> VisualFlowDaemon:
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = VisualFlowDaemon()
    return _ACTIVE
