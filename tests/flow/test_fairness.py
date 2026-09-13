"""Per-tenant fairness — §17.8.

"Many flows running at the same time" only becomes a scheduling problem once
there is more than one tenant. A single process-global pool means one tenant
submitting a thousand tasks fills every worker and everyone else waits.
"""

from __future__ import annotations

import threading
import time

import pytest
from _spec import requires, sym

pytestmark = requires("gyrfalcon.flow.futures:ThreadPoolTaskRunner", section="§17.8")


def as_user(user, tenant, roles=()):
    ident = sym("gyrfalcon.identity")
    return ident.use_principal(
        ident.Principal(user_id=user, tenant_id=tenant, roles=roles)
    )


@pytest.fixture()
def multi_tenant(monkeypatch):
    """Turn identity on; the caps are deliberately inert without it."""
    ident = sym("gyrfalcon.identity")
    monkeypatch.setattr(ident, "identity_enabled", lambda: True)
    yield


class TestTaskPermits:
    def test_no_cap_for_a_single_user_install(self):
        """A personal install has nobody to be fair to."""
        Runner = sym("gyrfalcon.flow.futures:ThreadPoolTaskRunner")
        assert Runner(max_workers=8)._tenant_limit() == 0

    def test_multi_tenant_defaults_to_half_the_pool(self, multi_tenant):
        """Below the pool size on purpose: at exactly the pool size one tenant
        could still occupy every worker."""
        Runner = sym("gyrfalcon.flow.futures:ThreadPoolTaskRunner")
        r = Runner(max_workers=8)
        assert r._tenant_limit() == 4
        assert r._tenant_limit() < r.max_workers

    def test_an_explicit_cap_is_honoured(self, multi_tenant, monkeypatch):
        futures = sym("gyrfalcon.flow.futures")
        import gyrfalcon.config as cfg
        monkeypatch.setattr(
            cfg, "cfg_get",
            lambda path, default=None: 2 if path.endswith("tenant_max_concurrent_tasks") else default,
        )
        assert futures.ThreadPoolTaskRunner(max_workers=8)._tenant_limit() == 2

    def test_each_tenant_gets_its_own_permit(self, multi_tenant):
        Runner = sym("gyrfalcon.flow.futures:ThreadPoolTaskRunner")
        r = Runner(max_workers=4)
        a = r._permit("acme")
        b = r._permit("other")
        assert a is not b
        assert r._permit("acme") is a, "permits must be stable per tenant"

    def test_a_tenant_cannot_exceed_its_share(self, multi_tenant, monkeypatch):
        """The mechanism itself: hold every permit, prove the next blocks."""
        futures = sym("gyrfalcon.flow.futures")
        import gyrfalcon.config as cfg
        monkeypatch.setattr(
            cfg, "cfg_get",
            lambda path, default=None: 2 if path.endswith("tenant_max_concurrent_tasks") else default,
        )
        r = futures.ThreadPoolTaskRunner(max_workers=8)
        sem = r._permit("acme")
        assert sem.acquire(blocking=False)
        assert sem.acquire(blocking=False)
        assert not sem.acquire(blocking=False), "third task exceeded the tenant cap"
        # ...and another tenant is unaffected, which is the whole point
        assert r._permit("other").acquire(blocking=False)

    def test_permits_are_released_when_tasks_finish(self, multi_tenant, monkeypatch):
        futures = sym("gyrfalcon.flow.futures")
        task = sym("gyrfalcon.flow:task")
        flow = sym("gyrfalcon.flow:flow")
        import gyrfalcon.config as cfg
        monkeypatch.setattr(
            cfg, "cfg_get",
            lambda path, default=None: 1 if path.endswith("tenant_max_concurrent_tasks") else default,
        )
        runner = futures.ThreadPoolTaskRunner(max_workers=4)
        monkeypatch.setattr(futures, "_DEFAULT_RUNNER", runner)

        @task
        def quick(x):
            time.sleep(0.01)
            return x

        @flow
        def many():
            return [f.result() for f in [quick.submit(i) for i in range(6)]]

        with as_user("alice", "acme"):
            # A cap of 1 with six tasks only completes if every permit comes
            # back; a leak would stall submission forever.
            #
            # Run inline rather than on a helper thread: a bare Thread starts
            # with an empty context and would lose the principal before
            # `submit()` ever asks for it — the same trap test_identity.py
            # documents.
            done = []
            watchdog = threading.Timer(20.0, lambda: done.append("TIMEOUT"))
            watchdog.start()
            try:
                result = many()
            finally:
                watchdog.cancel()
            assert not done, "permit leak: submission stalled"
            assert result == list(range(6))


class TestTenantRunCap:
    def test_disabled_for_single_user(self):
        store = sym("gyrfalcon.flow.store")
        assert store._tenant_run_cap() == 0

    def test_reservation_respects_the_tenant_cap(self, multi_tenant, monkeypatch,
                                                 make_store):
        store_mod = sym("gyrfalcon.flow.store")
        monkeypatch.setattr(store_mod, "_tenant_run_cap", lambda: 2)
        s = make_store()
        with as_user("alice", "acme"):
            granted = [s.reserve_run_slot(f"flow{i}", None) for i in range(4)]
        assert sum(g is not None for g in granted) == 2, granted

    def test_the_cap_is_per_tenant_not_global(self, multi_tenant, monkeypatch,
                                              make_store):
        store_mod = sym("gyrfalcon.flow.store")
        monkeypatch.setattr(store_mod, "_tenant_run_cap", lambda: 1)
        s = make_store()
        with as_user("alice", "acme"):
            assert s.reserve_run_slot("a", None) is not None
            assert s.reserve_run_slot("a", None) is None
        with as_user("bob", "other"):
            assert s.reserve_run_slot("b", None) is not None, (
                "one tenant's cap must not throttle another"
            )


class TestEnginePersist:
    def test_persist_can_be_set_per_engine(self):
        """It was a class attribute, which is a shared mutable global once more
        than one tenant is in the process (§17.8)."""
        engine_mod = sym("gyrfalcon.flow.engine")
        flow = sym("gyrfalcon.flow:flow")

        @flow
        def f():
            return 1

        a = engine_mod.FlowRunEngine(f, {}, persist=True)
        b = engine_mod.FlowRunEngine(f, {}, persist=False)
        assert a.persist is True
        assert b.persist is False
        assert engine_mod._BaseRunEngine.persist is False, "class default untouched"
