"""Flow engine exceptions.

Spec: §3.4 (engine-owned timeouts must be distinguishable from user ones),
§4.2 (Pause/Abort as control-flow signals), §5.3–5.4.
"""

from __future__ import annotations

from typing import Any, Optional


class FlowError(Exception):
    """Base for everything this package raises."""


class MissingContextError(FlowError):
    """Raised by get_run_context() outside a run."""


class UpstreamTaskError(FlowError):
    """An upstream dependency is not ready. Blocked, not broken."""


class MappingMissingIterable(FlowError):
    """`map` was called with nothing to iterate over."""


class _EngineTimeoutError(FlowError):
    """Engine-enforced timeout.

    Deliberately not a subclass of the builtin TimeoutError: 'we killed it' must
    never be confused with 'user code raised the same class'.
    """


class FlowRunTimeoutError(_EngineTimeoutError):
    ...


class TaskRunTimeoutError(_EngineTimeoutError):
    ...


class Pause(FlowError):
    """Raised when the server counter-proposes a PAUSED state, or when user code
    calls pause_flow_run()/suspend_flow_run().

    `run_id` is the id the pause was actually filed under. `pause_flow_run`
    accepts an explicit `flow_run_id` override that can differ from the calling
    engine's own run id (e.g. an external system pausing on a caller-supplied
    correlation id); carrying that id here — rather than letting the engine
    assume its own run_id — is what lets the engine poll the right record. Get
    this wrong and the engine polls a key nobody ever answers: no timeout ever
    fires for it either, since that check keys off the same id, so the run hangs
    forever rather than failing after its stated pause_timeout.
    """

    def __init__(self, message: str = "", state: Any = None, run_id: Optional[str] = None):
        super().__init__(message)
        self.state = state
        self.run_id = run_id


class Abort(FlowError):
    """Raised on ABORT. Stop entirely — do not retry."""

    def __init__(self, reason: str = ""):
        super().__init__(reason)
        self.reason = reason


class TerminationSignal(FlowError):
    def __init__(self, signal: Optional[int] = None):
        super().__init__(f"signal {signal}")
        self.signal = signal
