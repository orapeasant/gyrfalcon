"""Human-in-the-loop approval — pausing a turn until a person answers.

Spec 18-slack.md D3. `tools/approval.py` has long decided *whether* a command
needs approval; nothing ever asked anyone. The tool returned
`{"approval_required": true}` to the **model**, which was free to rephrase the
command and try again — a guardrail against accident, not against an adversary.
This is the missing half: the question reaches a person, and the answer decides.

It is a gateway capability rather than a Slack feature because the CLI, TUI and
dashboard have exactly the same hole; an adapter only has to say how to *ask*.

The mechanics are a thread boundary in the opposite direction to everything
else here. A tool call happens deep inside `run_conversation`, on the turn's
worker thread. The question has to be posted by the event loop, and the answer
arrives on the event loop (a button click, or a message). So the worker blocks
on an `Event` while the loop does both, and a timeout guarantees the worker is
released even if nobody ever answers.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("gateway.approval")

#: How long a question waits before it is treated as a refusal. Long enough to
#: notice a notification and answer, short enough that a forgotten prompt does
#: not hold a worker thread — and a slot in the turn pool — indefinitely.
DEFAULT_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class ApprovalRequest:
    """One dangerous thing, waiting on a person."""
    tool: str
    reason: str
    command: str = ""
    session_key: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)

    def summary(self) -> str:
        why = self.reason.split(":", 1)[-1].strip() or "needs approval"
        return f"{why} (`{self.tool}`)"


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    #: Who decided. Empty when nobody did — a timeout, or nowhere to ask.
    by: str = ""
    detail: str = ""

    def __bool__(self) -> bool:
        return self.approved


#: Posts the question wherever the conversation is happening. Returns True if
#: it managed to ask; False means there is nobody to ask and the request is
#: refused rather than left hanging.
Asker = Callable[[ApprovalRequest], Awaitable[bool]]


class _Pending:
    __slots__ = ("request", "event", "decision")

    def __init__(self, request: ApprovalRequest):
        self.request = request
        self.event = threading.Event()
        self.decision: Optional[ApprovalDecision] = None


class ApprovalBroker:
    """Carries a question from a worker thread to a person and back."""

    def __init__(self, loop: asyncio.AbstractEventLoop, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self._loop = loop
        self._timeout = timeout
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._askers: dict[str, Asker] = {}

    # -- event loop side ------------------------------------------------------

    def register_asker(self, session_key: str, asker: Asker) -> None:
        with self._lock:
            self._askers[session_key] = asker

    def unregister_asker(self, session_key: str) -> None:
        with self._lock:
            self._askers.pop(session_key, None)

    def pending_for(self, session_key: str) -> list[ApprovalRequest]:
        with self._lock:
            return [p.request for p in self._pending.values() if p.request.session_key == session_key]

    def resolve(self, request_id: str, approved: bool, by: str = "") -> Optional[ApprovalRequest]:
        """Answer one question. Returns the request, or None if it is not open.

        None covers the cases that matter: a button clicked twice, a button
        clicked after the question timed out, and a stale message from days ago.
        The caller reports it rather than silently doing nothing.
        """
        with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            return None
        pending.decision = ApprovalDecision(approved, by=by, detail="answered")
        pending.event.set()
        return pending.request

    def resolve_latest(self, session_key: str, approved: bool, by: str = "") -> Optional[ApprovalRequest]:
        """Answer this conversation's most recent open question — what `!approve`
        means when there are no buttons to click."""
        with self._lock:
            candidates = [p for p in self._pending.values() if p.request.session_key == session_key]
            newest = max(candidates, key=lambda p: p.request.created_at, default=None)
            request_id = newest.request.id if newest else None
        return self.resolve(request_id, approved, by) if request_id else None

    def cancel_for(self, session_key: str, detail: str = "cancelled") -> int:
        """Release every question in a conversation — `!stop`, or shutdown.
        Cancelled means refused: a paused tool must never run unapproved."""
        with self._lock:
            ids = [i for i, p in self._pending.items() if p.request.session_key == session_key]
            pendings = [self._pending.pop(i) for i in ids]
        for pending in pendings:
            pending.decision = ApprovalDecision(False, detail=detail)
            pending.event.set()
        return len(pendings)

    def cancel_all(self, detail: str = "shutting down") -> int:
        with self._lock:
            pendings = list(self._pending.values())
            self._pending.clear()
        for pending in pendings:
            pending.decision = ApprovalDecision(False, detail=detail)
            pending.event.set()
        return len(pendings)

    # -- worker thread side ---------------------------------------------------

    def request(self, req: ApprovalRequest) -> ApprovalDecision:
        """Ask, and block this thread until answered or timed out.

        Every failure is a refusal. Nobody to ask, the loop gone, the asker
        raising, nobody answering — all of it means the dangerous thing does not
        happen, because the alternative is doing it on the strength of silence.
        """
        with self._lock:
            asker = self._askers.get(req.session_key)
            if asker is None:
                return ApprovalDecision(False, detail="there is nobody to ask")
            pending = _Pending(req)
            self._pending[req.id] = pending

        try:
            asked = asyncio.run_coroutine_threadsafe(self._ask(asker, req), self._loop)
            if not asked.result(timeout=30):
                raise RuntimeError("the question could not be delivered")
        except Exception as exc:
            with self._lock:
                self._pending.pop(req.id, None)
            logger.warning(f"Could not ask for approval of {req.tool}: {exc}")
            return ApprovalDecision(False, detail="the question could not be delivered")

        if not pending.event.wait(self._timeout):
            with self._lock:
                self._pending.pop(req.id, None)
            logger.info(f"Approval for {req.tool} timed out after {self._timeout:.0f}s")
            return ApprovalDecision(False, detail=f"nobody answered within {self._timeout / 60:.0f} minutes")

        # `is not None`, not `or`: a denial is a real decision but a falsy
        # object (`__bool__` is `approved`), so `or` would throw away both the
        # reason and who made it, and report every refusal as "no decision".
        if pending.decision is not None:
            return pending.decision
        return ApprovalDecision(False, detail="no decision recorded")

    @staticmethod
    async def _ask(asker: Asker, req: ApprovalRequest) -> bool:
        try:
            return bool(await asker(req))
        except Exception:
            logger.exception("Approval asker failed")
            return False


#: The running gateway's broker. Module-level for the same reason the delivery
#: router is: the caller is a tool, several frames inside a turn, with no path
#: to the runner and no business growing one.
_broker: Optional[ApprovalBroker] = None


def set_broker(broker: Optional[ApprovalBroker]) -> None:
    global _broker
    _broker = broker


def get_broker() -> Optional[ApprovalBroker]:
    return _broker


#: Which conversation the current turn belongs to. A tool call is several frames
#: inside `run_conversation` and has no idea it is being run for a Slack thread;
#: this is how the question finds its way back to the right one. Bound inside the
#: worker thread by the runner, so nothing has to cross a thread boundary.
_CONVERSATION: contextvars.ContextVar[str] = contextvars.ContextVar("gyrfalcon_conversation", default="")


@contextlib.contextmanager
def in_conversation(session_key: str):
    token = _CONVERSATION.set(session_key)
    try:
        yield
    finally:
        _CONVERSATION.reset(token)


def current_conversation() -> str:
    return _CONVERSATION.get()


def ask_for_approval(tool: str, reason: str, command: str = "") -> Optional[ApprovalDecision]:
    """Ask a person about this tool call, if there is anyone to ask.

    `None` means no — not "denied", but "this install has no way to ask
    anybody", which is every context except a running gateway: the CLI, the TUI,
    the dashboard, a scheduled job. Callers keep their existing behaviour then,
    rather than having dangerous commands silently become impossible.
    """
    broker = get_broker()
    session_key = current_conversation()
    if broker is None or not session_key:
        return None
    return broker.request(ApprovalRequest(tool=tool, reason=reason, command=command, session_key=session_key))
