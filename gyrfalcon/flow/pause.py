"""Pause, suspend, resume.

Spec: §8.

`pause` keeps the process alive and blocks in place; `suspend` exits the process
and the run is rescheduled from scratch. For an approval that may take days you
must have the suspend variant, or you pin a process per pending approval —
exactly the shape of an agent step escalating to a human.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from gyrfalcon.flow import states as st
from gyrfalcon.flow.exceptions import Pause
from gyrfalcon.flow.states import State

#: run_id → pending pause record. A real deployment persists this.
_PAUSES: dict[str, dict[str, Any]] = {}
#: run_id → set of pause keys already prompted, for idempotency.
_SEEN_KEYS: dict[str, set[str]] = {}


class ResumeResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    flow_run_id: str
    state: Optional[State] = None
    run_input: Any = None


def should_prompt(pause_key: Optional[str], seen: set[str]) -> bool:
    """A re-entered pause must not re-prompt (§8).

    An unkeyed pause always prompts — without a key there is nothing to
    deduplicate against.
    """
    if pause_key is None:
        return True
    return pause_key not in seen


def build_paused_state(
    timeout: Optional[float] = None,
    reschedule: bool = False,
    pause_key: Optional[str] = None,
    run_input_keyset: Optional[dict[str, str]] = None,
) -> State:
    ctor = st.Suspended if reschedule else st.Paused
    return ctor(
        pause_timeout=timeout,
        pause_key=pause_key,
        pause_reschedule=reschedule,
        run_input_keyset=run_input_keyset,
    )


def _register(run_id: str, state: State, wait_for_input: Any) -> None:
    _PAUSES[run_id] = {
        "state": state,
        "wait_for_input": wait_for_input,
        "paused_at": time.time(),
        "run_input": None,
        "answered": False,
    }
    if state.state_details.pause_key:
        _SEEN_KEYS.setdefault(run_id, set()).add(state.state_details.pause_key)


def pause_flow_run(
    wait_for_input: Any = None,
    timeout: Optional[float] = 3600,
    poll_interval: float = 2.0,
    reschedule: bool = False,
    key: Optional[str] = None,
    flow_run_id: Optional[str] = None,
) -> Any:
    """Block this run until a human answers, or the timeout expires.

    Returns the submitted `RunInput` when one has already been supplied for this
    pause key; otherwise raises `Pause` carrying the PAUSED state, which the
    engine surfaces to the orchestration layer.
    """
    run_id = flow_run_id or _current_run_id()
    seen = _SEEN_KEYS.get(run_id, set())

    record = _PAUSES.get(run_id)
    if record is not None and record.get("answered"):
        return record["run_input"]

    if not should_prompt(key, seen):
        # Already prompted for this key and still unanswered — do not re-prompt.
        raise Pause("awaiting input", state=_PAUSES[run_id]["state"], run_id=run_id)

    keyset = {"input": key} if key else None
    state = build_paused_state(
        timeout=timeout, reschedule=reschedule, pause_key=key, run_input_keyset=keyset
    )
    _register(run_id, state, wait_for_input)
    raise Pause("paused for input", state=state, run_id=run_id)


def suspend_flow_run(
    wait_for_input: Any = None,
    timeout: Optional[float] = 3600,
    key: Optional[str] = None,
    flow_run_id: Optional[str] = None,
) -> Any:
    """Always reschedules; the process exits (§8)."""
    return pause_flow_run(
        wait_for_input=wait_for_input,
        timeout=timeout,
        reschedule=True,
        key=key,
        flow_run_id=flow_run_id,
    )


def resume_flow_run(flow_run_id: str, run_input: Any = None, principal=None,
                    authorize: bool = True) -> ResumeResult:
    """Deliver typed input and mark the run resumable.

    `answered` is tracked explicitly rather than inferred from `run_input`
    being non-None, so an approval with no payload is still distinguishable
    from "nobody has answered yet".

    Raises `NotThePerformerError` unless the caller may answer (§17.6). Pass
    `authorize=False` only for a flow resuming *itself* — never for input
    arriving from outside the process.
    """
    from gyrfalcon.identity import get_principal

    if authorize:
        authorize_answer(flow_run_id, principal)

    record = _PAUSES.setdefault(
        flow_run_id, {"state": None, "wait_for_input": None, "paused_at": time.time()}
    )
    record["run_input"] = run_input
    record["answered"] = True
    # Who approved it. An approval nobody can be attributed to is not
    # auditable, which for a spend-approval flow is the entire point (§17.6).
    answerer = principal or get_principal()
    record["answered_by"] = answerer.user_id if answerer else None
    record["answered_at"] = time.time()
    record["state"] = st.Resuming()
    return ResumeResult(flow_run_id=flow_run_id, state=record["state"], run_input=run_input)


def pause_record(flow_run_id: str) -> Optional[dict[str, Any]]:
    return _PAUSES.get(flow_run_id)


class NotThePerformerError(PermissionError):
    """Someone tried to answer an approval that is not theirs (§17.6)."""


def _run_row(flow_run_id: str):
    """The durable run behind a pause, or None if nothing is persisted.

    Read with a system scope on purpose: this is the lookup that *establishes*
    who may see the run, so it cannot itself be filtered by the caller.
    """
    try:
        from gyrfalcon.flow.db.scope import Scope
        from gyrfalcon.flow.store import get_store

        return get_store().get_run(
            flow_run_id,
            Scope.system(reason="authorization must read the row it is about to judge"),
        )
    except Exception:
        return None


def may_answer(flow_run_id: str, principal=None) -> bool:
    """Whether `principal` is entitled to answer this approval.

    §17.6 makes this the strictest rule in the table, and deliberately not the
    same as "can see the run": an approval answered by whoever happened to
    hold a session token is not an approval, it is a suggestion.

    The rule today is *owner, or an operator in the same tenant*. §16's
    **performer** is the intended final answer — the people a notification was
    addressed to are exactly the people entitled to respond — and this is the
    seam it plugs into. Until performers exist, ownership is the closest
    honest approximation, and it is already far narrower than "anybody".

    With nothing persisted (library use, no store) there is no row to protect
    and no identity to check against, so this allows.
    """
    from gyrfalcon.identity import require_principal

    row = _run_row(flow_run_id)
    if row is None:
        return True
    who = principal or require_principal()
    if row["tenant_id"] != who.tenant_id:
        return False
    return who.is_operator or row["user_id"] == who.user_id


def authorize_answer(flow_run_id: str, principal=None) -> None:
    """`may_answer`, as a guard."""
    if not may_answer(flow_run_id, principal):
        raise NotThePerformerError(
            f"Not entitled to respond to flow run {flow_run_id}. An approval "
            f"may only be answered by the run's owner or an operator in the "
            f"same tenant."
        )


def list_pending(all_tenants: bool = False) -> list[dict[str, Any]]:
    """Pending approval gates — the human-in-the-loop inbox (§8).

    A record is pending once it has been paused and stays so until answered;
    an already-resumed pause carries a non-None run_input and drops off.
    """
    out = []
    for run_id, record in _PAUSES.items():
        if record.get("state") is None or record.get("answered"):
            continue
        # The inbox must not show other people's approvals. It used to show
        # every pause in the process to every caller (§17.1).
        if not all_tenants and not may_answer(run_id):
            continue
        state = record["state"]
        out.append({
            "flow_run_id": run_id,
            "pause_key": state.state_details.pause_key,
            "reschedule": state.state_details.pause_reschedule,
            "timeout": state.state_details.pause_timeout,
            "paused_at": record.get("paused_at"),
            "expired": is_expired(run_id),
        })
    return out


_UNANSWERED = object()


def check_answer(flow_run_id: str) -> Any:
    """The delivered run_input, or the `_UNANSWERED` sentinel if still waiting.

    `None` is a legitimate answer (e.g. a RunInput with all-default fields), so a
    plain `None` return cannot distinguish "not yet answered" from "answered with
    nothing".
    """
    record = _PAUSES.get(flow_run_id)
    if record is None or not record.get("answered"):
        return _UNANSWERED
    return record["run_input"]


def is_expired(flow_run_id: str, now: Optional[float] = None) -> bool:
    """An unanswered approval must not wait forever."""
    record = _PAUSES.get(flow_run_id)
    if not record or record.get("state") is None:
        return False
    timeout = record["state"].state_details.pause_timeout
    if not timeout:
        return False
    return (now or time.time()) - record["paused_at"] > timeout


def _current_run_id() -> str:
    from gyrfalcon.flow.context import get_flow_run_context

    ctx = get_flow_run_context()
    return ctx.run_id if ctx and ctx.run_id else "detached"
