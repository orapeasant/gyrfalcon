"""Deployments and the Runner — Part IX, Runner half only.

Spec: §9.1 (Deployment), §9.2 (Runner vs Worker — this covers Runner: "dev,
small prod, single box", which is what a single gyrfalcon process is).
"""

from __future__ import annotations

import time

import pytest
from _spec import requires, sym

pytestmark = requires("gyrfalcon.flow.deployments:DeploymentStore", section="§9.1")


@pytest.fixture()
def dep_store(tmp_path):
    DeploymentStore = sym("gyrfalcon.flow.deployments:DeploymentStore")
    s = DeploymentStore(tmp_path / "flow.db")
    yield s
    s.close()


@pytest.fixture()
def run_store(tmp_path, dep_store):
    """Deployments and runs share one db file — the store fixture points at it."""
    RunStore = sym("gyrfalcon.flow.store:RunStore")
    set_store = sym("gyrfalcon.flow.store:set_store")
    s = RunStore(tmp_path / "flow.db")
    set_store(s)
    yield s
    set_store(None)
    s.close()


@pytest.fixture(autouse=True)
def _persist(run_store):
    engine = sym("gyrfalcon.flow.engine:_BaseRunEngine")
    engine.persist = True
    yield
    engine.persist = False


@pytest.fixture()
def registered_flow():
    """A @flow registered under a unique name per test, so tests don't collide
    in the shared process-global registry."""
    flow = sym("gyrfalcon.flow:flow")
    calls = []

    @flow(name=f"dep_test_flow_{id(calls)}")
    def f(x=1):
        calls.append(x)
        return x

    return f, calls


class TestDeploymentCRUD:
    def test_create_requires_a_registered_flow(self, dep_store):
        with pytest.raises(ValueError, match="No registered flow"):
            dep_store.create(name="d1", flow_name="does_not_exist")

    def test_create_and_get(self, dep_store, registered_flow):
        f, _ = registered_flow
        d = dep_store.create(name="my-deployment", flow_name=f.name, parameters={"x": 5})
        assert d["flow_name"] == f.name
        assert d["parameters"] == {"x": 5}
        assert dep_store.get(d["id"])["name"] == "my-deployment"

    def test_name_must_be_unique(self, dep_store, registered_flow):
        f, _ = registered_flow
        dep_store.create(name="dup", flow_name=f.name)
        with pytest.raises(Exception):
            dep_store.create(name="dup", flow_name=f.name)

    def test_schedule_reuses_the_existing_parser(self, dep_store, registered_flow):
        """Not a second parser — gyrfalcon.scheduler.parse_schedule, verbatim."""
        f, _ = registered_flow
        d = dep_store.create(name="scheduled", flow_name=f.name, schedule="30m")
        assert d["schedule"]["kind"] == "interval"
        assert d["schedule"]["minutes"] == 30
        assert d["next_run_at"] is not None

    def test_paused_deployment_has_no_next_run(self, dep_store, registered_flow):
        f, _ = registered_flow
        d = dep_store.create(name="off", flow_name=f.name, schedule="30m", paused=True)
        assert d["next_run_at"] is None

    def test_unpausing_computes_a_next_run(self, dep_store, registered_flow):
        f, _ = registered_flow
        d = dep_store.create(name="toggled", flow_name=f.name, schedule="30m", paused=True)
        resumed = dep_store.set_paused(d["id"], False)
        assert resumed["paused"] is False
        assert resumed["next_run_at"] is not None

    def test_pausing_clears_next_run(self, dep_store, registered_flow):
        f, _ = registered_flow
        d = dep_store.create(name="running", flow_name=f.name, schedule="30m")
        paused = dep_store.set_paused(d["id"], True)
        assert paused["next_run_at"] is None

    def test_delete(self, dep_store, registered_flow):
        f, _ = registered_flow
        d = dep_store.create(name="temp", flow_name=f.name)
        assert dep_store.delete(d["id"]) is True
        assert dep_store.get(d["id"]) is None
        assert dep_store.delete(d["id"]) is False

    def test_list_all_is_sorted_by_name(self, dep_store, registered_flow):
        f, _ = registered_flow
        dep_store.create(name="zeta", flow_name=f.name)
        dep_store.create(name="alpha", flow_name=f.name)
        names = [d["name"] for d in dep_store.list_all()]
        assert names == sorted(names)

    def test_update_changes_name_schedule_and_parameters(self, dep_store, registered_flow):
        f, _ = registered_flow
        d = dep_store.create(name="old", flow_name=f.name, parameters={"x": 1})
        updated = dep_store.update(
            d["id"], name="new", schedule="30m", parameters={"x": 2},
            concurrency_limit=3,
        )
        assert updated["name"] == "new"
        assert updated["schedule_raw"] == "30m"
        assert updated["parameters"] == {"x": 2}
        assert updated["concurrency_limit"] == 3

    def test_update_recomputes_next_run_from_new_schedule(self, dep_store, registered_flow):
        f, _ = registered_flow
        d = dep_store.create(name="d", flow_name=f.name)  # no schedule -> no next_run
        assert d["next_run_at"] is None
        updated = dep_store.update(d["id"], name="d", schedule="30m")
        assert updated["next_run_at"] is not None

    def test_update_of_paused_deployment_leaves_next_run_unset(self, dep_store, registered_flow):
        f, _ = registered_flow
        d = dep_store.create(name="d", flow_name=f.name, schedule="30m", paused=True)
        updated = dep_store.update(d["id"], name="d", schedule="1h")
        assert updated["next_run_at"] is None

    def test_update_of_missing_deployment_returns_none(self, dep_store):
        assert dep_store.update("no-such-id", name="x") is None


