"""The engine.

Spec: §3.1–3.5.

The core is a small loop whose condition is *"is this run still RUNNING?"*.
A retry is not recursion and not a for-loop: a failure handler sets the state
back to Running/AwaitingRetry, which re-satisfies the predicate. That is what
makes retries a control-plane concern rather than a code-path concern — a server
or an operator can inject one the engine never planned.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from typing import Any, Callable, Optional

from gyrfalcon.flow import states as st
from gyrfalcon.flow.context import (
    FlowRunContext,
    TaskRunContext,
    flow_run_context,
    task_run_context,
)
from gyrfalcon.flow.exceptions import (
    Abort,
    FlowRunTimeoutError,
    Pause,
    TaskRunTimeoutError,
    UpstreamTaskError,
    _EngineTimeoutError,
)
from gyrfalcon.flow.states import State

logger = logging.getLogger("gyrfalcon.flow")


class NotSet:
    """Distinguishes 'returned None' from 'hasn't returned' (§3.2)."""

    _singleton: Optional["NotSet"] = None

    def __new__(cls) -> "NotSet":
        if cls._singleton is None:
            cls._singleton = super().__new__(cls)
        return cls._singleton

    def __repr__(self) -> str:
        return "NotSet"

    def __bool__(self) -> bool:
        return False


NOT_SET = NotSet()


# ── retry helpers (pure, so they are testable without a run) ─────────────────

def compute_retry_delay(retry_delay_seconds: Any, attempt: int) -> Optional[float]:
    """A delay list is a backoff schedule whose last element repeats forever.

    `[1, 2, 4]` → 1, 2, 4, 4, 4… (§3.3)
    """
    if retry_delay_seconds is None:
        return None
    if isinstance(retry_delay_seconds, (list, tuple)):
        if not retry_delay_seconds:
            return None
        return retry_delay_seconds[min(attempt, len(retry_delay_seconds) - 1)]
    return retry_delay_seconds


def build_retry_state(delay_seconds: Optional[float], now: Optional[float] = None) -> State:
    """A delayed retry is a SCHEDULED state with a future time, never a sleep().

    That is what lets it survive a process restart and render as
    "will retry at 14:32".
    """
    if not delay_seconds:
        return st.Retrying()
    base = time.time() if now is None else now
    return st.AwaitingRetry(scheduled_time=base + delay_seconds)


def state_for_upstream_error(exc: BaseException) -> State:
    """Blocked is visibly distinct from broken (§5.4)."""
    return st.NotReady(message=str(exc))


# ── engines ───────────────────────────────────────────────────────────────────

