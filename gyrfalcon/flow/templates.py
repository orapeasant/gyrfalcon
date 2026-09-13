"""Templates — `Flow` and `Task`.

Spec: §1.1–1.2.

A template is a callable plus configuration. It holds **no mutable execution
state**, so one template is safely callable from many concurrent runs; all state
lives on the run. Every knob is declarative config consumed by the engine, which
is what lets the same function run locally, in a thread, or on a worker unedited.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable, Optional, Sequence

from gyrfalcon.flow.states import State, StateType

_HOOK_FOR_TYPE = {
    StateType.RUNNING: "on_running",
    StateType.COMPLETED: "on_completion",
    StateType.FAILED: "on_failure",
    StateType.CANCELLED: "on_cancellation",
    StateType.CRASHED: "on_crashed",
}


class _Template:
    """Shared behaviour. Subclasses only differ in which engine runs them."""

    is_flow = False
    is_task = False
    engine_cls: Any = None

    def __init__(self, fn: Callable, **options: Any):
        functools.update_wrapper(self, fn)
        self.fn = fn
        self.name: str = options.pop("name", None) or fn.__name__
        self.version: Optional[str] = options.pop("version", None)
        self.description: Optional[str] = options.pop("description", None) or fn.__doc__
        self.retries: int = options.pop("retries", 0) or 0
        self.retry_delay_seconds: Any = options.pop("retry_delay_seconds", None)
        self.retry_condition_fn: Optional[Callable] = options.pop("retry_condition_fn", None)
        self.timeout_seconds: Optional[float] = options.pop("timeout_seconds", None)
        self.persist_result: bool = bool(options.pop("persist_result", False))
        self.cache_policy: Any = options.pop("cache_policy", None)
        self.cache_key_fn: Optional[Callable] = options.pop("cache_key_fn", None)
        self.cache_expiration: Optional[float] = options.pop("cache_expiration", None)
        self.tags: list[str] = list(options.pop("tags", []) or [])
        self.task_runner: Any = options.pop("task_runner", None)
        self.server_authoritative_state: bool = bool(
            options.pop("server_authoritative_state", self.is_flow)
        )

        for attr in ("on_running", "on_completion", "on_failure", "on_cancellation", "on_crashed"):
            setattr(self, f"{attr}_hooks", list(options.pop(attr, []) or []))
        self.on_commit_hooks: list[Callable] = list(options.pop("on_commit", []) or [])
        self.on_rollback_hooks: list[Callable] = list(options.pop("on_rollback", []) or [])

        # Caching without persistence is meaningless (§6.1).
        if self.cache_policy is not None:
            self.persist_result = True

        self.options = options

    # -- reconfiguration -----------------------------------------------------
    def with_options(self, **overrides: Any) -> "_Template":
        """Return a *copy* with overrides — never mutate the original (§1.2)."""
        merged = self._current_options()
        merged.update(overrides)
        return type(self)(self.fn, **merged)

    def _current_options(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "retries": self.retries,
            "retry_delay_seconds": self.retry_delay_seconds,
            "retry_condition_fn": self.retry_condition_fn,
            "timeout_seconds": self.timeout_seconds,
            "persist_result": self.persist_result,
            "cache_policy": self.cache_policy,
            "cache_key_fn": self.cache_key_fn,
            "cache_expiration": self.cache_expiration,
            "tags": list(self.tags),
            "task_runner": self.task_runner,
            "server_authoritative_state": self.server_authoritative_state,
            "on_running": list(self.on_running_hooks),
            "on_completion": list(self.on_completion_hooks),
            "on_failure": list(self.on_failure_hooks),
            "on_cancellation": list(self.on_cancellation_hooks),
            "on_crashed": list(self.on_crashed_hooks),
            "on_commit": list(self.on_commit_hooks),
            "on_rollback": list(self.on_rollback_hooks),
            **self.options,
        }

    # -- hook registration as decorator methods (§13.2) ----------------------
    def _hook_registrar(self, bucket: str) -> Callable:
        def register(fn: Callable) -> Callable:
            getattr(self, bucket).append(fn)
            return fn
        return register

    @property
    def on_rollback(self) -> Callable:
        return self._hook_registrar("on_rollback_hooks")

    @property
    def on_commit(self) -> Callable:
        return self._hook_registrar("on_commit_hooks")

    @property
    def on_completion(self) -> Callable:
        return self._hook_registrar("on_completion_hooks")

    @property
    def on_failure(self) -> Callable:
        return self._hook_registrar("on_failure_hooks")

    def hooks_for(self, state: State) -> Sequence[Callable]:
        attr = _HOOK_FOR_TYPE.get(state.type)
        return list(getattr(self, f"{attr}_hooks", [])) if attr else []

    # -- invocation ----------------------------------------------------------
    def _bind(self, args: tuple, kwargs: dict) -> dict[str, Any]:
        from gyrfalcon.flow.annotations import resolve_argument

        bound = inspect.signature(self.fn).bind_partial(*args, **kwargs)
        bound.apply_defaults()
        return {k: resolve_argument(v) for k, v in bound.arguments.items()}

    def __call__(self, *args: Any, return_type: str = "result",
                 run_id: Optional[str] = None, **kwargs: Any) -> Any:
        parameters = self._bind(args, kwargs)
        engine = self.engine_cls(self, parameters, run_id=run_id)
        state = engine.run()
        return state if return_type == "state" else state.result()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} {self.name}>"


class Flow(_Template):
    is_flow = True

    def __init__(self, fn: Callable, **options: Any):
        super().__init__(fn, **options)
        # Auto-register so the API/UI can list definitions without a separate
        # authoring step — the decorated function *is* the definition.
        from gyrfalcon.flow.registry import register
        register(self)

    @property
    def engine_cls(self):  # type: ignore[override]
        from gyrfalcon.flow.engine import FlowRunEngine
        return FlowRunEngine


class Task(_Template):
    is_task = True

    @property
    def engine_cls(self):  # type: ignore[override]
        from gyrfalcon.flow.engine import TaskRunEngine
        return TaskRunEngine

    def submit(self, *args: Any, wait_for: Any = None, **kwargs: Any):
        from gyrfalcon.flow.futures import submit_task
        return submit_task(self, args, kwargs, wait_for=wait_for)

    def map(self, *args: Any, wait_for: Any = None, **kwargs: Any):
        from gyrfalcon.flow.futures import map_task
        return map_task(self, args, kwargs, wait_for=wait_for)


def _decorator(cls: type[_Template]):
    def decorate(__fn: Optional[Callable] = None, **options: Any):
        if __fn is not None and callable(__fn):
            return cls(__fn)

        def wrapper(fn: Callable) -> _Template:
            return cls(fn, **options)

        return wrapper

    return decorate


flow = _decorator(Flow)
task = _decorator(Task)
