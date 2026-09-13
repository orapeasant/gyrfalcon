"""Orchestration policies — ordered rule lists.

Spec: §4.4, §13.2, §13.4.

Ordering *is* policy, so it lives as data and is swappable. Note
`CacheRetrieval` before `SecureTaskConcurrencySlots`: a cached result does no
work, so a full slot pool must not block it. That single ordering encodes a real
product decision.

`AgentStepPolicy` is the gyrfalcon differentiator — six rules, each an ordinary
`BaseOrchestrationRule`, requiring zero engine changes.
"""

from __future__ import annotations

from typing import Iterable

from gyrfalcon.flow import states as st
from gyrfalcon.flow.orchestration import ANY, BaseOrchestrationRule
from gyrfalcon.flow.states import StateType

# ── core flow rules ───────────────────────────────────────────────────────────

class PreventDuplicateTransitions(BaseOrchestrationRule):
    FROM_STATES = [ANY]
    TO_STATES = [ANY]

    async def before_transition(self, initial, proposed, ctx):
        if initial is not None and proposed is not None:
            if initial.type is proposed.type and initial.name == proposed.name:
                if ctx.get("reject_duplicates"):
                    self.abort_transition("duplicate transition")


class HandleFlowTerminalStateTransitions(BaseOrchestrationRule):
    """A terminal state is immutable unless the transition is forced."""

    FROM_STATES = [StateType.COMPLETED, StateType.FAILED, StateType.CANCELLED, StateType.CRASHED]
    TO_STATES = [ANY]

    async def before_transition(self, initial, proposed, ctx):
        if not ctx.get("force"):
            self.abort_transition(f"run is already terminal ({initial.name})")


class EnforceCancellingToCancelledTransition(BaseOrchestrationRule):
    FROM_STATES = [StateType.CANCELLING]
    TO_STATES = [StateType.COMPLETED, StateType.FAILED, StateType.RUNNING]

    async def before_transition(self, initial, proposed, ctx):
        self.abort_transition("run is cancelling; only Cancelled is reachable")


class WaitForScheduledTime(BaseOrchestrationRule):
    """Answer WAIT until a scheduled state's time arrives."""

    FROM_STATES = [ANY]
    TO_STATES = [StateType.RUNNING]

    async def before_transition(self, initial, proposed, ctx):
        scheduled = (initial.state_details.scheduled_time if initial else None)
        now = ctx.get("now")
        if scheduled and now is not None and now < scheduled:
            self.delay_transition(scheduled - now, "scheduled time not reached")


class SecureFlowConcurrencySlots(BaseOrchestrationRule):
    FROM_STATES = [ANY]
    TO_STATES = [StateType.RUNNING]

    async def before_transition(self, initial, proposed, ctx):
        limit = ctx.get("concurrency_limit")
        if limit is not None and ctx.get("active_runs", 0) >= limit:
            self.reject_transition(st.AwaitingConcurrencySlot(), "concurrency limit reached")
        elif limit is not None:
            ctx["_slot_held"] = True

    async def cleanup(self, initial, validated, ctx):
        ctx.pop("_slot_held", None)


class ReleaseFlowConcurrencySlots(BaseOrchestrationRule):
    FROM_STATES = [StateType.RUNNING]
    TO_STATES = [StateType.COMPLETED, StateType.FAILED, StateType.CANCELLED, StateType.CRASHED]

    async def after_transition(self, initial, validated, ctx):
        ctx.pop("_slot_held", None)


class RetryFailedFlows(BaseOrchestrationRule):
    """Server-side retry injection — the client never planned this."""

    FROM_STATES = [StateType.RUNNING]
    TO_STATES = [StateType.FAILED]

    async def before_transition(self, initial, proposed, ctx):
        retries = ctx.get("retries", 0)
        max_retries = ctx.get("max_retries", 0)
        if retries < max_retries:
            self.reject_transition(st.AwaitingRetry(), "server-injected retry")


