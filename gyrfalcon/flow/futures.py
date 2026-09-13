"""Futures, task runners, and the execution-discovered DAG.

Spec: §5.1–5.4.

A future is identified by a **run id**, not an in-memory handle — that is what
lets it cross a process boundary or be reconstructed after a restart. The DAG is
discovered by execution rather than declared, which is the right default for an
agentic system where an agent decides at step 3 whether there is a step 4.
"""

from __future__ import annotations

import contextvars
import threading
import time
from concurrent.futures import Future as _Future
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, Optional

from gyrfalcon.flow import states as st
from gyrfalcon.flow.annotations import _Annotation
from gyrfalcon.flow.exceptions import MappingMissingIterable
from gyrfalcon.flow.states import State

#: run_id → State, so a future is reconstructable from its id alone.
_RUN_STATES: dict[str, State] = {}
#: run_id → upstream run_ids (implicit data edges and explicit wait_for edges).
_EDGES: dict[str, set[str]] = {}
#: parent run_id → child run_ids, for "the agent did these six tool calls".
_ENCAPSULATING: dict[str, set[str]] = {}

_LOCK = threading.Lock()


def record_state(run_id: str, state: State) -> None:
    with _LOCK:
        _RUN_STATES[run_id] = state


def record_edge(downstream: str, upstream: str) -> None:
    with _LOCK:
        _EDGES.setdefault(downstream, set()).add(upstream)


def record_encapsulation(parent: str, child: str) -> None:
    with _LOCK:
        _ENCAPSULATING.setdefault(parent, set()).add(child)


def edges_for(run_id: str) -> set[str]:
    return set(_EDGES.get(run_id, set()))


def encapsulating_edges(run_id: str) -> set[str]:
    return set(_ENCAPSULATING.get(run_id, set()))


class BaseFuture:
    """Identity is the run id; the handle is incidental."""

    def __init__(self, task_run_id: str):
        self.task_run_id = task_run_id

    def wait(self, timeout: Optional[float] = None) -> State:
        raise NotImplementedError

    def result(self, timeout: Optional[float] = None) -> Any:
        return self.wait(timeout).result()

    @property
    def state(self) -> State:
        return _RUN_STATES.get(self.task_run_id, st.Pending())

    def add_done_callback(self, fn: Callable[["BaseFuture"], Any]) -> None:
        raise NotImplementedError


class ConcurrentFuture(BaseFuture):
    """Backed by a thread pool."""

    def __init__(self, task_run_id: str, future: _Future):
        super().__init__(task_run_id)
        self._future = future

    def wait(self, timeout: Optional[float] = None) -> State:
        state = self._future.result(timeout)
        record_state(self.task_run_id, state)
        return state

    def add_done_callback(self, fn: Callable[["BaseFuture"], Any]) -> None:
        self._future.add_done_callback(lambda _: fn(self))


class DistributedFuture(BaseFuture):
    """Reconstructed from an id — survives the process that created it.

    Polls for a terminal state rather than reporting whatever is recorded right
    now; a future rehydrated the instant after submission would otherwise report
    the run as Pending forever.
    """

    poll_interval = 0.005

    def wait(self, timeout: Optional[float] = None) -> State:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            state = self.state
            if state.is_final():
                return state
            if deadline is not None and time.monotonic() >= deadline:
                return state
            time.sleep(self.poll_interval)


def future_from_id(task_run_id: str) -> BaseFuture:
    """Rebuild a future from its run id alone (§5.1)."""
    return DistributedFuture(task_run_id)


# ── task runners ──────────────────────────────────────────────────────────────

class TaskRunner:
    def submit(self, task, parameters, wait_for=None) -> BaseFuture:
        raise NotImplementedError

    def map(self, task, parameters, wait_for=None) -> list[BaseFuture]:
        raise NotImplementedError


