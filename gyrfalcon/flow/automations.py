"""Automations — the reactive complement to the imperative flow.

Spec: §10.

The flow says "do A then B"; an automation says "whenever X, do Y." Built
entirely on top of `events.EventLog.subscribe` — nothing below this module
needed to change to support it, which is the same payoff the orchestration
rules got from being ordinary data (§4.4).

Concretely aimed at agent observability, per the spec's own example: "if an
agent escalates twice in an hour, page a human" is a `threshold` trigger over
`gyrfalcon.agent.*` events plus a `notify` action — no new engine concept.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("gyrfalcon.flow.automations")


@dataclass
class Trigger:
    """Fires when `event_pattern` occurs `threshold` times within `window`.

    `threshold=1` (the default) is the plain "whenever X happens" case; a
    higher threshold with a window is the count-based case from the spec's own
    example.
    """

    event_pattern: str
    threshold: int = 1
    window_seconds: Optional[float] = None
    resource_id: Optional[str] = None

    def matches(self, event) -> bool:
        if self.resource_id is not None and event.resource_id != self.resource_id:
            return False
        pattern = self.event_pattern
        if pattern.endswith("*"):
            return event.event.startswith(pattern[:-1])
        return event.event == pattern


@dataclass
class Automation:
    name: str
    trigger: Trigger
    action: Callable[[list], None]
    enabled: bool = True
    #: Recent matching-event timestamps, for the threshold/window check.
    _hits: list[float] = field(default_factory=list, repr=False)
    #: Set once fired for a given window, so a threshold automation doesn't
    #: re-fire on every subsequent matching event until the window rolls over.
    _fired_until: float = field(default=0.0, repr=False)


class AutomationEngine:
    """Evaluates automations against a live event stream."""

    def __init__(self, event_log=None):
        from gyrfalcon.flow.events import get_event_log

        self._log = event_log or get_event_log()
        self._automations: dict[str, Automation] = {}
        self._lock = threading.Lock()
        self._unsubscribe = self._log.subscribe(self._on_event)

    def register(
        self,
        name: str,
        trigger: Trigger,
        action: Callable[[list], None],
        enabled: bool = True,
    ) -> Automation:
        auto = Automation(name=name, trigger=trigger, action=action, enabled=enabled)
        with self._lock:
            self._automations[name] = auto
        return auto

    def unregister(self, name: str) -> bool:
        with self._lock:
            return self._automations.pop(name, None) is not None

    def set_enabled(self, name: str, enabled: bool) -> Optional[Automation]:
        with self._lock:
            auto = self._automations.get(name)
            if auto is not None:
                auto.enabled = enabled
            return auto

    def list_automations(self) -> list[Automation]:
        with self._lock:
            return list(self._automations.values())

    def _on_event(self, event) -> None:
        with self._lock:
            candidates = [a for a in self._automations.values() if a.enabled]

        for auto in candidates:
            if not auto.trigger.matches(event):
                continue
            self._record_hit(auto, event)

    def _record_hit(self, auto: Automation, event) -> None:
        now = event.occurred
        window = auto.trigger.window_seconds
        with self._lock:
            auto._hits.append(now)
            if window is not None:
                cutoff = now - window
                auto._hits = [h for h in auto._hits if h >= cutoff]

            if len(auto._hits) < auto.trigger.threshold:
                return

            # One firing per full window: reset the count so the same burst of
            # events cannot fire the action once per event past the threshold.
            hits_snapshot = list(auto._hits)
            auto._hits = []
            auto._fired_until = now

        self._run_action(auto, hits_snapshot, event)

    def _run_action(self, auto: Automation, hit_timestamps: list[float], triggering_event) -> None:
        """Actions run off the event-emission call stack (§10's `subscribe`
        callers must never break on a raising subscriber) and must not
        themselves be allowed to break automation evaluation for others."""
        try:
            auto.action(hit_timestamps, triggering_event)
        except Exception:
            logger.error(f"Automation {auto.name!r} action raised", exc_info=True)

    def close(self) -> None:
        self._unsubscribe()


# ── built-in actions (§10's own list: run a deployment, cancel a run, pause a
# schedule, call a webhook, notify) ───────────────────────────────────────────

def action_cancel_run(resource_id_getter: Callable[[Any], str]):
    """Cancel whichever run the triggering event names."""
    def action(hits, event):
        from gyrfalcon.flow.store import get_store
        get_store().request_cancel(resource_id_getter(event))
    return action


def action_pause_deployment(deployment_name: str):
    def action(hits, event):
        from gyrfalcon.flow.deployments import get_deployment_store
        store = get_deployment_store()
        dep = store.get_by_name(deployment_name)
        if dep:
            store.set_paused(dep["id"], True)
    return action


def action_run_deployment(deployment_name: str):
    def action(hits, event):
        from gyrfalcon.flow.deployments import get_deployment_store
        from gyrfalcon.flow.registry import get_definition

        store = get_deployment_store()
        dep = store.get_by_name(deployment_name)
        if not dep:
            logger.warning(f"action_run_deployment: no deployment named {deployment_name!r}")
            return
        template = get_definition(dep["flow_name"])
        if template is None:
            logger.warning(f"action_run_deployment: flow {dep['flow_name']!r} not registered")
            return
        threading.Thread(
            target=lambda: template(**dep["parameters"], return_type="state"),
            daemon=True, name=f"automation-{deployment_name[:20]}",
        ).start()
    return action


def action_notify(callback: Callable[[str], None]):
    """Generic notification hook — the caller wires this to whatever channel
    it wants (message gateway, log, webhook); automations.py stays agnostic."""
    def action(hits, event):
        callback(
            f"Automation fired: {event.event} on {event.resource_id} "
            f"({len(hits)} occurrence(s))"
        )
    return action


_ENGINE: Optional[AutomationEngine] = None
_ENGINE_LOCK = threading.Lock()


def get_automation_engine() -> AutomationEngine:
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = AutomationEngine()
        return _ENGINE


def set_automation_engine(engine: Optional[AutomationEngine]) -> None:
    global _ENGINE
    with _ENGINE_LOCK:
        _ENGINE = engine