class InstrumentFlowRunStateTransitions(BaseOrchestrationRule):
    FROM_STATES = [ANY]
    TO_STATES = [ANY]

    async def after_transition(self, initial, validated, ctx):
        emit = ctx.get("emit")
        if callable(emit):
            emit({"event": "gyrfalcon.flow_run.state_change", "to": validated.name})


# ── core task rules ───────────────────────────────────────────────────────────

class CacheRetrieval(BaseOrchestrationRule):
    """A cache hit short-circuits everything — so it runs first."""

    FROM_STATES = [ANY]
    TO_STATES = [StateType.RUNNING]

    async def before_transition(self, initial, proposed, ctx):
        from gyrfalcon.flow.cache import CACHE

        key = ctx.get("cache_key")
        if key and key in CACHE:
            self.reject_transition(st.Cached(data=CACHE[key], cache_key=key), "cache hit")


class SecureTaskConcurrencySlots(BaseOrchestrationRule):
    FROM_STATES = [ANY]
    TO_STATES = [StateType.RUNNING]

    async def before_transition(self, initial, proposed, ctx):
        limit = ctx.get("task_concurrency_limit")
        if limit is not None and ctx.get("active_tasks", 0) >= limit:
            self.reject_transition(st.AwaitingConcurrencySlot(), "task concurrency limit")


class HandleTaskTerminalStateTransitions(HandleFlowTerminalStateTransitions):
    ...


class RetryFailedTasks(RetryFailedFlows):
    ...


class RenameReruns(BaseOrchestrationRule):
    FROM_STATES = [ANY]
    TO_STATES = [StateType.RUNNING]

    async def before_transition(self, initial, proposed, ctx):
        if ctx.get("retries", 0) > 0:
            self.rename_state("Retrying")


class CacheInsertion(BaseOrchestrationRule):
    FROM_STATES = [StateType.RUNNING]
    TO_STATES = [StateType.COMPLETED]

    async def after_transition(self, initial, validated, ctx):
        from gyrfalcon.flow.cache import CACHE

        key = ctx.get("cache_key")
        if key:
            CACHE[key] = validated.data


class ReleaseTaskConcurrencySlots(ReleaseFlowConcurrencySlots):
    ...


class CopyScheduledTime(BaseOrchestrationRule):
    FROM_STATES = [StateType.SCHEDULED]
    TO_STATES = [ANY]

    async def before_transition(self, initial, proposed, ctx):
        if initial.state_details.scheduled_time and not proposed.state_details.scheduled_time:
            proposed.state_details.scheduled_time = initial.state_details.scheduled_time


# ── agent rules (§13.4) — each ~40 lines, zero engine changes ─────────────────

class EnforceTokenBudget(BaseOrchestrationRule):
    FROM_STATES = [ANY]
    TO_STATES = [ANY]

    async def before_transition(self, initial, proposed, ctx):
        budget = ctx.get("token_budget")
        spent = ctx.get("tokens_spent", 0)
        if budget is not None and spent > budget:
            self.abort_transition(f"token budget exhausted: {spent} > {budget}")


class RequireApprovalAboveCost(BaseOrchestrationRule):
    """The gate between an agent's proposal and an expensive/irreversible action."""

    FROM_STATES = [ANY]
    TO_STATES = [ANY]

    async def before_transition(self, initial, proposed, ctx):
        threshold = ctx.get("approval_threshold")
        cost = ctx.get("estimated_cost")
        if threshold is not None and cost is not None and cost > threshold:
            self.reject_transition(
                st.Paused(pause_timeout=ctx.get("approval_timeout", 3600)),
                f"estimated cost {cost} exceeds approval threshold {threshold}",
            )


