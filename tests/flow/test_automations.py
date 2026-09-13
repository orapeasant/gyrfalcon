"""Automations — the reactive complement to the imperative flow.

Spec: 15-flow.md §10 ("the flow says 'do A then B'; the automation says
'whenever X, do Y'"), plus the spec's own worked example: "if an agent
escalates twice in an hour, page a human."
"""

from __future__ import annotations

import pytest
from _spec import requires, sym

pytestmark = requires("gyrfalcon.flow.automations:AutomationEngine", section="§10")


@pytest.fixture()
def log(tmp_path):
    EventLog = sym("gyrfalcon.flow.events:EventLog")
    lg = EventLog(tmp_path / "events.db")
    yield lg
    lg.close()


@pytest.fixture()
def engine(log):
    AutomationEngine = sym("gyrfalcon.flow.automations:AutomationEngine")
    e = AutomationEngine(event_log=log)
    yield e
    e.close()


def _emit(log, event_name, resource_id="r1", occurred=None):
    Event = sym("gyrfalcon.flow.events:Event")
    kwargs = {"event": event_name, "resource_id": resource_id}
    if occurred is not None:
        kwargs["occurred"] = occurred
    return log.emit(Event(**kwargs))


class TestTriggerMatching:
    def test_exact_event_name_match(self):
        Trigger = sym("gyrfalcon.flow.automations:Trigger")
        Event = sym("gyrfalcon.flow.events:Event")
        t = Trigger(event_pattern="gyrfalcon.agent.escalated")
        assert t.matches(Event(event="gyrfalcon.agent.escalated", resource_id="r1"))
        assert not t.matches(Event(event="gyrfalcon.agent.other", resource_id="r1"))

    def test_wildcard_prefix_match(self):
        Trigger = sym("gyrfalcon.flow.automations:Trigger")
        Event = sym("gyrfalcon.flow.events:Event")
        t = Trigger(event_pattern="gyrfalcon.agent.*")
        assert t.matches(Event(event="gyrfalcon.agent.escalated", resource_id="r1"))
        assert t.matches(Event(event="gyrfalcon.agent.budget-exceeded", resource_id="r1"))
        assert not t.matches(Event(event="gyrfalcon.flow-run.Completed", resource_id="r1"))

    def test_resource_id_scoping(self):
        Trigger = sym("gyrfalcon.flow.automations:Trigger")
        Event = sym("gyrfalcon.flow.events:Event")
        t = Trigger(event_pattern="x", resource_id="only-this-one")
        assert t.matches(Event(event="x", resource_id="only-this-one"))
        assert not t.matches(Event(event="x", resource_id="something-else"))


class TestSimpleAutomations:
    def test_fires_on_first_match_by_default(self, engine, log):
        fired = []
        Trigger = sym("gyrfalcon.flow.automations:Trigger")
        engine.register("a1", Trigger(event_pattern="x"), lambda hits, e: fired.append(e.event))

        _emit(log, "x")
        assert fired == ["x"]

    def test_does_not_fire_on_non_matching_events(self, engine, log):
        fired = []
        Trigger = sym("gyrfalcon.flow.automations:Trigger")
        engine.register("a1", Trigger(event_pattern="x"), lambda hits, e: fired.append(e))

        _emit(log, "y")
        assert fired == []

    def test_disabled_automation_does_not_fire(self, engine, log):
        fired = []
        Trigger = sym("gyrfalcon.flow.automations:Trigger")
        engine.register("a1", Trigger(event_pattern="x"), lambda hits, e: fired.append(e), enabled=False)

        _emit(log, "x")
        assert fired == []

    def test_set_enabled_toggles_firing(self, engine, log):
        fired = []
        Trigger = sym("gyrfalcon.flow.automations:Trigger")
        engine.register("a1", Trigger(event_pattern="x"), lambda hits, e: fired.append(e))

        engine.set_enabled("a1", False)
        _emit(log, "x")
        assert fired == []

        engine.set_enabled("a1", True)
        _emit(log, "x")
        assert len(fired) == 1

    def test_unregister_stops_firing(self, engine, log):
        fired = []
        Trigger = sym("gyrfalcon.flow.automations:Trigger")
        engine.register("a1", Trigger(event_pattern="x"), lambda hits, e: fired.append(e))
        assert engine.unregister("a1") is True
        _emit(log, "x")
        assert fired == []
        assert engine.unregister("a1") is False


