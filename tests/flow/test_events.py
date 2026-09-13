"""Events — the reactive complement to the imperative flow.

Spec: 15-flow.md §10.
"""

from __future__ import annotations

import pytest
from _spec import requires, sym

pytestmark = requires("gyrfalcon.flow.events:EventLog", section="§10")


@pytest.fixture()
def log(tmp_path):
    EventLog = sym("gyrfalcon.flow.events:EventLog")
    lg = EventLog(tmp_path / "events.db")
    yield lg
    lg.close()


def as_user(user, tenant="acme", roles=()):
    ident = sym("gyrfalcon.identity")
    return ident.use_principal(
        ident.Principal(user_id=user, tenant_id=tenant, roles=roles)
    )


class TestEventShape:
    def test_event_gets_a_time_ordered_id_when_unset(self):
        Event = sym("gyrfalcon.flow.events:Event")
        e1 = Event(event="x", resource_id="r1", occurred=1000.0)
        e2 = Event(event="x", resource_id="r1", occurred=1001.0)
        assert e1.id and e2.id
        assert e1.id < e2.id, "later occurred time must sort after earlier"

    def test_explicit_id_is_respected(self):
        Event = sym("gyrfalcon.flow.events:Event")
        e = Event(event="x", resource_id="r1", id="custom-id")
        assert e.id == "custom-id"


class TestEmitAndQuery:
    def test_emitted_event_is_queryable(self, log):
        Event = sym("gyrfalcon.flow.events:Event")
        log.emit(Event(event="gyrfalcon.flow-run.Completed", resource_id="run-1",
                       payload={"x": 1}))
        rows, total = log.list_events()
        assert total == 1
        assert rows[0]["event"] == "gyrfalcon.flow-run.Completed"
        assert rows[0]["payload"] == {"x": 1}

    def test_filter_by_event_type(self, log):
        Event = sym("gyrfalcon.flow.events:Event")
        log.emit(Event(event="a.type", resource_id="r1"))
        log.emit(Event(event="b.type", resource_id="r1"))
        rows, total = log.list_events(event_type="a.type")
        assert total == 1
        assert rows[0]["event"] == "a.type"

    def test_filter_by_resource(self, log):
        Event = sym("gyrfalcon.flow.events:Event")
        log.emit(Event(event="x", resource_id="r1"))
        log.emit(Event(event="x", resource_id="r2"))
        rows, total = log.list_events(resource_id="r2")
        assert total == 1
        assert rows[0]["resource_id"] == "r2"

    def test_related_resources_round_trip(self, log):
        Event = sym("gyrfalcon.flow.events:Event")
        RelatedResource = sym("gyrfalcon.flow.events:RelatedResource")
        log.emit(Event(event="x", resource_id="r1",
                       related=[RelatedResource(id="agent-1", role="agent")]))
        rows, _ = log.list_events()
        assert rows[0]["related"] == [{"id": "agent-1", "role": "agent"}]


class TestFollowsCausality:
    def test_follows_chain_reconstructs_causal_order(self, log):
        """§10: task events are client-local and may arrive out of order; follows
        is how a consumer reassembles the correct timeline regardless."""
        Event = sym("gyrfalcon.flow.events:Event")
        e1 = log.emit(Event(event="started", resource_id="r1"))
        e2 = log.emit(Event(event="progressed", resource_id="r1", follows=e1.id))
        e3 = log.emit(Event(event="completed", resource_id="r1", follows=e2.id))

        chain = log.follows_chain(e3.id)
        assert [c["event"] for c in chain] == ["started", "progressed", "completed"]

    def test_chain_of_one_is_just_that_event(self, log):
        Event = sym("gyrfalcon.flow.events:Event")
        e1 = log.emit(Event(event="solo", resource_id="r1"))
        assert [c["event"] for c in log.follows_chain(e1.id)] == ["solo"]

    def test_unknown_event_id_yields_empty_chain(self, log):
        assert log.follows_chain("nope") == []


class TestSubscribers:
    def test_subscriber_is_called_on_emit(self, log):
        Event = sym("gyrfalcon.flow.events:Event")
        seen = []
        log.subscribe(seen.append)
        log.emit(Event(event="x", resource_id="r1"))
        assert len(seen) == 1
        assert seen[0].event == "x"

    def test_unsubscribe_stops_delivery(self, log):
        Event = sym("gyrfalcon.flow.events:Event")
        seen = []
        unsub = log.subscribe(seen.append)
        unsub()
        log.emit(Event(event="x", resource_id="r1"))
        assert seen == []

    def test_a_raising_subscriber_does_not_break_emit(self, log):
        """Telemetry/automation consumers must never break the emitting caller."""
        Event = sym("gyrfalcon.flow.events:Event")
        log.subscribe(lambda e: (_ for _ in ()).throw(ValueError("boom")))
        # Must not raise.
        log.emit(Event(event="x", resource_id="r1"))
        rows, total = log.list_events()
        assert total == 1