class TestDueSelection:
    def test_a_due_past_schedule_is_selected(self, dep_store, registered_flow):
        f, _ = registered_flow
        d = dep_store.create(name="due", flow_name=f.name, schedule="30m")
        # Force it due by asking "is it due" far enough in the future.
        from datetime import datetime, timedelta, timezone
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        due = dep_store.get_due(now=future)
        assert d["id"] in [x["id"] for x in due]

    def test_not_yet_due_is_excluded(self, dep_store, registered_flow):
        f, _ = registered_flow
        dep_store.create(name="future", flow_name=f.name, schedule="30m")
        due = dep_store.get_due()  # "now" — a fresh 30m schedule isn't due yet
        assert due == []

    def test_paused_is_never_due(self, dep_store, registered_flow):
        f, _ = registered_flow
        dep_store.create(name="off", flow_name=f.name, schedule="30m", paused=True)
        from datetime import datetime, timedelta, timezone
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        assert dep_store.get_due(now=future) == []

    def test_advance_schedule_moves_next_run_forward(self, dep_store, registered_flow):
        f, _ = registered_flow
        d = dep_store.create(name="d", flow_name=f.name, schedule="30m")
        first_next = d["next_run_at"]
        dep_store.advance_schedule(d["id"])
        second_next = dep_store.get(d["id"])["next_run_at"]
        assert second_next >= first_next


