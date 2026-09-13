"""Cache policies.

Spec: §6.2.

Policies compose with `+` — that one operator is the whole ergonomic story.
`DEFAULT` includes a source-code hash, which is a strong opinion and the correct
one: editing a function should invalidate its cache.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from typing import Any, Optional, Sequence

logger = logging.getLogger("gyrfalcon.flow.cache")


def _hash(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8", errors="replace"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _stable(value: Any) -> str:
    """Deterministic rendering — unsorted dicts must not change a key."""
    try:
        return json.dumps(value, sort_keys=True, default=repr)
    except Exception:
        return repr(value)


class CachePolicy:
    def compute_key(
        self,
        task_ctx: Any,
        inputs: Optional[dict],
        flow_parameters: Optional[dict],
        **kwargs: Any,
    ) -> Optional[str]:
        raise NotImplementedError

    def __add__(self, other: "CachePolicy") -> "CompoundCachePolicy":
        left = list(self.policies) if isinstance(self, CompoundCachePolicy) else [self]
        right = list(other.policies) if isinstance(other, CompoundCachePolicy) else [other]
        return CompoundCachePolicy(left + right)

    def __repr__(self) -> str:  # pragma: no cover
        return f"{type(self).__name__}()"


class CompoundCachePolicy(CachePolicy):
    def __init__(self, policies: Sequence[CachePolicy]):
        self.policies = list(policies)

    def compute_key(self, task_ctx, inputs, flow_parameters, **kwargs) -> Optional[str]:
        parts: list[str] = []
        for policy in self.policies:
            key = policy.compute_key(task_ctx, inputs, flow_parameters, **kwargs)
            if key is None:
                # A NO_CACHE anywhere in the compound disables the whole thing.
                return None
            parts.append(key)
        return _hash(*parts) if parts else None


class Inputs(CachePolicy):
    def compute_key(self, task_ctx, inputs, flow_parameters, **kwargs) -> Optional[str]:
        return _hash("inputs", _stable(inputs or {}))


class TaskSource(CachePolicy):
    def compute_key(self, task_ctx, inputs, flow_parameters, **kwargs) -> Optional[str]:
        fn = getattr(getattr(task_ctx, "task", None), "fn", None)
        try:
            source = inspect.getsource(fn) if fn is not None else ""
        except (OSError, TypeError):
            source = repr(fn)
        return _hash("source", source)


class RunId(CachePolicy):
    def compute_key(self, task_ctx, inputs, flow_parameters, **kwargs) -> Optional[str]:
        run_id = getattr(task_ctx, "run_id", None) or ""
        return _hash("run_id", str(run_id))


class FlowParameters(CachePolicy):
    def compute_key(self, task_ctx, inputs, flow_parameters, **kwargs) -> Optional[str]:
        return _hash("flow_parameters", _stable(flow_parameters or {}))


class NoCache(CachePolicy):
    def compute_key(self, task_ctx, inputs, flow_parameters, **kwargs) -> Optional[str]:
        return None


INPUTS = Inputs()
TASK_SOURCE = TaskSource()
RUN_ID = RunId()
FLOW_PARAMETERS = FlowParameters()
NO_CACHE = NoCache()
DEFAULT = INPUTS + TASK_SOURCE + RUN_ID


def compute_transaction_key(
    policy: Optional[CachePolicy],
    task_ctx: Any,
    inputs: Optional[dict],
    flow_parameters: Optional[dict] = None,
    **kwargs: Any,
) -> Optional[str]:
    """Never fail a run over a cache key.

    A policy that raises logs and degrades to "don't cache", which is the correct
    failure mode (§6.2).
    """
    if policy is None:
        return None
    try:
        return policy.compute_key(task_ctx, inputs, flow_parameters, **kwargs)
    except Exception:
        logger.warning("cache policy %r raised; not caching", policy, exc_info=True)
        return None


#: cache key → result, populated by CacheInsertion and read by CacheRetrieval.
CACHE: dict[str, Any] = {}
