"""Phases 6–7 — futures, map, results, caching, transactions.

Spec: 15-flow.md §5.1–5.4, §6.1–6.3, §13.1(7,8,9).
"""

from __future__ import annotations

import pytest
from _spec import requires, sym

pytestmark = requires(
    "gyrfalcon.flow:task",
    "gyrfalcon.flow.cache:CachePolicy",
    section="§5, §6",
)


# ── §5.1 futures ──────────────────────────────────────────────────────────────

class TestFutures:
    def test_future_identity_is_a_run_id_not_an_object_handle(self):
        """§5.1: identity is server-side, so a future survives a process boundary."""
        task = sym("gyrfalcon.flow:task")

        @task
        def t(x):
            return x

        fut = t.submit(1)
        assert isinstance(fut.task_run_id, str) and fut.task_run_id

    def test_future_is_reconstructable_from_its_id(self):
        rehydrate = sym("gyrfalcon.flow.futures:future_from_id")
        task = sym("gyrfalcon.flow:task")

        @task
        def t(x):
            return x * 3

        fut = t.submit(7)
        assert rehydrate(fut.task_run_id).result() == 21


# ── §5.3 map and the annotation vocabulary ────────────────────────────────────

class TestMapAnnotations:
    def test_unmapped_broadcasts_instead_of_iterating(self):
        task = sym("gyrfalcon.flow:task")
        unmapped = sym("gyrfalcon.flow.annotations:unmapped")

        @task
        def t(item, config):
            return (item, config)

        futures = t.map([1, 2, 3], config=unmapped({"k": "v"}))
        assert [f.result() for f in futures] == [
            (1, {"k": "v"}), (2, {"k": "v"}), (3, {"k": "v"})
        ]

    def test_map_raises_when_nothing_is_iterable(self):
        task = sym("gyrfalcon.flow:task")
        unmapped = sym("gyrfalcon.flow.annotations:unmapped")
        MappingMissingIterable = sym("gyrfalcon.flow.exceptions:MappingMissingIterable")

        @task
        def t(a, b):
            return a

        with pytest.raises(MappingMissingIterable):
            t.map(unmapped(1), b=unmapped(2))

    def test_allow_failure_passes_a_failed_upstream_through(self):
        """Without it, a failed upstream propagates instead of being handled."""
        task = sym("gyrfalcon.flow:task")
        allow_failure = sym("gyrfalcon.flow.annotations:allow_failure")

        @task
        def boom():
            raise RuntimeError("upstream failed")

        @task
        def downstream(up):
            return type(up).__name__

        fut = boom.submit()
        assert downstream(allow_failure(fut)) == "RuntimeError"


# ── §5.4 dependencies ─────────────────────────────────────────────────────────

class TestDependencies:
    def test_passing_a_future_creates_an_implicit_edge(self):
        task = sym("gyrfalcon.flow:task")
        graph_edges = sym("gyrfalcon.flow.futures:edges_for")

        @task
        def upstream():
            return 1

        @task
        def downstream(x):
            return x + 1

        up = upstream.submit()
        down = downstream.submit(up)
        assert up.task_run_id in graph_edges(down.task_run_id)

    def test_wait_for_creates_ordering_without_data_flow(self):
        task = sym("gyrfalcon.flow:task")
        graph_edges = sym("gyrfalcon.flow.futures:edges_for")

        @task
        def a():
            return 1

        @task
        def b():
            return 2

        fa = a.submit()
        fb = b.submit(wait_for=[fa])
        assert fa.task_run_id in graph_edges(fb.task_run_id)

    def test_blocked_is_visibly_distinct_from_broken(self):
        """§5.4: an unready upstream yields Pending(name='NotReady'), not Failed."""
        build = sym("gyrfalcon.flow.engine:state_for_upstream_error")
        UpstreamTaskError = sym("gyrfalcon.flow.exceptions:UpstreamTaskError")

        state = build(UpstreamTaskError("not ready"))
        assert state.type.name == "PENDING"
        assert state.name == "NotReady"


# ── §6.2 cache policies ───────────────────────────────────────────────────────