class TestStoreIntegration:
    """RunStore auto-emits transition events, chained per run."""

    def test_transitions_emit_events_chained_by_follows(self, tmp_path):
        RunStore = sym("gyrfalcon.flow.store:RunStore")
        states = sym("gyrfalcon.flow.states")

        store = RunStore(tmp_path / "flow.db")
        store.create_run("r1", "demo", "flow")
        store.record_transition("r1", states.Running())
        store.record_transition("r1", states.Completed(data=1))

        rows, total = store.events.list_events(resource_id="r1")
        assert total == 2
        by_name = {r["payload"]["state_name"]: r for r in rows}
        completed = by_name["Completed"]
        running = by_name["Running"]
        assert completed["follows"] == running["id"]
        store.close()

    def test_event_names_are_kind_specific(self, tmp_path):
        RunStore = sym("gyrfalcon.flow.store:RunStore")
        states = sym("gyrfalcon.flow.states")

        store = RunStore(tmp_path / "flow.db")
        store.create_run("f1", "flow-demo", "flow")
        store.create_run("t1", "task-demo", "task")
        store.record_transition("f1", states.Completed())
        store.record_transition("t1", states.Completed())

        rows, _ = store.events.list_events()
        events_by_resource = {r["resource_id"]: r["event"] for r in rows}
        assert events_by_resource["f1"] == "gyrfalcon.flow-run.Completed"
        assert events_by_resource["t1"] == "gyrfalcon.task-run.Completed"
        store.close()

    def test_two_run_stores_on_different_files_do_not_cross_contaminate_events(self, tmp_path):
        """Regression risk: a shared process-global EventLog would leak events
        from a test's temp db into whatever the default store points at."""
        RunStore = sym("gyrfalcon.flow.store:RunStore")
        states = sym("gyrfalcon.flow.states")

        store_a = RunStore(tmp_path / "a.db")
        store_b = RunStore(tmp_path / "b.db")
        store_a.create_run("ra", "a", "flow")
        store_a.record_transition("ra", states.Completed())

        rows_a, total_a = store_a.events.list_events()
        rows_b, total_b = store_b.events.list_events()
        assert total_a == 1
        assert total_b == 0
        store_a.close()
        store_b.close()

    def test_emit_events_false_disables_event_writes(self, tmp_path):
        RunStore = sym("gyrfalcon.flow.store:RunStore")
        states = sym("gyrfalcon.flow.states")

        store = RunStore(tmp_path / "flow.db", emit_events=False)
        store.create_run("r1", "demo", "flow")
        store.record_transition("r1", states.Completed())
        assert store.events is None
        store.close()

    def test_a_broken_event_log_does_not_break_persistence(self, tmp_path, monkeypatch):
        """The state write is the source of truth; event emission is best-effort."""
        RunStore = sym("gyrfalcon.flow.store:RunStore")
        states = sym("gyrfalcon.flow.states")

        store = RunStore(tmp_path / "flow.db")
        monkeypatch.setattr(
            store._event_log, "emit",
            lambda e: (_ for _ in ()).throw(RuntimeError("event log down")),
        )
        store.create_run("r1", "demo", "flow")
        store.record_transition("r1", states.Completed(data=42))  # must not raise

        assert store.get_run("r1")["state_type"] == "COMPLETED"
        assert store.get_run("r1")["result"] == 42
        store.close()


