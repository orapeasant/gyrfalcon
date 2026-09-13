"""Run context.

Spec: §7 — one function returns the innermost flow-or-task context, and the
context is explicitly serializable so it can cross a process boundary. The
`detached` flag marks a context that has been shipped.
"""

from __future__ import annotations

import contextlib
import contextvars
from typing import Any, Iterator, Optional

from pydantic import BaseModel, ConfigDict

from gyrfalcon.flow.exceptions import MissingContextError


class RunContext(BaseModel):
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    run_id: Optional[str] = None
    parameters: dict[str, Any] = {}
    detached: bool = False


class FlowRunContext(RunContext):
    flow: Any = None
    flow_run: Any = None
    task_runner: Any = None
    result_store: Any = None
    persist_result: bool = False


class TaskRunContext(RunContext):
    task: Any = None
    task_run: Any = None
    result_store: Any = None


_FLOW_CTX: contextvars.ContextVar[Optional[FlowRunContext]] = contextvars.ContextVar(
    "gyrfalcon_flow_run_context", default=None
)
_TASK_CTX: contextvars.ContextVar[Optional[TaskRunContext]] = contextvars.ContextVar(
    "gyrfalcon_task_run_context", default=None
)


def get_run_context() -> RunContext:
    """Innermost context. Task wins over flow; raises outside a run."""
    ctx = _TASK_CTX.get() or _FLOW_CTX.get()
    if ctx is None:
        raise MissingContextError("no active flow or task run")
    return ctx


def get_flow_run_context() -> Optional[FlowRunContext]:
    return _FLOW_CTX.get()


def get_task_run_context() -> Optional[TaskRunContext]:
    return _TASK_CTX.get()


@contextlib.contextmanager
def flow_run_context(ctx: FlowRunContext) -> Iterator[FlowRunContext]:
    token = _FLOW_CTX.set(ctx)
    try:
        yield ctx
    finally:
        _FLOW_CTX.reset(token)


@contextlib.contextmanager
def task_run_context(ctx: TaskRunContext) -> Iterator[TaskRunContext]:
    token = _TASK_CTX.set(ctx)
    try:
        yield ctx
    finally:
        _TASK_CTX.reset(token)


#: Live handles that cannot cross a process boundary. What ships is enough to
#: *reconstruct* them on the far side (§9.1 pull_steps/entrypoint), not the
#: objects themselves.
_NON_TRANSPORTABLE = ("flow", "flow_run", "task", "task_run", "task_runner", "result_store")


def _portable(ctx: Optional[RunContext]) -> Optional[dict[str, Any]]:
    if ctx is None:
        return None
    payload = ctx.model_dump(mode="json", exclude=set(_NON_TRANSPORTABLE))
    # Carry identity so the receiver can re-import the code by name.
    for attr in ("flow", "task"):
        template = getattr(ctx, attr, None)
        if template is not None:
            payload[f"{attr}_name"] = getattr(template, "name", None)
    return payload


def serialize_context() -> dict[str, Any]:
    """Snapshot the active context for shipment to another process."""
    return {
        "flow_run_context": _portable(_FLOW_CTX.get()),
        "task_run_context": _portable(_TASK_CTX.get()),
    }


@contextlib.contextmanager
def hydrated_context(serialized: dict[str, Any]) -> Iterator[None]:
    """Rebuild a shipped context. Everything rebuilt here is `detached`."""
    with contextlib.ExitStack() as stack:
        flow_payload = (serialized or {}).get("flow_run_context")
        if flow_payload:
            ctx = FlowRunContext(**{**flow_payload, "detached": True})
            stack.enter_context(flow_run_context(ctx))
        task_payload = (serialized or {}).get("task_run_context")
        if task_payload:
            ctx = TaskRunContext(**{**task_payload, "detached": True})
            stack.enter_context(task_run_context(ctx))
        yield