class _BaseRunEngine:
    """Shared lifecycle. Concrete engines differ only in where state is written."""

    state_is_server_authoritative: bool = False

    def __init__(self, template: Any, parameters: Optional[dict] = None,
                 client: Any = None, run_id: Optional[str] = None,
                 persist: Optional[bool] = None):
        # `persist` is a class attribute below, which makes it a process-wide
        # switch — fine for one tenant, a shared mutable global for many
        # (§17.8). Setting it here shadows the class value for this engine
        # only, so one caller can persist without deciding for everyone.
        if persist is not None:
            self.persist = persist
        self.template = template
        self.parameters = parameters or {}
        self.client = client
        # A caller may pre-assign the id when it had to create the run row
        # first — the runner reserves a concurrency slot that way (§15.6.3).
        # `create_run` is insert-or-ignore, so adopting the id is a no-op
        # against the reservation rather than a second row.
        self.run_id = run_id or st.new_run_id()
        self.state: State = st.Pending()
        self.retries = 0
        self._return_value: Any = NOT_SET
        self._raised: Any = NOT_SET
        self._is_started = False
        self.short_circuit = False

    # -- persistence ---------------------------------------------------------
    #: Off by default so the library stays usable with no database; the API and
    #: UI turn it on, and phase 4 is what makes external control possible at all.
    #: This is the process-wide *default* — pass `persist=` to an engine to
    #: override it for one run without mutating shared state (§17.8).
    persist: bool = False

    def _store(self):
        if not self.persist:
            return None
        from gyrfalcon.flow.store import get_store
        return get_store()

    def _register_run(self) -> None:
        store = self._store()
        if store is None:
            return
        from gyrfalcon.flow.context import get_flow_run_context

        parent = get_flow_run_context()
        store.create_run(
            run_id=self.run_id,
            name=self.template.name,
            kind="flow" if self.template.is_flow else "task",
            parameters={k: repr(v) for k, v in self.parameters.items()},
            parent_run_id=parent.run_id if parent else None,
            flow_run_id=parent.run_id if (parent and not self.template.is_flow) else None,
            tags=list(getattr(self.template, "tags", [])),
        )

    # -- state ---------------------------------------------------------------
    def set_state(self, state: State, force: bool = False) -> State:
        """Adopt a state.

        Flow runs propose to the orchestration layer when a client is attached,
        so an operator's cancel or an injected retry can substitute a different
        state. Task runs write locally — no round trip (§4.1).
        """
        if self.client is not None and self.state_is_server_authoritative and not force:
            from gyrfalcon.flow.orchestration import propose_state

            state = propose_state(self.client, self.run_id, state)

        # A pending cancel outranks whatever outcome the body produced. Checked
        # here rather than after the loop because writing the outcome would
        # otherwise overwrite the CANCELLING row and lose the request.
        if state.is_final() and not state.is_cancelled() and self._cancel_requested():
            state = st.Cancelled(message="cancelled by operator")

        self.state = state
        state.id = state.id or self.run_id
        store = self._store()
        if store is not None:
            store.record_transition(self.run_id, state)
        return state

    def is_running(self) -> bool:
        if self._cancel_requested():
            return False
        return self.state.type in (st.StateType.RUNNING, st.StateType.SCHEDULED)

    def _cancel_requested(self) -> bool:
        """Cooperative cancellation: an operator moved the run to Cancelling."""
        store = self._store()
        if store is None:
            return False
        row = store.get_run(self.run_id)
        return bool(row and row["state_type"] == st.StateType.CANCELLING.value)

    # -- outcome handlers ----------------------------------------------------
    def handle_success(self, result: Any) -> State:
        self._return_value = result
        self._stage_rollback()
        return self.set_state(st.Completed(data=result))

    def _stage_rollback(self) -> None:
        """Register this run's compensating actions with the active transaction.

        Staged on success only: a step that never took effect has nothing to undo.
        """
        hooks = getattr(self.template, "on_rollback_hooks", None)
        if not hooks:
            return
        from gyrfalcon.flow.transactions import get_transaction

        txn = get_transaction()
        if txn is None:
            return
        for hook in hooks:
            txn.stage_rollback(hook, self.template)

    def handle_exception(self, exc: BaseException) -> State:
        self._raised = exc
        if self.handle_retry(exc):
            return self.state
        return self.set_state(st.Failed(message=str(exc), exception=exc))

    def handle_retry(self, exc: BaseException) -> bool:
        """Return True when a retry was scheduled (§3.3)."""
        if self.retries >= getattr(self.template, "retries", 0):
            return False
        if not self.can_retry(exc):
            return False

        delay = compute_retry_delay(
            getattr(self.template, "retry_delay_seconds", None), self.retries
        )
        new_state = build_retry_state(delay)
        self.set_state(new_state, force=True)
        if new_state.is_running():
            self.call_hooks(new_state)
        self.retries += 1
        return True

    def can_retry(self, exc: BaseException) -> bool:
        fn = getattr(self.template, "retry_condition_fn", None)
        if fn is None:
            return True
        probe = st.Failed(message=str(exc), exception=exc)
        try:
            return bool(fn(self.template, self, probe))
        except Exception:  # a broken predicate must not decide the run
            logger.warning("retry_condition_fn raised; defaulting to retry", exc_info=True)
            return True

    def handle_timeout(self, exc: BaseException) -> State:
        return self.set_state(st.TimedOut(message=str(exc), exception=exc))

    def handle_crash(self, exc: BaseException) -> State:
        return self.set_state(st.Crashed(message=str(exc), exception=exc))

    def handle_cancellation(self, exc: BaseException) -> State:
        return self.set_state(st.Cancelled(message=str(exc)))

    def _settle_cancellation(self) -> None:
        """Complete the two-phase cancel: Cancelling -> Cancelled.

        Cancelling means "teardown in progress" and is deliberately non-terminal;
        only the run itself may confirm it actually stopped (§2.3).
        """
        if not self._cancel_requested():
            return
        self.state = st.Cancelled(message="cancelled by operator")
        self.state.id = self.run_id
        store = self._store()
        if store is not None:
            store.record_transition(self.run_id, self.state)

    # -- hooks ---------------------------------------------------------------
    def call_hooks(self, state: State) -> None:
        """A raising hook is logged and swallowed; it may not corrupt the run."""
        for hook in self.template.hooks_for(state):
            try:
                hook(self.template, self, state)
            except Exception:
                logger.warning("hook %r raised; ignoring", hook, exc_info=True)

    def result(self) -> Any:
        return self.state.result()