class TestDefaultStoreSharesTheGlobalEventLog:
    """Regression: the default RunStore built its own private EventLog even
    when pointed at the default db path, so an AutomationEngine subscribed to
    events.get_event_log() (the process-global singleton) never saw events
    the store emitted — two different EventLog objects, same file, disjoint
    in-memory subscriber lists.
    """

    def test_default_store_and_default_event_log_are_the_same_object(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "gyrfalcon.gyrfalcon_constants.get_gyrfalcon_home", lambda: tmp_path
        )
        RunStore = sym("gyrfalcon.flow.store:RunStore")
        get_event_log = sym("gyrfalcon.flow.events:get_event_log")
        set_event_log = sym("gyrfalcon.flow.events:set_event_log")
        set_event_log(None)

        store = RunStore()  # no explicit db_path -> the default
        assert store.events is get_event_log()
        set_event_log(None)

    def test_an_explicit_path_still_gets_an_isolated_event_log(self, tmp_path):
        """The isolation fix from earlier must survive this change."""
        RunStore = sym("gyrfalcon.flow.store:RunStore")
        get_event_log = sym("gyrfalcon.flow.events:get_event_log")

        store = RunStore(tmp_path / "custom.db")
        assert store.events is not get_event_log()
        store.close()

    def test_closing_a_default_store_does_not_close_the_shared_log(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "gyrfalcon.gyrfalcon_constants.get_gyrfalcon_home", lambda: tmp_path
        )
        RunStore = sym("gyrfalcon.flow.store:RunStore")
        set_event_log = sym("gyrfalcon.flow.events:set_event_log")
        Event = sym("gyrfalcon.flow.events:Event")
        set_event_log(None)

        store = RunStore()
        shared_log = store.events
        store.close()

        # Must still be usable — closing the store must not have closed it.
        shared_log.emit(Event(event="x", resource_id="r1"))
        set_event_log(None)

    def test_automation_engine_sees_events_emitted_through_the_default_store(self, tmp_path, monkeypatch):
        """The actual failure mode: register an automation, run a flow, expect it to fire."""
        monkeypatch.setattr(
            "gyrfalcon.gyrfalcon_constants.get_gyrfalcon_home", lambda: tmp_path
        )
        set_store = sym("gyrfalcon.flow.store:set_store")
        set_event_log = sym("gyrfalcon.flow.events:set_event_log")
        set_automation_engine = sym("gyrfalcon.flow.automations:set_automation_engine")
        get_automation_engine = sym("gyrfalcon.flow.automations:get_automation_engine")
        Trigger = sym("gyrfalcon.flow.automations:Trigger")
        action_notify = sym("gyrfalcon.flow.automations:action_notify")
        flow = sym("gyrfalcon.flow:flow")
        engine_mod = sym("gyrfalcon.flow.engine")

        set_store(None)
        set_event_log(None)
        set_automation_engine(None)
        engine_mod._BaseRunEngine.persist = True

        try:
            notified = []
            get_automation_engine().register(
                "notify-on-failure",
                Trigger(event_pattern="gyrfalcon.flow-run.Failed"),
                action_notify(notified.append),
            )

            @flow(name=f"regression_target_{id(tmp_path)}")
            def risky():
                raise RuntimeError("boom")

            risky(return_type="state")
            assert notified, "automation never observed the store's event"
        finally:
            engine_mod._BaseRunEngine.persist = False
            set_store(None)
            set_event_log(None)
            set_automation_engine(None)


class TestTenantIsolation:
    """§17.5. `list_events` and `follows_chain` are the two public reads on
    `EventLog` and neither is exercised anywhere else against more than one
    tenant — `test_scope.py`'s isolation suite covers `RunStore` only."""

    def test_list_events_does_not_cross_tenants(self, log):
        Event = sym("gyrfalcon.flow.events:Event")
        with as_user("alice", "acme"):
            log.emit(Event(event="x", resource_id="r1", payload={"secret": 1}))
        with as_user("bob", "other"):
            rows, total = log.list_events()
            assert rows == [] and total == 0
        with as_user("alice", "acme"):
            rows, total = log.list_events()
            assert total == 1

    def test_list_events_filters_still_compose_with_the_tenant_filter(self, log):
        Event = sym("gyrfalcon.flow.events:Event")
        with as_user("alice", "acme"):
            log.emit(Event(event="a.type", resource_id="r1"))
        with as_user("bob", "other"):
            log.emit(Event(event="a.type", resource_id="r1"))
        with as_user("alice", "acme"):
            rows, total = log.list_events(event_type="a.type")
            assert total == 1

    def test_an_operator_sees_the_whole_tenant_not_just_their_own_events(self, log):
        Event = sym("gyrfalcon.flow.events:Event")
        with as_user("alice", "acme"):
            log.emit(Event(event="x", resource_id="r1"))
        with as_user("ops", "acme", roles=["operator"]):
            _, total = log.list_events()
            assert total == 1
        with as_user("ops", "other", roles=["operator"]):
            _, total = log.list_events()
            assert total == 0, "operator is tenant-wide, never cross-tenant"

    def test_follows_chain_does_not_cross_a_tenant_boundary(self, log):
        """A chain built entirely inside one tenant must be invisible from
        another — walking `follows` must not bypass the scope on each hop."""
        Event = sym("gyrfalcon.flow.events:Event")
        with as_user("alice", "acme"):
            e1 = log.emit(Event(event="started", resource_id="r1"))
            e2 = log.emit(Event(event="completed", resource_id="r1", follows=e1.id))
        with as_user("bob", "other"):
            assert log.follows_chain(e2.id) == []
        with as_user("alice", "acme"):
            chain = log.follows_chain(e2.id)
            assert [c["event"] for c in chain] == ["started", "completed"]

    def test_follows_chain_stops_rather_than_walking_into_another_tenant(self, log):
        """If a chain's own tenant is visible but (hypothetically) one link in
        it belonged to someone else, the walk must stop at that link rather
        than silently continuing across the boundary."""
        Event = sym("gyrfalcon.flow.events:Event")
        with as_user("alice", "acme"):
            e1 = log.emit(Event(event="started", resource_id="r1"))
        with as_user("bob", "other"):
            # A cross-tenant `follows` reference should not occur in practice,
            # but the chain walk must be defensive about it regardless.
            e2 = log.emit(Event(event="continued", resource_id="r1", follows=e1.id))
            chain = log.follows_chain(e2.id)
        assert [c["event"] for c in chain] == ["continued"], (
            "the walk must not have been able to read alice's event"
        )
