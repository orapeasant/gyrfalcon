"""Quickstart flows — see docs/flow/quickstart.md.

A flow is a decorated Python function; decorating it is what registers it, so
importing this module is the entire "definition" step. Nothing here needs a
database, a schedule, or a running dashboard — every flow below also runs from
a plain `python` shell.
"""

from __future__ import annotations

from gyrfalcon.flow import flow, task
from gyrfalcon.flow.exceptions import Pause
from gyrfalcon.flow.pause import pause_flow_run


# ── 1. The smallest possible flow ─────────────────────────────────────────────
#
# `@flow` alone is enough. Calling it runs it immediately and returns the
# result, exactly like calling the undecorated function would.

@flow
def hello_flow(name: str = "world") -> str:
    return f"Hello, {name}!"


# ── 2. Flow + task, with a retry ──────────────────────────────────────────────
#
# A task is the same idea at finer grain: state is written locally rather than
# proposed to the orchestration layer, so a fan-out of many tasks stays cheap.
# `retries` here is on the TASK, so a single flaky step retries by itself
# instead of re-running the whole flow around it.

@task(retries=2, retry_delay_seconds=1)
def fetch_page(page: int) -> dict:
    # Swap this for a real network/DB call. Left deterministic on purpose so
    # the quickstart has something to run with zero external dependencies.
    return {"page": page, "rows": page * 10}


@flow(retries=1)
def ingest_pages(pages: int = 3) -> list[dict]:
    return [fetch_page(i) for i in range(pages)]


# ── 3. Human-in-the-loop ──────────────────────────────────────────────────────
#
# `pause_flow_run` raises `Pause`; the engine persists a PAUSED state, and a
# human answers it later — from the dashboard's "My Tasks" page, or by calling
# `resume_flow_run(run_id, run_input=...)` directly. The flow is re-entrant
# around the pause: once answered, re-invoking the function returns the answer
# immediately instead of pausing again.

class ApprovalNeededError(Exception):
    """Raised only if a flow calls this outside an engine run (e.g. a plain
    function call in a test) — the real Pause is caught by the engine."""


@flow
def spend_request(amount: float) -> str:
    if amount <= 100:
        return f"Auto-approved: ${amount:.2f}"

    try:
        answer = pause_flow_run(
            key="approve-spend",
            timeout=3600,  # 1 hour — after this the run fails as PauseTimedOut
        )
    except Pause:
        # The engine catches this itself; re-raising here just documents that
        # execution genuinely stops at this line until someone answers.
        raise

    if answer and answer.get("approve"):
        return f"Approved by human: ${amount:.2f}"
    return f"Rejected: ${amount:.2f}"


# ── 4. An agent step (optional — needs a configured model provider) ─────────
#
# Wrapping reasoning in `agent_step` (rather than a plain `@flow`) is what the
# spec calls out specifically: an agent's nondeterminism is quarantined inside
# a subflow with a fixed interface, and it inherits its own retries/timeout
# separate from whatever deterministic flow calls it. This only actually talks
# to a model when *called* — importing/decorating it here is free.

from gyrfalcon.flow.agent import agent_step, model_call  # noqa: E402


@agent_step(retries=1, timeout_seconds=60)
def summarize(text: str) -> str:
    return model_call(f"Summarize in one sentence:\n\n{text}")