class ThreadPoolTaskRunner(TaskRunner):
    """One pool, shared by every tenant — with a per-tenant share of it.

    §17.8: an unbounded shared pool is a noisy-neighbour problem. One tenant
    submitting a thousand tasks fills every worker and everyone else waits.
    A per-tenant semaphore, set *below* the pool size, means no single tenant
    can occupy the whole pool and others always have workers available.

    The permit is taken on the submitting thread, before the work reaches the
    pool. Acquiring inside the worker would hold a pool slot while blocked,
    which is how a fairness mechanism turns into a deadlock. Blocking the
    submitter is the backpressure doing its job.
    """

    def __init__(self, max_workers: int = 8):
        self.max_workers = max_workers
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._tenant_permits: dict[str, threading.Semaphore] = {}
        self._permit_lock = threading.Lock()

    def _tenant_limit(self) -> int:
        """Concurrent tasks one tenant may hold. 0 disables the cap."""
        from gyrfalcon.identity import identity_enabled

        if not identity_enabled():
            # A single-user install is its own tenant; capping it would just
            # be a slower gyrfalcon with no one to be fair to.
            return 0
        try:
            from gyrfalcon.config import cfg_get

            configured = cfg_get("flow.limits.tenant_max_concurrent_tasks", None)
        except Exception:
            configured = None
        if configured is None:
            return max(1, self.max_workers // 2)
        return max(0, int(configured))

    def _permit(self, tenant_id: str):
        limit = self._tenant_limit()
        if not limit:
            return None
        with self._permit_lock:
            sem = self._tenant_permits.get(tenant_id)
            if sem is None:
                sem = threading.Semaphore(limit)
                self._tenant_permits[tenant_id] = sem
        return sem

    def submit(self, task, parameters, wait_for=None) -> BaseFuture:
        run_id = st.new_run_id()
        _link_upstreams(run_id, parameters, wait_for)

        from gyrfalcon.identity import require_principal

        sem = self._permit(require_principal().tenant_id)
        if sem is not None:
            sem.acquire()

        # ThreadPoolExecutor does not copy the calling contextvars.Context into
        # the worker thread. Without capturing it here, a task submitted from
        # inside a flow loses get_flow_run_context() on the worker thread, and
        # silently drops its parent linkage (persistence, encapsulating edges)
        # with no error — it just looks like an orphaned run.
        caller_ctx = contextvars.copy_context()

        def _run() -> State:
            try:
                engine = task.engine_cls(task, _resolved(parameters))
                engine.run_id = run_id
                state = engine.run()
                record_state(run_id, state)
                return state
            finally:
                if sem is not None:
                    sem.release()

        return ConcurrentFuture(run_id, self._pool.submit(caller_ctx.run, _run))


_DEFAULT_RUNNER = ThreadPoolTaskRunner()


def _link_upstreams(run_id: str, parameters: dict, wait_for: Optional[Iterable]) -> None:
    """Collect upstream links *before* resolving, to retain the graph (§5.3)."""
    for value in parameters.values():
        for fut in _futures_in(value):
            record_edge(run_id, fut.task_run_id)
    for fut in wait_for or []:
        if isinstance(fut, BaseFuture):
            record_edge(run_id, fut.task_run_id)


def _futures_in(value: Any) -> list[BaseFuture]:
    if isinstance(value, BaseFuture):
        return [value]
    if isinstance(value, _Annotation):
        return _futures_in(value.unwrap())
    if isinstance(value, (list, tuple, set)):
        return [f for v in value for f in _futures_in(v)]
    if isinstance(value, dict):
        return [f for v in value.values() for f in _futures_in(v)]
    return []


def _resolved(parameters: dict) -> dict:
    from gyrfalcon.flow.annotations import resolve_argument
    return {k: resolve_argument(v) for k, v in parameters.items()}


def _bind(task, args: tuple, kwargs: dict) -> dict:
    import inspect
    bound = inspect.signature(task.fn).bind_partial(*args, **kwargs)
    bound.apply_defaults()
    return dict(bound.arguments)


def submit_task(task, args: tuple, kwargs: dict, wait_for=None) -> BaseFuture:
    runner = task.task_runner or _DEFAULT_RUNNER
    return runner.submit(task, _bind(task, args, kwargs), wait_for=wait_for)


#: Iterable types that are values in their own right, never things to map over.
_ATOMIC_ITERABLES = (str, bytes, bytearray, dict)


def _is_mappable(value: Any) -> bool:
    """Whether `map` should iterate this argument or broadcast it.

    Annotations and futures are always broadcast; strings and dicts are values,
    not sequences to fan out over.
    """
    if isinstance(value, (_Annotation, BaseFuture)):
        return False
    if isinstance(value, _ATOMIC_ITERABLES):
        return False
    return hasattr(value, "__iter__")


def map_task(task, args: tuple, kwargs: dict, wait_for=None) -> list[BaseFuture]:
    """Partition parameters into iterable (mapped) and static (broadcast)."""
    parameters = _bind(task, args, kwargs)

    iterable_keys = [k for k, v in parameters.items() if _is_mappable(v)]

    if not iterable_keys:
        raise MappingMissingIterable(
            f"{task.name}.map() requires at least one iterable parameter; "
            f"got {sorted(parameters)} with everything broadcast"
        )

    # Materialize once: a generator argument is exhausted by the first pass, so
    # measuring and then re-listing would silently map over nothing.
    materialized = {k: list(parameters[k]) for k in iterable_keys}
    length = min(len(v) for v in materialized.values())

    futures: list[BaseFuture] = []
    for i in range(length):
        child = dict(parameters)
        for key in iterable_keys:
            child[key] = materialized[key][i]
        runner = task.task_runner or _DEFAULT_RUNNER
        futures.append(runner.submit(task, child, wait_for=wait_for))
    return futures