class TestThresholdWindow:
    def test_the_spec_example_escalates_twice_in_an_hour(self, engine, log):
        """The spec's own worked case for why events must be first-class."""
        Trigger = sym("gyrfalcon.flow.automations:Trigger")
        paged = []
        engine.register(
            "page-on-repeat-escalation",
            Trigger(event_pattern="gyrfalcon.agent.escalated", threshold=2, window_seconds=3600),
            lambda hits, e: paged.append(e.resource_id),
        )

        _emit(log, "gyrfalcon.agent.escalated", resource_id="agent-run-1", occurred=1000.0)
        assert paged == [], "must not page on the first occurrence alone"

        _emit(log, "gyrfalcon.agent.escalated", resource_id="agent-run-1", occurred=1500.0)
        assert paged == ["agent-run-1"], "must page once the threshold is reached"

    def test_events_outside_the_window_do_not_count(self, engine, log):
        Trigger = sym("gyrfalcon.flow.automations:Trigger")
        fired = []
        engine.register(
            "t1", Trigger(event_pattern="x", threshold=2, window_seconds=60),
            lambda hits, e: fired.append(1),
        )

        _emit(log, "x", occurred=1000.0)
        _emit(log, "x", occurred=1070.0)  # 70s later — outside the 60s window
        assert fired == [], "the first hit must have aged out of the window"

    def test_resets_after_firing_so_it_can_fire_again_on_a_new_burst(self, engine, log):
        Trigger = sym("gyrfalcon.flow.automations:Trigger")
        fired = []
        engine.register(
            "t1", Trigger(event_pattern="x", threshold=2, window_seconds=3600),
            lambda hits, e: fired.append(1),
        )

        _emit(log, "x", occurred=1000.0)
        _emit(log, "x", occurred=1100.0)
        assert len(fired) == 1

        _emit(log, "x", occurred=1200.0)
        assert len(fired) == 1, "a single event past a just-fired threshold must not re-fire alone"

        _emit(log, "x", occurred=1250.0)
        assert len(fired) == 2, "a fresh pair should fire again"

    def test_threshold_one_is_equivalent_to_a_simple_trigger(self, engine, log):
        Trigger = sym("gyrfalcon.flow.automations:Trigger")
        fired = []
        engine.register("t1", Trigger(event_pattern="x", threshold=1), lambda hits, e: fired.append(1))
        _emit(log, "x")
        assert fired == [1]


class TestActionSafety:
    def test_a_raising_action_does_not_break_the_emitting_call(self, engine, log):
        """§10's subscribe() contract, carried through actions specifically."""
        Trigger = sym("gyrfalcon.flow.automations:Trigger")

        def bad_action(hits, e):
            raise RuntimeError("action exploded")

        engine.register("bad", Trigger(event_pattern="x"), bad_action)
        _emit(log, "x")  # must not raise

    def test_one_automations_failure_does_not_block_another(self, engine, log):
        Trigger = sym("gyrfalcon.flow.automations:Trigger")
        fired = []

        engine.register("bad", Trigger(event_pattern="x"), lambda hits, e: (_ for _ in ()).throw(ValueError()))
        engine.register("good", Trigger(event_pattern="x"), lambda hits, e: fired.append(1))

        _emit(log, "x")
        assert fired == [1]


class TestBuiltinActions:
    def test_action_cancel_run(self, tmp_path):
        RunStore = sym("gyrfalcon.flow.store:RunStore")
        AutomationEngine = sym("gyrfalcon.flow.automations:AutomationEngine")
        Trigger = sym("gyrfalcon.flow.automations:Trigger")
        action_cancel_run = sym("gyrfalcon.flow.automations:action_cancel_run")
        states = sym("gyrfalcon.flow.states")

        set_store = sym("gyrfalcon.flow.store:set_store")

        store = RunStore(tmp_path / "flow.db")
        store.create_run("r1", "long", "flow")
        store.record_transition("r1", states.Running())

        # action_cancel_run reaches for the ambient get_store() singleton, the
        # same pattern the runner and REST layer use — point it at this store.
        set_store(store)
        engine = AutomationEngine(event_log=store.events)
        try:
            engine.register(
                "auto-cancel", Trigger(event_pattern="danger.signal"),
                action_cancel_run(lambda e: e.payload["target_run_id"]),
            )

            from gyrfalcon.flow.events import Event
            store.events.emit(Event(event="danger.signal", resource_id="x",
                                    payload={"target_run_id": "r1"}))

            assert store.get_run("r1")["state_type"] == "CANCELLING"
        finally:
            engine.close()
            set_store(None)
            store.close()

    def test_action_pause_deployment(self, tmp_path):
        DeploymentStore = sym("gyrfalcon.flow.deployments:DeploymentStore")
        deployments_mod = sym("gyrfalcon.flow.deployments")
        AutomationEngine = sym("gyrfalcon.flow.automations:AutomationEngine")
        Trigger = sym("gyrfalcon.flow.automations:Trigger")
        action_pause_deployment = sym("gyrfalcon.flow.automations:action_pause_deployment")
        EventLog = sym("gyrfalcon.flow.events:EventLog")
        flow = sym("gyrfalcon.flow:flow")

        @flow(name=f"auto_pause_target_{id(tmp_path)}")
        def f():
            return 1

        dep_store = DeploymentStore(tmp_path / "flow.db")
        dep = dep_store.create(name="dep1", flow_name=f.name, schedule="30m")
        assert dep["paused"] is False

        deployments_mod.set_deployment_store(dep_store)
        log = EventLog(tmp_path / "flow.db")
        engine = AutomationEngine(event_log=log)
        try:
            engine.register("auto-pause", Trigger(event_pattern="stop.signal"),
                            action_pause_deployment("dep1"))
            from gyrfalcon.flow.events import Event
            log.emit(Event(event="stop.signal", resource_id="x"))

            assert dep_store.get(dep["id"])["paused"] is True
        finally:
            engine.close()
            log.close()
            deployments_mod.set_deployment_store(None)
            dep_store.close()

    def test_action_notify_formats_a_readable_message(self):
        action_notify = sym("gyrfalcon.flow.automations:action_notify")
        Event = sym("gyrfalcon.flow.events:Event")

        captured = []
        action = action_notify(captured.append)
        action([1000.0, 1001.0], Event(event="x.y", resource_id="r1"))

        assert len(captured) == 1
        assert "x.y" in captured[0]
        assert "r1" in captured[0]
        assert "2" in captured[0]