class TestRunnerExecution:
    def test_tick_starts_a_due_deployment(self, dep_store, run_store, registered_flow):
        Runner = sym("gyrfalcon.flow.runner:Runner")
        f, calls = registered_flow
        dep_store.create(name="d", flow_name=f.name, parameters={"x": 42}, schedule="30m")

        # Monkeypatch get_due to force it due without waiting on real wall time.
        from datetime import datetime, timedelta, timezone
        orig = dep_store.get_due
        dep_store.get_due = lambda now=None: orig(now=datetime.now(timezone.utc) + timedelta(hours=1))

        from gyrfalcon.flow import deployments as dep_mod
        dep_mod.set_deployment_store(dep_store)
        try:
            runner = Runner()
            runner.tick()
            for _ in range(50):
                if calls:
                    break
                time.sleep(0.02)
            assert calls == [42]
        finally:
            dep_mod.set_deployment_store(None)

    def test_tick_advances_schedule_even_when_run_starts_async(self, dep_store, run_store, registered_flow):
        Runner = sym("gyrfalcon.flow.runner:Runner")
        f, calls = registered_flow
        d = dep_store.create(name="d", flow_name=f.name, schedule="30m")

        from datetime import datetime, timedelta, timezone
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        dep_store.get_due = lambda now=None: dep_store.__class__.get_due(dep_store, now=future)

        from gyrfalcon.flow import deployments as dep_mod
        dep_mod.set_deployment_store(dep_store)
        try:
            runner = Runner()
            first_next = d["next_run_at"]
            runner.tick()
            assert dep_store.get(d["id"])["next_run_at"] != first_next
        finally:
            dep_mod.set_deployment_store(None)

    def test_unregistered_flow_is_skipped_not_raised(self, dep_store, run_store, registered_flow):
        """A deployment outliving its flow (module reload, typo) must not crash the tick."""
        Runner = sym("gyrfalcon.flow.runner:Runner")
        f, _ = registered_flow
        d = dep_store.create(name="d", flow_name=f.name, schedule="30m")

        from gyrfalcon.flow import registry
        registry._REGISTRY.pop(f.name, None)  # simulate the flow disappearing

        dep_store.get_due = lambda now=None: [dep_store.get(d["id"])]

        from gyrfalcon.flow import deployments as dep_mod
        dep_mod.set_deployment_store(dep_store)
        try:
            runner = Runner()
            runner.tick()  # must not raise
        finally:
            dep_mod.set_deployment_store(None)
            registry.register(f)  # restore for other tests

    def test_concurrency_limit_blocks_a_new_start(self, dep_store, run_store, registered_flow):
        Runner = sym("gyrfalcon.flow.runner:Runner")
        states = sym("gyrfalcon.flow.states")
        f, calls = registered_flow

        d = dep_store.create(name="d", flow_name=f.name, concurrency_limit=1, schedule="30m")
        # Simulate one already-active run of this flow.
        run_store.create_run("already-running", f.name, "flow")
        run_store.record_transition("already-running", states.Running())

        dep_store.get_due = lambda now=None: [dep_store.get(d["id"])]
        from gyrfalcon.flow import deployments as dep_mod
        dep_mod.set_deployment_store(dep_store)
        try:
            runner = Runner()
            runner.tick()
            time.sleep(0.1)
            assert calls == [], "concurrency limit must block the new start"
        finally:
            dep_mod.set_deployment_store(None)

    def test_concurrency_limit_allows_a_start_when_under_limit(self, dep_store, run_store, registered_flow):
        Runner = sym("gyrfalcon.flow.runner:Runner")
        f, calls = registered_flow

        d = dep_store.create(name="d", flow_name=f.name, concurrency_limit=2, parameters={"x": 7})
        dep_store.get_due = lambda now=None: [dep_store.get(d["id"])]
        from gyrfalcon.flow import deployments as dep_mod
        dep_mod.set_deployment_store(dep_store)
        try:
            runner = Runner()
            runner.tick()
            for _ in range(50):
                if calls:
                    break
                time.sleep(0.02)
            assert calls == [7]
        finally:
            dep_mod.set_deployment_store(None)


