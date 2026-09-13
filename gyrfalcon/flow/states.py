"""State model — the closed type enum and the open name vocabulary.

Spec: 15-flow.md §1.3, §2.1–2.3.

`type` is a closed enum the orchestration layer switches on; `name` is an open
string for humans. That split is what lets agentic vocabulary ("Deliberating",
"AwaitingTool") exist without touching the state machine.
"""

from __future__ import annotations

import enum
import uuid
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class StateType(str, enum.Enum):
    SCHEDULED = "SCHEDULED"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    CANCELLING = "CANCELLING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    CRASHED = "CRASHED"


#: Membership here is the definition of `is_final()`; nothing else encodes it.
TERMINAL_STATES: frozenset[StateType] = frozenset({
    StateType.COMPLETED,
    StateType.FAILED,
    StateType.CANCELLED,
    StateType.CRASHED,
})


class StateDetails(BaseModel):
    """Sidecar for transition metadata.

    Deliberately `extra="allow"`: new orchestration features land here without a
    migration on `State` itself.
    """

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    scheduled_time: Optional[float] = None
    cache_key: Optional[str] = None
    cache_expiration: Optional[float] = None
    pause_timeout: Optional[float] = None
    pause_reschedule: bool = False
    pause_key: Optional[str] = None
    run_input_keyset: Optional[dict[str, str]] = None
    retriable: Optional[bool] = None
    traceparent: Optional[str] = None
    deployment_concurrency_lease_id: Optional[str] = None
    child_flow_run_id: Optional[str] = None


class State(BaseModel):
    """A run's position. Only runs have states; templates never do."""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    type: StateType
    name: Optional[str] = None
    timestamp: Optional[float] = None
    message: Optional[str] = None
    data: Any = None
    state_details: StateDetails = Field(default_factory=StateDetails)
    id: Optional[str] = None

    # Set by the engine when user code raised; kept off `data` so a failure
    # carrying a value stays distinguishable from a success.
    exception: Any = None

    def is_final(self) -> bool:
        return self.type in TERMINAL_STATES

    def is_completed(self) -> bool:
        return self.type is StateType.COMPLETED

    def is_failed(self) -> bool:
        return self.type is StateType.FAILED

    def is_crashed(self) -> bool:
        return self.type is StateType.CRASHED

    def is_running(self) -> bool:
        return self.type is StateType.RUNNING

    def is_paused(self) -> bool:
        return self.type is StateType.PAUSED

    def is_scheduled(self) -> bool:
        return self.type is StateType.SCHEDULED

    def is_pending(self) -> bool:
        return self.type is StateType.PENDING

    def is_cancelled(self) -> bool:
        return self.type is StateType.CANCELLED

    def result(self, raise_on_failure: bool = True) -> Any:
        """Unwrap into a value, or raise what the run raised."""
        from gyrfalcon.flow.results import ResultRecordMetadata, resolve_result

        if self.type in (StateType.FAILED, StateType.CRASHED) and raise_on_failure:
            raise self.exception if self.exception is not None else RuntimeError(
                self.message or "run failed"
            )
        if isinstance(self.data, ResultRecordMetadata):
            return resolve_result(self.data)
        return self.data

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.name or self.type.value}(type={self.type.value})"


def _make(type_: StateType, default_name: str, **kwargs: Any) -> State:
    """Build a state, letting callers override the display name.

    The override matters: a rule producing `Failed(name="IterationCapReached")`
    is the type-vs-name split doing its job.
    """
    name = kwargs.pop("name", None) or default_name
    details = kwargs.pop("state_details", None) or StateDetails()
    for key in list(kwargs):
        if key in StateDetails.model_fields:
            setattr(details, key, kwargs.pop(key))
    return State(type=type_, name=name, state_details=details, **kwargs)


# ── SCHEDULED ────────────────────────────────────────────────────────────────

def Scheduled(**kw: Any) -> State:
    return _make(StateType.SCHEDULED, "Scheduled", **kw)


def Late(**kw: Any) -> State:
    """Scheduled time passed without the run becoming Pending."""
    return _make(StateType.SCHEDULED, "Late", **kw)


def AwaitingRetry(**kw: Any) -> State:
    """Failed, retries remain, waiting out the delay. Survives a restart."""
    return _make(StateType.SCHEDULED, "AwaitingRetry", **kw)


def AwaitingConcurrencySlot(**kw: Any) -> State:
    return _make(StateType.SCHEDULED, "AwaitingConcurrencySlot", **kw)


def Resuming(**kw: Any) -> State:
    return _make(StateType.SCHEDULED, "Resuming", **kw)


# ── PENDING ──────────────────────────────────────────────────────────────────

def Pending(**kw: Any) -> State:
    return _make(StateType.PENDING, "Pending", **kw)


def Submitting(**kw: Any) -> State:
    return _make(StateType.PENDING, "Submitting", **kw)


def InfrastructurePending(**kw: Any) -> State:
    return _make(StateType.PENDING, "InfrastructurePending", **kw)


def NotReady(**kw: Any) -> State:
    """Blocked on an upstream, which is visibly distinct from broken (§5.4)."""
    return _make(StateType.PENDING, "NotReady", **kw)


# ── RUNNING ──────────────────────────────────────────────────────────────────

def Running(**kw: Any) -> State:
    return _make(StateType.RUNNING, "Running", **kw)


def Retrying(**kw: Any) -> State:
    """Executing after a prior failure — RUNNING, not a separate type."""
    return _make(StateType.RUNNING, "Retrying", **kw)


# ── PAUSED ───────────────────────────────────────────────────────────────────

def Paused(**kw: Any) -> State:
    """Stopped with the process alive."""
    kw.setdefault("pause_reschedule", False)
    return _make(StateType.PAUSED, "Paused", **kw)


def Suspended(**kw: Any) -> State:
    """Stopped with the process exited — what makes a three-day approval affordable."""
    kw.setdefault("pause_reschedule", True)
    return _make(StateType.PAUSED, "Suspended", **kw)


# ── CANCELLING / CANCELLED ───────────────────────────────────────────────────

def Cancelling(**kw: Any) -> State:
    """Teardown in progress. Non-terminal: cancellation is two-phase."""
    return _make(StateType.CANCELLING, "Cancelling", **kw)


def Cancelled(**kw: Any) -> State:
    return _make(StateType.CANCELLED, "Cancelled", **kw)


# ── COMPLETED ────────────────────────────────────────────────────────────────

def Completed(**kw: Any) -> State:
    return _make(StateType.COMPLETED, "Completed", **kw)


def Cached(**kw: Any) -> State:
    return _make(StateType.COMPLETED, "Cached", **kw)


def RolledBack(**kw: Any) -> State:
    """Succeeded, then had its effects undone. Honestly a success."""
    return _make(StateType.COMPLETED, "RolledBack", **kw)


# ── FAILED / CRASHED ─────────────────────────────────────────────────────────

def Failed(**kw: Any) -> State:
    """Your code raised."""
    return _make(StateType.FAILED, "Failed", **kw)


def TimedOut(**kw: Any) -> State:
    """The engine killed it — never confused with a user TimeoutError."""
    return _make(StateType.FAILED, "TimedOut", **kw)


def Crashed(**kw: Any) -> State:
    """The world broke: process killed, OOM, node lost."""
    return _make(StateType.CRASHED, "Crashed", **kw)


def new_run_id() -> str:
    return uuid.uuid4().hex
