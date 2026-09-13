"""The agentic half.

Spec: §13.3–13.4.

The central rule: **an agent step is a flow run, not a task run.** Flow-run
transitions cross the orchestration boundary, so an agent is cancellable,
pausable and budget-limitable from outside; a flow run can pause for human input;
and its nondeterminism is quarantined in a subgraph whose interface is fixed.

Inside that subflow, one model call is one task, so a flaky API burns one retry
instead of replaying the whole agent loop.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from gyrfalcon.flow import states as st
from gyrfalcon.flow.context import get_flow_run_context
from gyrfalcon.flow.futures import record_encapsulation
from gyrfalcon.flow.states import State
from gyrfalcon.flow.templates import Flow, task

logger = logging.getLogger("gyrfalcon.flow.agent")


class AgentStep(Flow):
    """A subflow wrapping one agent's reasoning.

    A `Flow` rather than a `Task` deliberately — see §13.3. Carries its own
    retries and timeout so the parent's deterministic DAG stays intact.
    """

    #: Rendered by the run graph as an encapsulating node over its tool calls.
    encapsulates_children = True

    def __init__(self, fn: Callable, **options: Any):
        options.setdefault("retries", 1)
        options.setdefault("timeout_seconds", 300)
        super().__init__(fn, **options)

    def __call__(self, *args: Any, return_type: str = "result", **kwargs: Any) -> Any:
        parent = get_flow_run_context()
        result = super().__call__(*args, return_type="state", **kwargs)
        if parent is not None and parent.run_id:
            record_encapsulation(parent.run_id, result.id or "")
        # Record self-encapsulation so a top-level agent step still has a node.
        record_encapsulation(result.id or "", result.id or "")
        return result if return_type == "state" else result.result()


def agent_step(__fn: Optional[Callable] = None, **options: Any):
    """Declare an agent subflow."""
    if __fn is not None and callable(__fn):
        return AgentStep(__fn)

    def wrapper(fn: Callable) -> AgentStep:
        return AgentStep(fn, **options)

    return wrapper


@task(retries=2, retry_delay_seconds=[1, 2, 4])
def model_call(prompt: str, model: Optional[str] = None, **kwargs: Any) -> Any:
    """One model call is one task (§13.3.1).

    Retries, timeout and cache belong here rather than on the agent loop, so a
    flaky provider costs one retry instead of a full replay.
    """
    from gyrfalcon.agent.auxiliary_client import call_llm

    response = call_llm(
        "agent_step",
        messages=[{"role": "user", "content": prompt}],
        model=model,
        **kwargs,
    )
    return response.choices[0].message.content


def validate_output(schema: type, payload: Any, return_type: str = "result") -> Any:
    """The agent's output crosses the boundary validated, or not at all (§13.3.3).

    A validation failure is an ordinary retryable task failure — not a special
    agent error — so it inherits normal retry semantics.
    """
    try:
        validated = schema(**payload) if isinstance(payload, dict) else schema.model_validate(payload)
    except Exception as exc:
        state = st.Failed(
            message=f"agent output failed validation against {schema.__name__}: {exc}",
            exception=exc,
            retriable=True,
        )
        if return_type == "state":
            return state
        raise
    if return_type == "state":
        return st.Completed(data=validated)
    return validated


def run_with_policy(step: AgentStep, *args: Any, policy_name: str = "agent_step", **kwargs: Any) -> State:
    """Execute an agent step with its governance policy applied.

    The policy is data (§4.4), so swapping governance needs no engine change.
    """
    from gyrfalcon.flow.orchestration import LocalOrchestrationClient
    from gyrfalcon.flow.policies import registry

    client = LocalOrchestrationClient(registry.get(policy_name))
    engine = step.engine_cls(step, step._bind(args, kwargs), client=client)
    return engine.run()
