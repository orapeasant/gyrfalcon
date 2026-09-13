"""Runner — the single-box execution topology from §9.2.

Not the Worker/work-pool half of §9.2's table (heterogeneous fleets, Docker/K8s
job variables) — that infrastructure has no referent in a single gyrfalcon
process, and building it would be scope for its own sake. This is deliberately
the "dev, small prod, single box" row: a tick loop, mirroring
`gyrfalcon.scheduler.Scheduler` on purpose rather than inventing a second
polling pattern gyrfalcon didn't already have.

`CancelFinalizer` (§9.2): never claim a clean outcome you did not verify. That
principle already lives in `store.RunStore._reconcile_crashed` for process
restarts and in `engine._settle_cancellation` for the live case; this module
adds nothing further to it.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger("gyrfalcon.flow.runner")

_HEARTBEAT_STALE_SECONDS = 150


def _heartbeat_path():
    from pathlib import Path

    from gyrfalcon.flow.store import get_store

    db_path = get_store().db_path
    if not db_path:
        # PostgreSQL: the store has no file to sit beside. Fall back to the
        # profile directory — this signal is about "is a runner alive on this
        # machine", which is a per-host question either way.
        from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home
        return get_gyrfalcon_home() / "flow.runner-heartbeat"
    return Path(db_path).with_suffix(".runner-heartbeat")


def is_runner_running() -> bool:
    """Cross-process liveness check, mirroring `scheduler.is_scheduler_running`.

    The runner's tick loop lives in whatever process started it; the dashboard,
    the CLI, and any other process need an on-disk signal to know whether a
    deployment will actually fire, for the same reason the scheduler needed one.
    """
    if _RUNNER is not None and _RUNNER._running:
        return True
    try:
        age = time.time() - float(_heartbeat_path().read_text().strip())
    except (OSError, ValueError):
        return False
    return age < _HEARTBEAT_STALE_SECONDS


def _write_heartbeat() -> None:
    try:
        _heartbeat_path().write_text(str(time.time()))
    except OSError as e:
        logger.warning(f"Could not write runner heartbeat: {e}")


class Runner:
    """Polls deployments, starts due flow runs, respects concurrency limits."""

    def __init__(self, tick_seconds: float = 5.0):
        self.tick_seconds = tick_seconds
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="flow-runner")
        self._thread.start()
        logger.info("Flow runner started")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Flow runner stopped")

    def _loop(self) -> None:
        while self._running:
            _write_heartbeat()
            self.tick()
            for _ in range(int(self.tick_seconds * 10)):
                if not self._running:
                    break
                time.sleep(0.1)

    def tick(self) -> None:
        try:
            from gyrfalcon.flow.deployments import get_deployment_store

            store = get_deployment_store()
            # Claiming advances the schedule as part of winning the row
            # (§15.6.2), which also gives the old `finally: advance_schedule`
            # behaviour for free: a deployment whose flow fails to launch has
            # already moved to its next slot and cannot spin in a tight loop.
            for dep in store.claim_due():
                try:
                    self._start(dep)
                except Exception:
                    logger.error(f"Failed to start deployment {dep['name']!r}", exc_info=True)
        except Exception:
            logger.error("Runner tick failed", exc_info=True)

    def _start(self, dep: dict) -> None:
        from gyrfalcon.identity import owner_of, use_principal

        # Scheduled work runs as the person who created the deployment, not as
        # the platform (§17.4). An unattended run still has someone's name on
        # it, and it is scoped to their tenant rather than seeing everything.
        with use_principal(owner_of(dep)):
            self._start_as_owner(dep)

    def _start_as_owner(self, dep: dict) -> Optional[str]:
        """Returns the new run's id, or None if it was skipped (unregistered
        flow, or at its concurrency limit) — a scheduled tick logs and moves
        on either way, but `run_now` (below) turns a `None` here into a real
        error, since a manual click has a caller waiting on the result."""
        import contextvars

        from gyrfalcon.flow.registry import get_definition
        from gyrfalcon.flow.store import get_store

        template = get_definition(dep["flow_name"])
        if template is None:
            logger.warning(
                f"Deployment {dep['name']!r} references unregistered flow "
                f"{dep['flow_name']!r}; skipping this tick."
            )
            return None

        params = dict(dep["parameters"])

        # Reserve and create the run row in one transaction (§15.6.3). The
        # old count-then-start let two runners both see room and both start.
        run_id = get_store().reserve_run_slot(
            dep["flow_name"],
            dep["concurrency_limit"],
            parameters={k: repr(v) for k, v in params.items()},
            tags=list(dep["tags"]),
        )
        if run_id is None:
            logger.info(
                f"Deployment {dep['name']!r} at concurrency limit "
                f"({dep['concurrency_limit']}); skipping this tick."
            )
            return None

        def _run() -> None:
            try:
                template(**params, return_type="state", run_id=run_id)
            except Exception:
                logger.error(f"Deployment {dep['name']!r} run raised", exc_info=True)

        # The run executes on its own thread, and a bare thread starts with an
        # empty context — which would drop the owner's identity exactly the way
        # the task pool used to drop the parent link.
        ctx = contextvars.copy_context()
        threading.Thread(
            target=lambda: ctx.run(_run), daemon=True,
            name=f"deployment-{dep['name'][:20]}",
        ).start()
        return run_id

    def run_now(self, deployment_id: str) -> str:
        """Fire a deployment immediately, outside its schedule.

        Deliberately does not go through `_start`/`owner_of` — a scheduled
        tick is unattended, so §17.4 attributes it to the deployment's
        recorded owner; a manual "Run" click has someone present right now,
        so it runs as *that* caller's already-current principal instead
        (the same REST-caller attribution §20.6 uses for flow invocation).
        `contextvars.copy_context()` below carries whichever principal is
        active at call time, so no identity override is needed here.

        Does not touch `next_run_at` — firing early must not perturb the
        deployment's own schedule; the next scheduled tick still fires on
        its original cadence.
        """
        from gyrfalcon.flow.deployments import get_deployment_store

        dep = get_deployment_store().get(deployment_id)
        if dep is None:
            raise ValueError(f"No deployment {deployment_id!r}")
        run_id = self._start_as_owner(dep)
        if run_id is None:
            raise ValueError(
                f"Deployment {dep['name']!r} could not be started — its flow "
                f"is not registered, or it is at its concurrency limit."
            )
        return run_id


_RUNNER: Optional[Runner] = None
_RUNNER_LOCK = threading.Lock()


def get_runner() -> Runner:
    global _RUNNER
    with _RUNNER_LOCK:
        if _RUNNER is None:
            _RUNNER = Runner()
        return _RUNNER


def start_runner(tick_seconds: float = 5.0) -> Runner:
    r = get_runner()
    r.tick_seconds = tick_seconds
    if not r._running:
        r.start()
    return r


def stop_runner() -> None:
    global _RUNNER
    with _RUNNER_LOCK:
        if _RUNNER is not None:
            _RUNNER.stop()