class TestRunNow:
    """`run_now` — the dashboard's manual "Run" button (spec §14.9a)."""

    def test_run_now_starts_immediately_without_being_due(self, dep_store, run_store, registered_flow):
        """No get_due monkeypatch here on purpose — a fresh schedule is not
        due yet, and run_now must fire anyway."""
        Runner = sym("gyrfalcon.flow.runner:Runner")
        f, calls = registered_flow
        d = dep_store.create(name="d", flow_name=f.name, parameters={"x": 9}, schedule="30m")

        from gyrfalcon.flow import deployments as dep_mod
        dep_mod.set_deployment_store(dep_store)
        try:
            runner = Runner()
            run_id = runner.run_now(d["id"])
            assert run_id
            for _ in range(50):
                if calls:
                    break
                time.sleep(0.02)
            assert calls == [9]
        finally:
            dep_mod.set_deployment_store(None)

    def test_run_now_does_not_perturb_the_schedule(self, dep_store, run_store, registered_flow):
        Runner = sym("gyrfalcon.flow.runner:Runner")
        f, _ = registered_flow
        d = dep_store.create(name="d", flow_name=f.name, schedule="30m")
        first_next = d["next_run_at"]

        from gyrfalcon.flow import deployments as dep_mod
        dep_mod.set_deployment_store(dep_store)
        try:
            Runner().run_now(d["id"])
            assert dep_store.get(d["id"])["next_run_at"] == first_next
        finally:
            dep_mod.set_deployment_store(None)

    def test_run_now_on_missing_deployment_raises(self, dep_store, run_store):
        Runner = sym("gyrfalcon.flow.runner:Runner")
        from gyrfalcon.flow import deployments as dep_mod
        dep_mod.set_deployment_store(dep_store)
        try:
            with pytest.raises(ValueError):
                Runner().run_now("no-such-id")
        finally:
            dep_mod.set_deployment_store(None)

    def test_run_now_on_unregistered_flow_raises_rather_than_silently_skipping(
        self, dep_store, run_store, registered_flow
    ):
        """The tick path swallows this (a schedule shouldn't crash the runner);
        run_now has a caller waiting on the result and must not swallow it."""
        Runner = sym("gyrfalcon.flow.runner:Runner")
        f, _ = registered_flow
        d = dep_store.create(name="d", flow_name=f.name)

        from gyrfalcon.flow import registry
        registry._REGISTRY.pop(f.name, None)

        from gyrfalcon.flow import deployments as dep_mod
        dep_mod.set_deployment_store(dep_store)
        try:
            with pytest.raises(ValueError):
                Runner().run_now(d["id"])
        finally:
            dep_mod.set_deployment_store(None)
            registry.register(f)

    def test_run_now_respects_concurrency_limit(self, dep_store, run_store, registered_flow):
        Runner = sym("gyrfalcon.flow.runner:Runner")
        states = sym("gyrfalcon.flow.states")
        f, calls = registered_flow
        d = dep_store.create(name="d", flow_name=f.name, concurrency_limit=1)
        run_store.create_run("already-running", f.name, "flow")
        run_store.record_transition("already-running", states.Running())

        from gyrfalcon.flow import deployments as dep_mod
        dep_mod.set_deployment_store(dep_store)
        try:
            with pytest.raises(ValueError):
                Runner().run_now(d["id"])
            assert calls == []
        finally:
            dep_mod.set_deployment_store(None)


class TestConcurrencyCounting:
    def test_count_active_is_exact_not_substring(self, run_store):
        """Regression risk: list_runs(name=...) is a LIKE %name% search filter;
        reusing it for concurrency counting would over-count a substring match."""
        states = sym("gyrfalcon.flow.states")
        run_store.create_run("r1", "ingest", "flow")
        run_store.record_transition("r1", states.Running())
        run_store.create_run("r2", "ingest_v2", "flow")
        run_store.record_transition("r2", states.Running())

        assert run_store.count_active("ingest") == 1
        assert run_store.count_active("ingest_v2") == 1

    def test_count_active_excludes_terminal_runs(self, run_store):
        states = sym("gyrfalcon.flow.states")
        run_store.create_run("r1", "f", "flow")
        run_store.record_transition("r1", states.Completed())
        assert run_store.count_active("f") == 0


class TestRunnerLiveness:
    def test_not_running_when_never_started(self):
        is_runner_running = sym("gyrfalcon.flow.runner:is_runner_running")
        from gyrfalcon.flow import runner as runner_mod
        runner_mod._RUNNER = None
        assert is_runner_running() is False

    def test_running_flag_reflects_a_started_runner(self, run_store):
        Runner = sym("gyrfalcon.flow.runner:Runner")
        from gyrfalcon.flow import runner as runner_mod

        r = Runner(tick_seconds=100)  # long tick — we only check the flag, not a real fire
        runner_mod._RUNNER = r
        try:
            r.start()
            assert runner_mod.is_runner_running() is True
        finally:
            r.stop()
            runner_mod._RUNNER = None