class DetectToolLoop(BaseOrchestrationRule):
    FROM_STATES = [ANY]
    TO_STATES = [ANY]

    #: A cycle must repeat at least this many times before it counts as a loop.
    MIN_REPEATS = 3

    async def before_transition(self, initial, proposed, ctx):
        calls = list(ctx.get("recent_tool_calls") or [])
        if len(calls) < self.MIN_REPEATS * 2:
            return
        for size in range(1, len(calls) // self.MIN_REPEATS + 1):
            cycle = calls[-size:]
            repeats = 1
            idx = len(calls) - size
            while idx - size >= 0 and calls[idx - size:idx] == cycle:
                repeats += 1
                idx -= size
            if repeats >= self.MIN_REPEATS:
                self.abort_transition(f"tool loop detected: {cycle} x{repeats}")
                return


class CapAgentIterations(BaseOrchestrationRule):
    FROM_STATES = [ANY]
    TO_STATES = [ANY]

    async def before_transition(self, initial, proposed, ctx):
        max_iterations = ctx.get("max_iterations")
        iterations = ctx.get("iterations", 0)
        if max_iterations is not None and iterations >= max_iterations:
            self.reject_transition(
                st.Failed(name="IterationCapReached"),
                f"iteration cap {max_iterations} reached",
            )
            # Failed() names itself; force the observable label.
            self._t.proposed = self._t.proposed.model_copy(
                update={"name": "IterationCapReached"}
            )


class RouteToStrongerModel(BaseOrchestrationRule):
    FROM_STATES = [ANY]
    TO_STATES = [ANY]

    async def before_transition(self, initial, proposed, ctx):
        escalate_after = ctx.get("escalate_after")
        retries = ctx.get("retries", 0)
        if escalate_after is not None and retries > escalate_after:
            self.rename_state("EscalatingModel")
            ctx["model"] = ctx.get("stronger_model", "escalated")


class RecordAgentDecision(BaseOrchestrationRule):
    FROM_STATES = [ANY]
    TO_STATES = [ANY]

    async def after_transition(self, initial, validated, ctx):
        emit = ctx.get("emit")
        if callable(emit):
            emit({
                "event": "gyrfalcon.agent.state_change",
                "from": initial.name if initial else None,
                "to": validated.name if validated else None,
            })


# ── policies ──────────────────────────────────────────────────────────────────

class _Policy:
    rules: list[type[BaseOrchestrationRule]] = []

    @classmethod
    def priority(cls) -> list[type[BaseOrchestrationRule]]:
        return list(cls.rules)


class CoreFlowPolicy(_Policy):
    rules = [
        PreventDuplicateTransitions,
        HandleFlowTerminalStateTransitions,
        EnforceCancellingToCancelledTransition,
        SecureFlowConcurrencySlots,
        CopyScheduledTime,
        WaitForScheduledTime,
        RetryFailedFlows,
        InstrumentFlowRunStateTransitions,
        ReleaseFlowConcurrencySlots,
    ]


class CoreTaskPolicy(_Policy):
    rules = [
        CacheRetrieval,                 # first: a hit short-circuits everything
        HandleTaskTerminalStateTransitions,
        SecureTaskConcurrencySlots,     # after CacheRetrieval, deliberately
        CopyScheduledTime,
        WaitForScheduledTime,
        RetryFailedTasks,
        RenameReruns,
        CacheInsertion,
        ReleaseTaskConcurrencySlots,
    ]


class MinimalTaskPolicy(_Policy):
    rules = [HandleTaskTerminalStateTransitions]


class AgentStepPolicy(_Policy):
    """Agent governance, expressed entirely as ordinary rules."""

    rules = [
        EnforceTokenBudget,
        RequireApprovalAboveCost,
        DetectToolLoop,
        CapAgentIterations,
        RouteToStrongerModel,
        RecordAgentDecision,
    ]


class PolicyRegistry:
    """First-class registration — the best extension point, exposed deliberately."""

    def __init__(self) -> None:
        self._policies: dict[str, list[type[BaseOrchestrationRule]]] = {
            "core_flow": CoreFlowPolicy.priority(),
            "core_task": CoreTaskPolicy.priority(),
            "minimal_task": MinimalTaskPolicy.priority(),
            "agent_step": AgentStepPolicy.priority(),
        }

    def register(self, name: str, rules: Iterable[type[BaseOrchestrationRule]]) -> None:
        self._policies[name] = list(rules)

    def get(self, name: str) -> list[type[BaseOrchestrationRule]]:
        return list(self._policies[name])

    def names(self) -> list[str]:
        return sorted(self._policies)


registry = PolicyRegistry()
