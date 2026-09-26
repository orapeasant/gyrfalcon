"""Identity and its propagation — §17.3.

The interesting tests here are not "does a dataclass hold fields". They are
the ones about a principal surviving a thread boundary, because that is the
failure this codebase has already shipped twice with a different contextvar
(task pool linkage in `futures.py`, timeout workers in `engine.py`). Losing a
parent link produces a wrong graph; losing a principal produces one tenant
reading another's data, silently.
"""

from __future__ import annotations

import pytest
from _spec import requires, sym

pytestmark = requires("gyrfalcon.identity:Principal", section="§17.3 identity")


class TestPrincipal:
    def test_tenant_defaults_to_the_user_so_personal_installs_are_not_special(self):
        Principal = sym("gyrfalcon.identity:Principal")
        assert Principal(user_id="u1").tenant_id == "u1"

    def test_an_explicit_tenant_is_kept(self):
        Principal = sym("gyrfalcon.identity:Principal")
        assert Principal(user_id="u1", tenant_id="acme").tenant_id == "acme"

    def test_an_empty_user_id_is_rejected(self):
        Principal = sym("gyrfalcon.identity:Principal")
        with pytest.raises(ValueError):
            Principal(user_id="")

    def test_roles_normalise_to_a_frozenset(self):
        Principal = sym("gyrfalcon.identity:Principal")
        p = Principal(user_id="u1", roles=["operator", "operator"])
        assert p.roles == frozenset({"operator"})

    def test_operator_and_admin_both_grant_tenant_wide_sight(self):
        Principal = sym("gyrfalcon.identity:Principal")
        assert Principal(user_id="a", roles=["operator"]).is_operator
        assert Principal(user_id="b", roles=["admin"]).is_operator
        assert not Principal(user_id="c").is_operator

    def test_a_principal_is_immutable(self):
        Principal = sym("gyrfalcon.identity:Principal")
        with pytest.raises(Exception):
            Principal(user_id="u1").user_id = "u2"


class TestBinding:
    def test_binding_is_scoped_and_restores(self):
        ident = sym("gyrfalcon.identity")
        p = ident.Principal(user_id="alice")
        assert ident.get_principal() is None
        with ident.use_principal(p):
            assert ident.get_principal() is p
        assert ident.get_principal() is None

    def test_nesting_restores_the_outer_identity(self):
        ident = sym("gyrfalcon.identity")
        a = ident.Principal(user_id="a")
        b = ident.Principal(user_id="b")
        with ident.use_principal(a):
            with ident.use_principal(b):
                assert ident.require_principal().user_id == "b"
            assert ident.require_principal().user_id == "a"

    def test_single_user_mode_resolves_to_local(self, monkeypatch):
        ident = sym("gyrfalcon.identity")
        monkeypatch.setattr(ident, "identity_enabled", lambda: False)
        assert ident.require_principal().user_id == "local"

    def test_identity_enabled_makes_absence_an_error(self, monkeypatch):
        """The rule that keeps a dropped principal from becoming a data leak."""
        ident = sym("gyrfalcon.identity")
        monkeypatch.setattr(ident, "identity_enabled", lambda: True)
        with pytest.raises(ident.MissingPrincipalError):
            ident.require_principal()

    def test_system_must_be_asked_for_explicitly(self):
        """Background work says it is the platform; it never inherits that."""
        ident = sym("gyrfalcon.identity")
        with ident.as_system():
            assert ident.require_principal().user_id == "system"
        assert ident.get_principal() is None


class TestPropagation:
    """The part that has actually broken before."""

    def test_a_plain_thread_does_not_inherit(self):
        """Documents the trap rather than pretending it does not exist."""
        import threading

        ident = sym("gyrfalcon.identity")
        seen = {}
        with ident.use_principal(ident.Principal(user_id="alice")):
            t = threading.Thread(target=lambda: seen.update(p=ident.get_principal()))
            t.start()
            t.join()
        assert seen["p"] is None, (
            "a bare Thread starts with an empty context — machinery that "
            "crosses threads must copy it explicitly"
        )

    def test_copy_context_carries_the_principal(self):
        import contextvars
        import threading

        ident = sym("gyrfalcon.identity")
        seen = {}
        with ident.use_principal(ident.Principal(user_id="alice")):
            ctx = contextvars.copy_context()
            t = threading.Thread(
                target=lambda: ctx.run(lambda: seen.update(p=ident.get_principal()))
            )
            t.start()
            t.join()
        assert seen["p"].user_id == "alice"

    def test_a_submitted_task_keeps_the_callers_identity(self):
        """futures.py's ThreadPoolTaskRunner already copies the context; the
        principal must ride along with no further work."""
        ident = sym("gyrfalcon.identity")
        flow = sym("gyrfalcon.flow:flow")
        activity = sym("gyrfalcon.flow:activity")

        @activity
        def who() -> str:
            p = ident.get_principal()
            return p.user_id if p else "<lost>"

        @flow
        def parent() -> list[str]:
            return [f.result() for f in [who.submit(), who.submit()]]

        with ident.use_principal(ident.Principal(user_id="alice")):
            assert parent() == ["alice", "alice"]

    def test_a_timed_flow_body_keeps_the_identity(self):
        """A flow that merely sets timeout_seconds runs its body on a worker
        thread. That path used to drop the whole run context."""
        ident = sym("gyrfalcon.identity")
        flow = sym("gyrfalcon.flow:flow")

        @flow(timeout_seconds=30)
        def timed() -> str:
            p = ident.get_principal()
            return p.user_id if p else "<lost>"

        with ident.use_principal(ident.Principal(user_id="alice")):
            assert timed() == "alice"

    def test_a_timed_flow_keeps_its_run_context(self):
        """Regression: the same worker thread also carried the flow context,
        and losing it orphaned every task the flow called."""
        flow = sym("gyrfalcon.flow:flow")
        get_flow_run_context = sym("gyrfalcon.flow.context:get_flow_run_context")

        @flow(timeout_seconds=30)
        def timed() -> bool:
            return get_flow_run_context() is not None

        assert timed() is True