def as_user(user, tenant="acme", roles=()):
    ident = sym("gyrfalcon.identity")
    return ident.use_principal(
        ident.Principal(user_id=user, tenant_id=tenant, roles=roles)
    )


class TestDeploymentTenantIsolation:
    """§17.5. `get_by_name` and `list_all` are DeploymentStore's other public
    reads — `test_scope.py`'s isolation suite only exercises `RunStore`."""

    def test_get_by_name_does_not_cross_tenants(self, dep_store, registered_flow):
        Scope = sym("gyrfalcon.flow.db.scope:Scope")
        f, _ = registered_flow
        with as_user("alice", "acme"):
            dep_store.create(name=f"secret_{id(f)}", flow_name=f.name)
        with as_user("bob", "other"):
            assert dep_store.get_by_name(f"secret_{id(f)}") is None
        with as_user("alice", "acme"):
            assert dep_store.get_by_name(f"secret_{id(f)}") is not None
        # And a system read, used only by the internal bookkeeping helpers,
        # can still find it regardless of who is asking.
        assert dep_store.get_by_name(
            f"secret_{id(f)}", Scope.system(reason="test")
        ) is not None

    def test_list_all_does_not_cross_tenants(self, dep_store, registered_flow):
        f, _ = registered_flow
        with as_user("alice", "acme"):
            dep_store.create(name=f"mine_{id(f)}", flow_name=f.name)
        with as_user("bob", "other"):
            names = [d["name"] for d in dep_store.list_all()]
            assert f"mine_{id(f)}" not in names
        with as_user("alice", "acme"):
            names = [d["name"] for d in dep_store.list_all()]
            assert f"mine_{id(f)}" in names

    def test_an_operator_sees_every_deployment_in_their_tenant(self, dep_store, registered_flow):
        f, _ = registered_flow
        with as_user("alice", "acme"):
            dep_store.create(name=f"op_{id(f)}", flow_name=f.name)
        with as_user("ops", "acme", roles=["operator"]):
            names = [d["name"] for d in dep_store.list_all()]
            assert f"op_{id(f)}" in names
        with as_user("ops", "other", roles=["operator"]):
            names = [d["name"] for d in dep_store.list_all()]
            assert f"op_{id(f)}" not in names, "operator is tenant-wide, never cross-tenant"


class TestScheduledRunOwnership:
    """§17.4: unattended, scheduled work is not orphaned or platform-owned —
    the runner starts it under the identity that created the deployment."""

    def test_a_scheduled_run_is_owned_by_the_deployments_creator(
        self, dep_store, run_store, registered_flow
    ):
        Runner = sym("gyrfalcon.flow.runner:Runner")
        Scope = sym("gyrfalcon.flow.db.scope:Scope")
        f, calls = registered_flow

        with as_user("alice", "acme"):
            d = dep_store.create(name=f"scheduled_{id(f)}", flow_name=f.name, schedule="30m")

        system = Scope.system(reason="test reads across the tenant boundary it just set up")
        dep_store.get_due = lambda now=None: [dep_store.get(d["id"], system)]

        from gyrfalcon.flow import deployments as dep_mod
        dep_mod.set_deployment_store(dep_store)
        try:
            runner = Runner()
            runner.tick()
            for _ in range(50):
                if calls:
                    break
                time.sleep(0.02)
            assert calls, "the deployment's flow never ran"

            rows, total = run_store.list_runs(name=f.name, scope=system)
            assert total == 1
            assert rows[0]["user_id"] == "alice"
            assert rows[0]["tenant_id"] == "acme"
        finally:
            dep_mod.set_deployment_store(None)