def _call_with_timeout(fn: Callable[[], Any], timeout: Optional[float], timeout_exc: type) -> Any:
    """Run `fn`, raising `timeout_exc` if it outlives `timeout`.

    The worker thread is a daemon: a user function that ignores cooperative
    cancellation must not keep the process alive.
    """
    if not timeout:
        return fn()

    box: dict[str, Any] = {}

    # The caller has already entered flow_run_context(...)/task_run_context(...)
    # on *this* thread, and a bare Thread starts with an empty context. Without
    # copying it, a flow that merely sets `timeout_seconds` runs its body with
    # no run context: get_flow_run_context() returns None, and every task it
    # calls is persisted as an orphan with no flow_run_id. Same failure the
    # ThreadPoolTaskRunner hit (futures.py), in a second place.
    caller_ctx = contextvars.copy_context()

    def target() -> None:
        try:
            box["value"] = caller_ctx.run(fn)
        except BaseException as e:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = e

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise timeout_exc(f"exceeded timeout of {timeout}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


class FlowRunEngine(_BaseRunEngine):
    """Flow state is authoritative server-side — that is what buys external control."""

    state_is_server_authoritative = True
    timeout_exc = FlowRunTimeoutError

    def run(self) -> State:
        template = self.template
        ctx = FlowRunContext(run_id=self.run_id, parameters=self.parameters, flow=template)

        self._register_run()
        self.set_state(st.Running())
        self.call_hooks(self.state)

        while self.is_running():
            if self.state.is_scheduled():
                # A delayed retry: honour scheduled_time, then resume.
                delay = (self.state.state_details.scheduled_time or 0) - time.time()
                if delay > 0:
                    time.sleep(delay)
                self.set_state(st.Retrying())

            try:
                with flow_run_context(ctx):
                    value = _call_with_timeout(
                        lambda: template.fn(**self.parameters),
                        getattr(template, "timeout_seconds", None),
                        self.timeout_exc,
                    )
                self.handle_success(value)
            except _EngineTimeoutError as e:
                self.handle_timeout(e)
            except Abort:
                raise
            except Pause as p:
                if not self._handle_pause(p):
                    break
            except UpstreamTaskError as e:
                self.set_state(state_for_upstream_error(e))
            except Exception as e:
                self.handle_exception(e)
            except BaseException as e:  # the world broke, not the code
                self.handle_crash(e)
                break

        self._settle_cancellation()
        self.call_hooks(self.state)
        return self.state

    def _handle_pause(self, p: Pause) -> bool:
        """Persist the pause and wait it out.

        Without this, a paused flow's run row was left showing whatever state
        preceded the pause (usually Running) forever — the run list and the
        human-task inbox would visibly disagree about what the run was doing.

        Returns True to loop back and re-invoke the flow body (the pause was
        answered and is not rescheduled — flows are re-entrant around
        `pause_flow_run`, which returns the answer instead of pausing again once
        one has been delivered). Returns False to stop here: either the pause
        reschedules (Suspended — the process is meant to exit; a library call
        stops rather than tearing down the interpreter) or it timed out
        unanswered.
        """
        from gyrfalcon.flow.pause import _UNANSWERED, check_answer, is_expired

        state = p.state if p.state is not None else st.Paused()
        self.set_state(state, force=True)

        if state.state_details.pause_reschedule:
            return False

        # Poll under the id the pause was actually filed under. `pause_flow_run`
        # can be given an explicit flow_run_id that differs from this engine's
        # own run_id; polling self.run_id in that case would watch a key nobody
        # ever answers and never expires either — an unconditional hang.
        pause_run_id = p.run_id or self.run_id

        poll_interval = 0.05
        while True:
            if check_answer(pause_run_id) is not _UNANSWERED:
                self.set_state(st.Running(), force=True)
                return True
            if is_expired(pause_run_id):
                self.set_state(
                    st.Failed(name="PauseTimedOut", message="approval timed out"),
                    force=True,
                )
                return False
            time.sleep(poll_interval)


class TaskRunEngine(_BaseRunEngine):
    """Task state is written locally — no round trip, so a fan-out stays cheap."""

    state_is_server_authoritative = False
    timeout_exc = TaskRunTimeoutError

    def run(self) -> State:
        template = self.template
        ctx = TaskRunContext(run_id=self.run_id, parameters=self.parameters, task=template)

        self._register_run()
        self.set_state(st.Running())
        self.call_hooks(self.state)

        while self.is_running():
            if self.state.is_scheduled():
                delay = (self.state.state_details.scheduled_time or 0) - time.time()
                if delay > 0:
                    time.sleep(delay)
                self.set_state(st.Retrying())

            try:
                with task_run_context(ctx):
                    value = _call_with_timeout(
                        lambda: template.fn(**self.parameters),
                        getattr(template, "timeout_seconds", None),
                        self.timeout_exc,
                    )
                self.handle_success(value)
            except _EngineTimeoutError as e:
                self.handle_timeout(e)
            except (Abort, Pause):
                raise
            except UpstreamTaskError as e:
                self.set_state(state_for_upstream_error(e))
            except Exception as e:
                self.handle_exception(e)
            except BaseException as e:
                self.handle_crash(e)
                break

        self._settle_cancellation()
        self.call_hooks(self.state)
        return self.state
