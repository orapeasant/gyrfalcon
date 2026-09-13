"""Multi-writer correctness — §15.6, delivered by §15.11 step 6.

Three behaviours were correct only because SQLite implied exactly one process.
Each is tested here with two independent stores against one database, which is
the smallest arrangement that can reproduce the bug at all: a single store
cannot race itself.

Runs against every configured backend (see conftest.store_target). The fixes
are deliberately backend-neutral — a compare-and-swap claim rather than
`FOR UPDATE SKIP LOCKED` — so these assertions are meaningful on SQLite too,
not skipped until someone remembers to point them at PostgreSQL.
"""

from __future__ import annotations

import time

import pytest
from _spec import requires, sym

pytestmark = requires("gyrfalcon.flow.store:RunStore", section="§15.6 multi-writer")


@pytest.fixture()
def two_stores(store_target):
    """Two RunStores on one database, as two processes would be."""
    RunStore = sym("gyrfalcon.flow.store:RunStore")
    a = RunStore(**store_target, emit_events=False, reconcile=False)
    b = RunStore(**store_target, emit_events=False, reconcile=False)
    yield a, b
    for s in (a, b):
        try:
            s.close()
        except Exception:
            pass


class TestCrashReconciliationRespectsLiveOwners:
    """§15.6.1 — the fix that stops a second process settling a peer's work."""

    def test_a_peers_live_run_is_not_marked_crashed(self, two_stores, store_target):
        RunStore = sym("gyrfalcon.flow.store:RunStore")
        states = sym("gyrfalcon.flow.states")
        a, _ = two_stores

        a.create_run("live", "still-going", "flow")
        a.record_transition("live", states.Running())

        # A second engine starting up must leave it alone: it is heartbeating.
        c = RunStore(**store_target, emit_events=False, reconcile=True)
        try:
            assert c.get_run("live")["state_type"] == "RUNNING"
        finally:
            c.close()

    def test_an_abandoned_run_is_still_reclaimed(self, two_stores, store_target):
        """The old behaviour must survive: silence past the threshold is death."""
        RunStore = sym("gyrfalcon.flow.store:RunStore")
        store = sym("gyrfalcon.flow.store")
        states = sym("gyrfalcon.flow.states")
        a, _ = two_stores

        a.create_run("dead", "orphan", "flow")
        a.record_transition("dead", states.Running())
        # Backdate the heartbeat: the owner died without releasing anything.
        with a._db.connect() as conn:
            conn.execute(
                "UPDATE flow_runs SET heartbeat_at = ? WHERE id = ?",
                (time.time() - store.STALE_OWNER_SECONDS - 60, "dead"),
            )

        c = RunStore(**store_target, emit_events=False, reconcile=True)
        try:
            assert c.get_run("dead")["state_type"] == "CRASHED"
        finally:
            c.close()

    def test_clean_shutdown_releases_ownership_immediately(self, store_target):
        """A clean close must not force a peer to wait out the threshold."""
        RunStore = sym("gyrfalcon.flow.store:RunStore")
        states = sym("gyrfalcon.flow.states")

        a = RunStore(**store_target, emit_events=False, reconcile=False)
        a.create_run("released", "orphan", "flow")
        a.record_transition("released", states.Running())
        a.close()

        b = RunStore(**store_target, emit_events=False, reconcile=True)
        try:
            assert b.get_run("released")["state_type"] == "CRASHED"
        finally:
            b.close()

    def test_a_terminal_run_carries_no_owner(self, two_stores):
        """Finished runs must drop out of the heartbeat UPDATE entirely."""
        states = sym("gyrfalcon.flow.states")
        a, _ = two_stores

        a.create_run("done", "finished", "flow")
        a.record_transition("done", states.Running())
        a.record_transition("done", states.Completed(data=1))
        with a._db.connect() as conn:
            row = conn.fetchone(
                "SELECT owner_id, heartbeat_at FROM flow_runs WHERE id = ?", ("done",)
            )
        assert row["owner_id"] is None
        assert row["heartbeat_at"] is None

    def test_heartbeat_refreshes_only_this_instances_runs(self, two_stores):
        states = sym("gyrfalcon.flow.states")
        a, b = two_stores

        a.create_run("mine", "a", "flow")
        a.record_transition("mine", states.Running())
        b.create_run("theirs", "b", "flow")
        b.record_transition("theirs", states.Running())

        with a._db.connect() as conn:
            before = conn.fetchone(
                "SELECT heartbeat_at FROM flow_runs WHERE id = ?", ("theirs",)
            )["heartbeat_at"]
        time.sleep(0.01)
        a.touch_owned_runs()
        with a._db.connect() as conn:
            after = conn.fetchone(
                "SELECT heartbeat_at FROM flow_runs WHERE id = ?", ("theirs",)
            )["heartbeat_at"]
        assert after == before, "one instance must not heartbeat another's runs"