class TestOwnershipInheritance:
    """§17.4: whoever started the flow owns everything it produces. Checked
    against the persisted row itself, not the contextvar — `TestPropagation`
    above already covers the contextvar surviving a thread hop; the risk left
    open is a persisted `user_id`/`tenant_id` that disagrees with it because
    something re-derived ownership instead of inheriting it for free.
    """

    @pytest.fixture()
    def store(self, make_store):
        set_store = sym("gyrfalcon.flow.store:set_store")
        engine = sym("gyrfalcon.flow.engine:_BaseRunEngine")
        s = make_store()
        set_store(s)
        engine.persist = True
        yield s
        engine.persist = False
        set_store(None)

    def _as(self, user, tenant="acme"):
        ident = sym("gyrfalcon.identity")
        return ident.use_principal(ident.Principal(user_id=user, tenant_id=tenant))

    def test_a_submitted_tasks_persisted_row_is_owned_by_the_flows_starter(self, store):
        flow = sym("gyrfalcon.flow:flow")
        activity = sym("gyrfalcon.flow:activity")

        @activity
        def child(i):
            return i

        @flow
        def parent():
            return child.submit().result()

        with self._as("alice", "acme"):
            state = parent(return_type="state")

        Scope = sym("gyrfalcon.db.scope:Scope")
        rows, total = store.list_runs(scope=Scope.system(reason="test"))
        children = [r for r in rows if r["parent_run_id"] == state.id]
        assert len(children) == 1
        assert children[0]["user_id"] == "alice"
        assert children[0]["tenant_id"] == "acme"

    def test_a_subflows_persisted_row_is_owned_by_the_parents_starter(self, store):
        """A flow calling another @flow directly — not a submitted task."""
        flow = sym("gyrfalcon.flow:flow")

        @flow
        def child():
            return 1

        @flow
        def parent():
            return child(return_type="state")

        with self._as("bob", "acme"):
            outer = parent(return_type="state")

        # A subflow does not set `flow_run_id` (that field is task-only —
        # see `engine._register_run`); `parent_run_id` is set for both kinds
        # and is what actually links a subflow to its caller.
        Scope = sym("gyrfalcon.db.scope:Scope")
        rows, total = store.list_runs(scope=Scope.system(reason="test"))
        children = [r for r in rows if r["parent_run_id"] == outer.id]
        assert len(children) == 1
        assert children[0]["user_id"] == "bob"
        assert children[0]["tenant_id"] == "acme"

    def test_ownership_is_unchanged_across_a_retry(self, store):
        """A retried run is the same row, re-transitioned — not a new one — so
        its ownership must not move to whoever happens to be current when the
        retry fires."""
        flow = sym("gyrfalcon.flow:flow")

        @flow(retries=1)
        def flaky():
            raise RuntimeError("always")

        with self._as("carol", "acme"):
            state = flaky(return_type="state")

        row = store.get_run(state.id, sym("gyrfalcon.db.scope:Scope").system(reason="test"))
        assert row["user_id"] == "carol"
        assert row["tenant_id"] == "acme"

    def test_a_run_resumed_after_a_pause_keeps_its_original_owner(self, store):
        """Resuming answers the pause; it must not re-attribute the run to
        whoever happens to answer it (§17.6 keeps "may see" and "may act"
        distinct, but ownership of the *run* itself is narrower still — it
        never moves to the answerer)."""
        pause = sym("gyrfalcon.flow.pause")
        states = sym("gyrfalcon.flow.states")

        with self._as("dave", "acme"):
            store.create_run("gate", "spend_request", "flow")
            store.record_transition("gate", states.Running())
        pause._PAUSES["gate"] = {
            "state": states.Paused(), "wait_for_input": None, "paused_at": 0.0,
        }
        try:
            with self._as("ops", "acme"):
                pause.resume_flow_run("gate", run_input={"approve": True}, authorize=False)

            row = store.get_run("gate", sym("gyrfalcon.db.scope:Scope").system(reason="test"))
            assert row["user_id"] == "dave", "resuming must not reassign ownership to the answerer"
            assert row["tenant_id"] == "acme"
        finally:
            pause._PAUSES.pop("gate", None)
