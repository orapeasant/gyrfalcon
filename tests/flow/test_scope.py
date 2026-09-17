"""Tenant scoping — §17.5.

Two kinds of test here, and the second is the point.

The behavioural ones check that one tenant cannot see another's rows. The
*mechanical* ones read `sql.py` and the store modules as text and assert
structural properties: every read of an owned table is scope-aware, and no
store assembles a WHERE clause of its own. Behavioural tests only cover the
queries someone remembered to write a test for; the mechanical ones cover the
query added next month by someone who has never read this file.
"""

from __future__ import annotations

import inspect
import re

import pytest
from _spec import requires, sym

pytestmark = requires("gyrfalcon.flow.db.scope:Scope", section="§17.5 scoping")

OWNED_TABLES = (
    "flow_runs", "flow_events", "flow_deployments",
    # Tenant configuration for navigation/access control. `nav_pages` is
    # deliberately absent: it is global (a route either shipped or it did
    # not), so a bare constant read of it is correct rather than a gap.
    "nav_functions", "nav_menus", "nav_menu_items", "auth_roles",
)


# ── mechanical ───────────────────────────────────────────────────────────────

class TestStructure:
    def test_only_one_place_builds_a_where_clause(self):
        """`WHERE` is assembled in `scope.merge` and nowhere else.

        This is what makes "is every query filtered" answerable by reading one
        function instead of auditing every call site.
        """
        offenders = []
        for mod in ("gyrfalcon.flow.store", "gyrfalcon.flow.events",
                    "gyrfalcon.flow.deployments", "gyrfalcon.flow.db.sql"):
            src = inspect.getsource(sym(mod))
            for lineno, line in enumerate(src.splitlines(), 1):
                if re.search(r'["\']WHERE\s+["\' ]', line) or '"WHERE " +' in line:
                    offenders.append(f"{mod}:{lineno}: {line.strip()}")
        assert not offenders, (
            "WHERE clauses must be composed via scope.merge()/sql.compose_where():\n"
            + "\n".join(offenders)
        )

    def test_every_read_of_an_owned_table_is_scope_aware(self):
        """No SELECT against an owned table may be a bare module constant.

        A constant cannot carry a scope, so a `SELECT * FROM flow_runs` stored
        as a string is a filter that can never be applied. Reads must be
        builders that take a Scope.
        """
        sql_mod = sym("gyrfalcon.flow.db.sql")
        offenders = []
        for name, value in vars(sql_mod).items():
            if not isinstance(value, str) or name.startswith("_"):
                continue
            if not re.search(r"\bSELECT\b", value, re.I):
                continue
            for table in OWNED_TABLES:
                if re.search(rf"\bFROM\s+{table}\b", value, re.I):
                    offenders.append(f"{name} = {value.strip()[:70]}")
        assert not offenders, (
            "these reads are constants and so can never be tenant-scoped; "
            "make them functions taking a Scope:\n" + "\n".join(offenders)
        )

    def test_scope_taking_builders_emit_the_predicate(self):
        """Every builder that accepts a Scope must actually use it."""
        sql_mod = sym("gyrfalcon.flow.db.sql")
        Scope = sym("gyrfalcon.flow.db.scope:Scope")
        checked = 0
        for name, fn in vars(sql_mod).items():
            if not callable(fn) or name.startswith("_"):
                continue
            try:
                params = inspect.signature(fn).parameters
            except (TypeError, ValueError):
                continue
            if "scope" not in params:
                continue
            args = [Scope(tenant_id="t1", user_id="u1")]
            args += [2] * (len(params) - 1)
            try:
                stmt, bound = fn(*args)
            except TypeError:
                continue
            checked += 1
            assert "tenant_id = ?" in stmt, f"{name} ignores its scope: {stmt}"
            assert "t1" in bound, f"{name} did not bind the tenant: {bound}"
        assert checked >= 8, f"expected to check most builders, checked {checked}"

    def test_a_system_scope_must_state_a_reason(self):
        Scope = sym("gyrfalcon.flow.db.scope:Scope")
        with pytest.raises(ValueError):
            Scope.system(reason="")
        assert Scope.system(reason="migrations").is_system

    def test_every_system_scope_in_the_package_names_a_reason(self):
        """Cross-tenant reads stay greppable and reviewed."""
        for mod in ("gyrfalcon.flow.store", "gyrfalcon.flow.deployments",
                    "gyrfalcon.flow.events"):
            src = inspect.getsource(sym(mod))
            for call in re.findall(r"Scope\.system\((.*?)\)", src, re.S):
                assert "reason=" in call, f"{mod}: Scope.system() without a reason"


# ── behavioural ──────────────────────────────────────────────────────────────

