"""Authorization — §17.6.

The rule under test is the strictest row in §17.6's table: answering an
approval is not the same as being able to see it. Before this,
`POST /api/flow/tasks/{run_id}/respond` resumed *any* run for *any* caller,
so any authenticated user could approve anyone else's spend request (§17.1).
"""

from __future__ import annotations

import pytest
from _spec import requires, sym

pytestmark = requires("gyrfalcon.flow.pause:may_answer", section="§17.6 authorization")


def as_user(user, tenant="acme", roles=()):
    ident = sym("gyrfalcon.identity")
    return ident.use_principal(
        ident.Principal(user_id=user, tenant_id=tenant, roles=roles)
    )


@pytest.fixture()
def store(make_store):
    set_store = sym("gyrfalcon.flow.store:set_store")
    s = make_store()
    set_store(s)
    yield s
    set_store(None)
    s.close()


@pytest.fixture()
def paused_run(store):
    """A run owned by alice, sitting at an approval gate."""
    pause = sym("gyrfalcon.flow.pause")
    states = sym("gyrfalcon.flow.states")
    with as_user("alice"):
        store.create_run("gate", "spend_request", "flow")
        store.record_transition("gate", states.Running())
    pause._PAUSES["gate"] = {
        "state": states.Paused(), "wait_for_input": None, "paused_at": 0.0,
    }
    yield "gate"
    pause._PAUSES.pop("gate", None)


class TestWhoMayAnswer:
    def test_the_owner_may(self, paused_run):
        pause = sym("gyrfalcon.flow.pause")
        with as_user("alice"):
            assert pause.may_answer(paused_run)

    def test_a_peer_in_the_same_tenant_may_not(self, paused_run):
        pause = sym("gyrfalcon.flow.pause")
        with as_user("bob"):
            assert not pause.may_answer(paused_run)

    def test_an_operator_in_the_same_tenant_may(self, paused_run):
        pause = sym("gyrfalcon.flow.pause")
        with as_user("ops", roles=["operator"]):
            assert pause.may_answer(paused_run)

    def test_an_operator_in_another_tenant_may_not(self, paused_run):
        """Operator is tenant-wide, never cross-tenant."""
        pause = sym("gyrfalcon.flow.pause")
        with as_user("ops", tenant="other", roles=["operator"]):
            assert not pause.may_answer(paused_run)


class TestTheGate:
    def test_a_stranger_is_refused(self, paused_run):
        pause = sym("gyrfalcon.flow.pause")
        with as_user("bob"):
            with pytest.raises(pause.NotThePerformerError):
                pause.resume_flow_run(paused_run, run_input={"approve": True})

    def test_a_refused_answer_does_not_resume_the_run(self, paused_run):
        pause = sym("gyrfalcon.flow.pause")
        with as_user("bob"):
            with pytest.raises(pause.NotThePerformerError):
                pause.resume_flow_run(paused_run, run_input={"approve": True})
        assert not pause._PAUSES[paused_run].get("answered"), (
            "a refused approval must leave the gate closed"
        )

    def test_the_owner_resumes(self, paused_run):
        pause = sym("gyrfalcon.flow.pause")
        with as_user("alice"):
            result = pause.resume_flow_run(paused_run, run_input={"approve": True})
        assert result.flow_run_id == paused_run
        assert pause._PAUSES[paused_run]["answered"] is True

    def test_the_answer_is_attributed(self, paused_run):
        """An approval nobody can be attributed to is not auditable."""
        pause = sym("gyrfalcon.flow.pause")
        with as_user("ops", roles=["operator"]):
            pause.resume_flow_run(paused_run, run_input={"approve": True})
        record = pause._PAUSES[paused_run]
        assert record["answered_by"] == "ops"
        assert record["answered_at"] > 0

    def test_a_flow_resuming_itself_bypasses_the_gate(self, paused_run):
        """`authorize=False` exists for in-process resumption only."""
        pause = sym("gyrfalcon.flow.pause")
        with as_user("bob"):
            pause.resume_flow_run(paused_run, run_input=None, authorize=False)
        assert pause._PAUSES[paused_run]["answered"] is True


class TestTheInbox:
    def test_the_inbox_shows_only_answerable_gates(self, paused_run):
        pause = sym("gyrfalcon.flow.pause")
        with as_user("alice"):
            assert [t["flow_run_id"] for t in pause.list_pending()] == [paused_run]
        with as_user("bob"):
            assert pause.list_pending() == [], "inbox leaked another user's approval"

    def test_an_operator_sees_the_tenants_gates(self, paused_run):
        pause = sym("gyrfalcon.flow.pause")
        with as_user("ops", roles=["operator"]):
            assert len(pause.list_pending()) == 1

    def test_all_tenants_is_opt_in(self, paused_run):
        pause = sym("gyrfalcon.flow.pause")
        with as_user("bob", tenant="other"):
            assert pause.list_pending() == []
            assert len(pause.list_pending(all_tenants=True)) == 1


class TestLibraryUse:
    def test_with_no_store_there_is_nothing_to_protect(self):
        """Used as a plain library, with no persistence, resumption is open —
        there is no run row to own and no identity to check against."""
        pause = sym("gyrfalcon.flow.pause")
        set_store = sym("gyrfalcon.flow.store:set_store")
        states = sym("gyrfalcon.flow.states")
        set_store(None)
        pause._PAUSES["free"] = {
            "state": states.Paused(), "wait_for_input": None, "paused_at": 0.0,
        }
        try:
            with as_user("anyone"):
                assert pause.may_answer("free")
        finally:
            pause._PAUSES.pop("free", None)
