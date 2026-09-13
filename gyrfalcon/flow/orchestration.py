"""The orchestration boundary.

Spec: §4.1–4.4. "This is the most important section for gyrfalcon."

The client proposes a state; the server answers ACCEPT / REJECT / ABORT / WAIT.
REJECT is the subtle one: the server does not merely veto, it counter-proposes,
and the client adopts the counter-proposal as its new reality. That is how a
server-side retry, a cache hit, or an injected pause reaches a client that never
asked for one.
"""

from __future__ import annotations

import asyncio
import enum
import time
from contextlib import AsyncExitStack
from typing import Any, Callable, Iterable, Optional, Sequence

from pydantic import BaseModel, ConfigDict

from gyrfalcon.flow import states as st
from gyrfalcon.flow.exceptions import Abort, Pause
from gyrfalcon.flow.states import State

#: Sentinel accepted in FROM_STATES/TO_STATES meaning "any state type".
ANY = "*"


class SetStateStatus(str, enum.Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    ABORT = "ABORT"
    WAIT = "WAIT"


class SetStateResponse(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    status: SetStateStatus
    state: Optional[State] = None
    details: dict[str, Any] = {}


def propose_state(
    client: Any,
    run_id: str,
    state: State,
    force: bool = False,
    sleep: Callable[[float], Any] = time.sleep,
    max_waits: int = 100,
) -> State:
    """Propose a state and resolve the four-way answer.

    WAIT loops internally. ACCEPT adopts the server's identity. REJECT adopts the
    server's substituted state — raising `Pause` if that state is PAUSED. ABORT
    raises and never re-proposes.
    """
    waits = 0
    while True:
        response = client.set_state(run_id, state, force=force)

        if response.status is SetStateStatus.ACCEPT:
            served = response.state
            if served is not None:
                state.id = served.id if served.id is not None else state.id
                if served.timestamp is not None:
                    state.timestamp = served.timestamp
                if served.state_details is not None:
                    state.state_details = served.state_details
            return state

        if response.status is SetStateStatus.ABORT:
            raise Abort(response.details.get("reason", ""))

        if response.status is SetStateStatus.REJECT:
            substituted = response.state
            if substituted is not None and substituted.is_paused():
                raise Pause(response.details.get("reason", "paused"), state=substituted)
            return substituted if substituted is not None else state

        # WAIT
        waits += 1
        if waits > max_waits:
            raise Abort("exceeded maximum orchestration waits")
        sleep(response.details.get("delay_seconds", 0))


# ── Rules ─────────────────────────────────────────────────────────────────────

class OrchestrationResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    status: SetStateStatus = SetStateStatus.ACCEPT
    state: Optional[State] = None
    details: dict[str, Any] = {}


class _TransitionContext:
    """Mutable transition being negotiated, shared across nested rules."""

    def __init__(self, initial: State, proposed: State, ctx: dict[str, Any]):
        self.initial = initial
        self.proposed = proposed
        self.ctx = ctx
        self.status = SetStateStatus.ACCEPT
        self.details: dict[str, Any] = {}
        self.aborted = False


class BaseOrchestrationRule:
    """A rule is a context manager over one transition.

    `before_transition` may mutate the proposal or produce a side effect;
    `after_transition` acts on what committed; `cleanup` compensates a side
    effect when the transition dies. Because rules nest, a rule that acquired a
    slot in `before_transition` releases it in `cleanup` even if an inner rule
    aborted.
    """

    FROM_STATES: Sequence[Any] = ()
    TO_STATES: Sequence[Any] = ()

    def __init__(self, transition: _TransitionContext):
        self._t = transition
        self._entered = False

    # -- gating -------------------------------------------------------------
    @classmethod
    def _matches(cls, gate: Sequence[Any], state: Optional[State]) -> bool:
        if ANY in gate:
            return True
        if state is None:
            return False
        return state.type in gate

    @classmethod
    def applies_to(cls, initial: Optional[State], proposed: Optional[State]) -> bool:
        return cls._matches(cls.FROM_STATES, initial) and cls._matches(cls.TO_STATES, proposed)

    # -- verbs --------------------------------------------------------------
    def reject_transition(self, state: Optional[State], reason: str = "") -> None:
        self._t.status = SetStateStatus.REJECT
        if state is not None:
            self._t.proposed = state
        self._t.details["reason"] = reason

    def delay_transition(self, delay_seconds: float, reason: str = "") -> None:
        self._t.status = SetStateStatus.WAIT
        self._t.details.update({"delay_seconds": delay_seconds, "reason": reason})

    def abort_transition(self, reason: str = "") -> None:
        self._t.status = SetStateStatus.ABORT
        self._t.details["reason"] = reason
        self._t.aborted = True

    def rename_state(self, name: str) -> None:
        """Keep the type; change the label."""
        self._t.proposed = self._t.proposed.model_copy(update={"name": name})

    # -- hooks --------------------------------------------------------------
    async def before_transition(self, initial, proposed, ctx) -> None:  # noqa: D401
        ...

    async def after_transition(self, initial, validated, ctx) -> None:
        ...

    async def cleanup(self, initial, validated, ctx) -> None:
        ...

    async def __aenter__(self) -> "BaseOrchestrationRule":
        await self.before_transition(self._t.initial, self._t.proposed, self._t.ctx)
        self._entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._t.aborted or self._t.status is SetStateStatus.ABORT:
            await self.cleanup(self._t.initial, self._t.proposed, self._t.ctx)
        else:
            await self.after_transition(self._t.initial, self._t.proposed, self._t.ctx)
        return False


async def run_rules_async(
    rules: Iterable[type[BaseOrchestrationRule]],
    initial: State,
    proposed: State,
    ctx: Optional[dict[str, Any]] = None,
) -> OrchestrationResult:
    transition = _TransitionContext(initial, proposed, ctx or {})

    async with AsyncExitStack() as stack:
        for rule_cls in rules:
            if not rule_cls.applies_to(transition.initial, transition.proposed):
                continue
            await stack.enter_async_context(rule_cls(transition))
            if transition.status is SetStateStatus.ABORT:
                # Stop entering further rules; already-entered ones still clean up.
                break

    return OrchestrationResult(
        status=transition.status,
        state=transition.proposed,
        details=transition.details,
    )


def run_rules(
    rules: Iterable[type[BaseOrchestrationRule]],
    initial: State,
    proposed: State,
    ctx: Optional[dict[str, Any]] = None,
) -> OrchestrationResult:
    """Synchronous wrapper — rules are async so side effects can do I/O."""
    return asyncio.run(run_rules_async(rules, initial, proposed, ctx))


class LocalOrchestrationClient:
    """In-process server stand-in: evaluates a policy and answers the four ways."""

    def __init__(self, policy: Optional[Iterable[type[BaseOrchestrationRule]]] = None):
        self.policy = list(policy or [])
        self.history: list[tuple[str, State]] = []
        self.context: dict[str, Any] = {}

    def set_state(self, run_id: str, state: State, force: bool = False) -> SetStateResponse:
        previous = self.history[-1][1] if self.history else st.Pending()
        if force or not self.policy:
            self.history.append((run_id, state))
            return SetStateResponse(status=SetStateStatus.ACCEPT, state=state)

        result = run_rules(self.policy, previous, state, self.context)
        if result.status is SetStateStatus.ACCEPT and result.state is not None:
            self.history.append((run_id, result.state))
        return SetStateResponse(
            status=result.status, state=result.state, details=result.details
        )