class TestDeploymentClaim:
    """§15.6.2 — two runners, one due deployment, exactly one start."""

    @pytest.fixture()
    def due_deployment(self, store_target, request):
        from datetime import datetime, timedelta, timezone

        DeploymentStore = sym("gyrfalcon.flow.deployments:DeploymentStore")
        flow = sym("gyrfalcon.flow:flow")

        name = f"claim_flow_{id(request)}"
        f = flow(name=name)(lambda: 1)
        assert f is not None

        a = DeploymentStore(**store_target)
        b = DeploymentStore(**store_target)
        dep = a.create(name="claimed", flow_name=name, schedule="0 2 * * *")
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with a._db.connect() as conn:
            conn.execute(
                "UPDATE flow_deployments SET next_run_at = ? WHERE id = ?",
                (past, dep["id"]),
            )
        yield a, b, dep
        for s in (a, b):
            s.close()

    def test_exactly_one_runner_claims_a_due_deployment(self, due_deployment):
        a, b, dep = due_deployment
        assert len(a.get_due()) == 1, "precondition: the deployment is due"

        first = a.claim_due()
        second = b.claim_due()

        won = [d["id"] for d in first + second]
        assert won == [dep["id"]], "a due deployment must be claimed exactly once"
        assert len(first) == 1 and len(second) == 0

    def test_claiming_advances_the_schedule(self, due_deployment):
        a, _, dep = due_deployment
        before = a.get(dep["id"])["next_run_at"]
        a.claim_due()
        assert a.get(dep["id"])["next_run_at"] != before
        assert a.get_due() == [], "an already-claimed deployment is no longer due"


class TestConcurrencyLimitReservation:
    """§15.6.3 — count and insert in one transaction, not check-then-act."""

    def test_two_stores_cannot_exceed_the_limit(self, two_stores):
        a, b = two_stores
        granted = [
            s.reserve_run_slot("capped", 2)
            for s in (a, b, a, b, a)
        ]
        assert sum(g is not None for g in granted) == 2, granted
        assert len(set(g for g in granted if g)) == 2, "ids must be distinct"

    def test_a_reservation_is_a_real_pending_run(self, two_stores):
        a, _ = two_stores
        run_id = a.reserve_run_slot("capped", 1)
        row = a.get_run(run_id)
        assert row["state_type"] == "PENDING"
        assert row["name"] == "capped"

    def test_no_limit_always_grants(self, two_stores):
        a, b = two_stores
        assert all(s.reserve_run_slot("uncapped", None) for s in (a, b, a, b))

    def test_a_finished_run_frees_its_slot(self, two_stores):
        states = sym("gyrfalcon.flow.states")
        a, b = two_stores

        first = a.reserve_run_slot("capped", 1)
        assert b.reserve_run_slot("capped", 1) is None
        a.record_transition(first, states.Completed(data=1))
        assert b.reserve_run_slot("capped", 1) is not None

    def test_the_engine_adopts_a_reserved_id_without_duplicating_it(self, two_stores):
        """The reservation and the run must be one row, not two."""
        a, _ = two_stores
        run_id = a.reserve_run_slot("adopted", 5)
        # `create_run` is insert-or-ignore, which is what makes adoption safe.
        a.create_run(run_id, "adopted", "flow")
        rows, total = a.list_runs(limit=50, name="adopted")
        assert total == 1
        assert rows[0]["id"] == run_id
