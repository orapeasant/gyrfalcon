"""Argument-position annotations.

Spec: §5.3 — the parallelism vocabulary lives in the argument position, not in a
separate API. `t.map(items, config=unmapped(cfg))` reads better than a
partitioning callback.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

T = TypeVar("T")


class _Annotation(Generic[T]):
    __slots__ = ("value",)

    def __init__(self, value: T):
        self.value = value

    def unwrap(self) -> T:
        return self.value

    def __repr__(self) -> str:  # pragma: no cover
        return f"{type(self).__name__}({self.value!r})"


class unmapped(_Annotation[T]):
    """Broadcast to every child instead of iterating over it."""


class allow_failure(_Annotation[T]):
    """Pass the upstream result through even if it failed."""


class quote(_Annotation[T]):
    """Do not resolve or introspect this value at all."""


class opaque(_Annotation[T]):
    """Treat as an opaque blob for dependency purposes."""


def resolve_argument(value: Any) -> Any:
    """Resolve futures and annotations into concrete call arguments."""
    from gyrfalcon.flow.futures import BaseFuture

    if isinstance(value, quote):
        return value.unwrap()
    if isinstance(value, allow_failure):
        inner = value.unwrap()
        if isinstance(inner, BaseFuture):
            state = inner.wait()
            # The point of allow_failure: hand over the failure instead of raising.
            return state.exception if state.is_failed() or state.is_crashed() else state.result()
        return inner
    if isinstance(value, (unmapped, opaque)):
        return resolve_argument(value.unwrap())
    if isinstance(value, BaseFuture):
        return value.result()
    return value