class TestPredicate:
    def test_a_person_sees_only_their_own_rows(self):
        Scope = sym("gyrfalcon.flow.db.scope:Scope")
        Principal = sym("gyrfalcon.identity:Principal")
        s = Scope.of(Principal(user_id="alice", tenant_id="acme"))
        clauses, params = s.predicate()
        assert clauses == ["tenant_id = ?", "user_id = ?"]
        assert params == ["acme", "alice"]

    def test_an_operator_sees_the_whole_tenant(self):
        Scope = sym("gyrfalcon.flow.db.scope:Scope")
        Principal = sym("gyrfalcon.identity:Principal")
        s = Scope.of(Principal(user_id="ops", tenant_id="acme", roles=["operator"]))
        assert s.predicate() == (["tenant_id = ?"], ["acme"])

    def test_an_operator_is_still_confined_to_their_tenant(self):
        Scope = sym("gyrfalcon.flow.db.scope:Scope")
        Principal = sym("gyrfalcon.identity:Principal")
        s = Scope.of(Principal(user_id="ops", tenant_id="acme", roles=["admin"]))
        assert not s.is_system, "operator is not a cross-tenant super-user"

    def test_system_scope_has_no_predicate(self):
        Scope = sym("gyrfalcon.flow.db.scope:Scope")
        assert Scope.system(reason="test").predicate() == ([], [])

    def test_owns_checks_a_row_in_hand(self):
        Scope = sym("gyrfalcon.flow.db.scope:Scope")
        s = Scope(tenant_id="acme", user_id="alice")
        assert s.owns({"tenant_id": "acme", "user_id": "alice"})
        assert not s.owns({"tenant_id": "acme", "user_id": "bob"})
        assert not s.owns({"tenant_id": "other", "user_id": "alice"})
        assert not s.owns(None)


class TestIsolation:
    """Two tenants, one database."""

    @pytest.fixture()
    def store(self, make_store):
        set_store = sym("gyrfalcon.flow.store:set_store")
        s = make_store()
        set_store(s)
        yield s
        set_store(None)
        s.close()

    def _as(self, user, tenant, roles=()):
        ident = sym("gyrfalcon.identity")
        return ident.use_principal(
            ident.Principal(user_id=user, tenant_id=tenant, roles=roles)
        )

    def test_a_run_is_invisible_to_another_tenant(self, store):
        with self._as("alice", "acme"):
            store.create_run("r-acme", "secret", "flow")
            assert store.get_run("r-acme") is not None
        with self._as("bob", "other"):
            assert store.get_run("r-acme") is None, "cross-tenant read by id"
            assert store.list_runs(limit=50)[1] == 0

    def test_a_run_is_invisible_to_a_peer_in_the_same_tenant(self, store):
        with self._as("alice", "acme"):
            store.create_run("r-alice", "mine", "flow")
        with self._as("bob", "acme"):
            assert store.get_run("r-alice") is None
        with self._as("ops", "acme", roles=["operator"]):
            assert store.get_run("r-alice") is not None, "operator sees the tenant"

    def test_history_follows_the_runs_visibility(self, store):
        states = sym("gyrfalcon.flow.states")
        with self._as("alice", "acme"):
            store.create_run("r-hist", "mine", "flow")
            store.record_transition("r-hist", states.Running())
            assert len(store.get_history("r-hist")) == 1
        with self._as("bob", "other"):
            assert store.get_history("r-hist") == []

    def test_aggregates_are_scoped(self, store):
        with self._as("alice", "acme"):
            store.create_run("a1", "x", "flow")
            store.create_run("a2", "x", "flow")
        with self._as("bob", "other"):
            store.create_run("b1", "x", "flow")
            assert sum(store.counts_by_state().values()) == 1
            assert store.count_active("x") == 1
        with self._as("alice", "acme"):
            assert store.count_active("x") == 2

    def test_the_graph_is_scoped(self, store):
        with self._as("alice", "acme"):
            store.create_run("g1", "parent", "flow")
        with self._as("bob", "other"):
            assert store.get_graph("g1")["nodes"] == []

    def test_history_buckets_do_not_count_another_tenants_runs(self, store):
        """§11's pre-bucketed aggregates must not leak a count either — the UI
        never re-aggregates what it's handed, so a stray count here would
        render directly."""
        with self._as("alice", "acme"):
            store.create_run("hb1", "x", "flow")
        with self._as("bob", "other"):
            store.create_run("hb2", "x", "flow")
            store.create_run("hb3", "x", "flow")
            buckets = store.history_buckets(hours=1, buckets=4)
            assert sum(sum(b["counts"].values()) for b in buckets) == 2
        with self._as("alice", "acme"):
            buckets = store.history_buckets(hours=1, buckets=4)
            assert sum(sum(b["counts"].values()) for b in buckets) == 1

    def test_reconciliation_still_crosses_tenants(self, store, make_store):
        """A crashed process took every tenant's runs down with it."""
        states = sym("gyrfalcon.flow.states")
        with self._as("alice", "acme"):
            store.create_run("x1", "orphan", "flow")
            store.record_transition("x1", states.Running())
        store.close()

        second = make_store(reconcile=True)
        with self._as("alice", "acme"):
            assert second.get_run("x1")["state_type"] == "CRASHED"