class TestCachePolicies:
    def test_policies_compose_with_plus(self):
        policies = sym("gyrfalcon.flow.cache")
        compound = policies.INPUTS + policies.TASK_SOURCE
        assert isinstance(compound, policies.CompoundCachePolicy)

    def test_default_is_inputs_plus_source_plus_run_id(self):
        policies = sym("gyrfalcon.flow.cache")
        parts = {type(p).__name__ for p in policies.DEFAULT.policies}
        assert {"Inputs", "TaskSource", "RunId"} <= parts

    def test_no_cache_returns_none(self):
        policies = sym("gyrfalcon.flow.cache")
        assert policies.NO_CACHE.compute_key(None, {}, {}) is None

    def test_same_inputs_same_key(self):
        policies = sym("gyrfalcon.flow.cache")
        a = policies.INPUTS.compute_key(None, {"x": 1}, {})
        b = policies.INPUTS.compute_key(None, {"x": 1}, {})
        assert a == b

    def test_different_inputs_different_key(self):
        policies = sym("gyrfalcon.flow.cache")
        a = policies.INPUTS.compute_key(None, {"x": 1}, {})
        b = policies.INPUTS.compute_key(None, {"x": 2}, {})
        assert a != b

    def test_editing_the_function_invalidates_its_cache(self):
        """§6.2: including TASK_SOURCE by default is 'a strong opinion and the correct one'."""
        policies = sym("gyrfalcon.flow.cache")

        def v1(x):
            return x + 1

        def v2(x):
            return x + 2

        k1 = policies.TASK_SOURCE.compute_key(_ctx(v1), {}, {})
        k2 = policies.TASK_SOURCE.compute_key(_ctx(v2), {}, {})
        assert k1 != k2

    def test_a_raising_policy_degrades_to_no_caching(self):
        """§6.2: correct failure mode — log and return None, never fail the run."""
        compute = sym("gyrfalcon.flow.cache:compute_transaction_key")
        CachePolicy = sym("gyrfalcon.flow.cache:CachePolicy")

        class Exploding(CachePolicy):
            def compute_key(self, task_ctx, inputs, flow_parameters, **kw):
                raise ValueError("bad policy")

        assert compute(Exploding(), None, {}, {}) is None

    def test_cache_policy_forces_result_persistence(self):
        """§6.1: caching without persistence is meaningless."""
        task = sym("gyrfalcon.flow:task")
        policies = sym("gyrfalcon.flow.cache")

        @task(cache_policy=policies.INPUTS, persist_result=False)
        def t(x):
            return x

        assert t.persist_result is True


# ── §6.3 transactions ─────────────────────────────────────────────────────────

class TestTransactions:
    def test_state_progression(self):
        TransactionState = sym("gyrfalcon.flow.transactions:TransactionState")
        assert [s.name for s in TransactionState] == [
            "PENDING", "ACTIVE", "STAGED", "COMMITTED", "ROLLED_BACK",
        ]

    def test_child_get_falls_through_to_parent(self):
        transaction = sym("gyrfalcon.flow.transactions:transaction")

        with transaction() as outer:
            outer.set("k", "from-parent")
            with transaction() as inner:
                assert inner.get("k") == "from-parent"

    def test_later_failure_rolls_back_earlier_step(self, recorder):
        """§6.3: the compensating-action pattern."""
        transaction = sym("gyrfalcon.flow.transactions:transaction")
        task = sym("gyrfalcon.flow:task")

        @task
        def step_one():
            recorder.record("one")

        @step_one.on_rollback
        def _rb(txn):
            recorder.record("rollback-one")

        @task
        def step_two():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            with transaction():
                step_one()
                step_two()

        assert recorder.labels == ["one", "rollback-one"]

    def test_rolled_back_run_is_completed_typed(self):
        """§6.3: the run did succeed; its effects were undone."""
        states = sym("gyrfalcon.flow.states")
        assert states.RolledBack().type is states.StateType.COMPLETED


def _ctx(fn):
    """Minimal task context carrying the function, for TASK_SOURCE hashing."""
    TaskCtx = sym("gyrfalcon.flow.context:TaskRunContext")
    return TaskCtx.model_construct(task=type("T", (), {"fn": staticmethod(fn)})())
