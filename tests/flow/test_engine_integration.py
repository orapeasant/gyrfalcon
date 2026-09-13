"""End-to-end behaviour and regressions found while implementing.

These go beyond the spec-derived acceptance cases: they cover paths the unit
tests exercise only in pieces (the orchestration client driving a real policy)
and pin bugs that shipped in the first implementation.
"""

from __future__ import annotations

import threading
import time

import pytest
from _spec import requires, sym

pytestmark = requires("gyrfalcon.flow:flow", section="§3, §4")


# ── regressions ───────────────────────────────────────────────────────────────

class TestRegressions:
    def test_map_over_a_generator_is_not_consumed_twice(self):
        """Regression: measuring length re-listed the argument, exhausting it."""
        task = sym("gyrfalcon.flow:task")

        @task
        def double(x):
            return x * 2

        futures = double.map(i for i in range(4))
        assert [f.result() for f in futures] == [0, 2, 4, 6]

    def test_state_constructors_accept_a_name_override(self):
        """Regression: `Failed(name=...)` raised TypeError, breaking CapAgentIterations."""
        states = sym("gyrfalcon.flow.states")
        s = states.Failed(name="IterationCapReached")
        assert s.name == "IterationCapReached"
        assert s.type.name == "FAILED"

    def test_distributed_future_polls_rather_than_snapshotting(self):
        """Regression: rehydrating right after submit reported Pending forever."""
        task = sym("gyrfalcon.flow:task")
        future_from_id = sym("gyrfalcon.flow.futures:future_from_id")

        @task
        def slow(x):
            time.sleep(0.02)
            return x + 1

        fut = slow.submit(1)
        assert future_from_id(fut.task_run_id).result(timeout=5) == 2

    def test_context_with_live_handles_is_serializable(self):
        """Regression: dumping a context holding a Flow object raised."""
        flow = sym("gyrfalcon.flow:flow")
        serialize = sym("gyrfalcon.flow.context:serialize_context")
        captured = {}

        @flow
        def f():
            captured["payload"] = serialize()

        f()
        payload = captured["payload"]["flow_run_context"]
        assert payload["flow_name"] == "f"
        assert "flow" not in payload, "live handles must not be shipped"


# ── the orchestration client driving a real policy ────────────────────────────

class TestPolicyIntegration:
    def test_cache_hit_short_circuits_into_cached(self):
        propose_state = sym("gyrfalcon.flow.orchestration:propose_state")
        Client = sym("gyrfalcon.flow.orchestration:LocalOrchestrationClient")
        registry = sym("gyrfalcon.flow.policies:registry")
        states = sym("gyrfalcon.flow.states")
        CACHE = sym("gyrfalcon.flow.cache:CACHE")

        CACHE["k-hit"] = "from-cache"
        client = Client(registry.get("core_task"))
        client.context["cache_key"] = "k-hit"

        result = propose_state(client, "run-1", states.Running())
        assert result.name == "Cached"
        assert result.data == "from-cache"

    def test_terminal_states_are_immutable(self):
        propose_state = sym("gyrfalcon.flow.orchestration:propose_state")
        Client = sym("gyrfalcon.flow.orchestration:LocalOrchestrationClient")
        registry = sym("gyrfalcon.flow.policies:registry")
        states = sym("gyrfalcon.flow.states")
        Abort = sym("gyrfalcon.flow.exceptions:Abort")

        client = Client(registry.get("core_task"))
        client.history.append(("r", states.Completed()))

        with pytest.raises(Abort, match="already terminal"):
            propose_state(client, "r", states.Running())

    def test_server_can_inject_a_retry_the_client_never_planned(self):
        """§4.2: REJECT-with-substitution is the point of the round trip."""
        propose_state = sym("gyrfalcon.flow.orchestration:propose_state")
        Client = sym("gyrfalcon.flow.orchestration:LocalOrchestrationClient")
        registry = sym("gyrfalcon.flow.policies:registry")
        states = sym("gyrfalcon.flow.states")

        client = Client(registry.get("core_flow"))
        client.history.append(("r", states.Running()))
        client.context.update({"retries": 0, "max_retries": 3})

        result = propose_state(client, "r", states.Failed())
        assert result.name == "AwaitingRetry"


# ── engine behaviour under composition ────────────────────────────────────────

class TestComposition:
    def test_flow_calling_a_failing_task_fails_rather_than_crashes(self):
        flow = sym("gyrfalcon.flow:flow")
        task = sym("gyrfalcon.flow:task")

        @task
        def bad():
            raise ValueError("inner boom")

        @flow
        def parent():
            return bad()

        state = parent(return_type="state")
        assert state.type.name == "FAILED", "a task error is not infrastructure failure"
        assert isinstance(state.exception, ValueError)

    def test_original_exception_type_survives_result(self):
        task = sym("gyrfalcon.flow:task")

        class Custom(Exception):
            ...

        @task
        def raiser():
            raise Custom("specific")

        with pytest.raises(Custom, match="specific"):
            raiser()

    def test_retry_delay_is_actually_waited_out(self):
        flow = sym("gyrfalcon.flow:flow")
        attempts = {"n": 0}

        @flow(retries=1, retry_delay_seconds=0.05)
        def delayed():
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise RuntimeError("retry me")
            return "ok"

        started = time.time()
        assert delayed() == "ok"
        assert time.time() - started >= 0.05

    def test_rollbacks_run_in_reverse_order(self):
        """Most recent effect is undone first."""
        task = sym("gyrfalcon.flow:task")
        transaction = sym("gyrfalcon.flow.transactions:transaction")
        order: list[str] = []

        @task
        def one():
            order.append("one")

        @one.on_rollback
        def _r1(txn):
            order.append("rollback-one")

        @task
        def two():
            order.append("two")

        @two.on_rollback
        def _r2(txn):
            order.append("rollback-two")

        @task
        def boom():
            raise RuntimeError("die")

        with pytest.raises(RuntimeError):
            with transaction():
                one()
                two()
                boom()

        assert order == ["one", "two", "rollback-two", "rollback-one"]

    def test_parallel_submissions_do_not_cross_talk(self):
        task = sym("gyrfalcon.flow:task")

        @task
        def add(a, b):
            return a + b

        futures = [add.submit(i, 1) for i in range(200)]
        assert [f.result() for f in futures] == [i + 1 for i in range(200)]

    def test_a_hanging_run_times_out_without_blocking_exit(self):
        flow = sym("gyrfalcon.flow:flow")

        @flow(timeout_seconds=0.05)
        def hangs():
            time.sleep(30)

        state = hangs(return_type="state")
        assert state.name == "TimedOut"
        # The abandoned worker must be a daemon or the process cannot exit.
        workers = [t for t in threading.enumerate() if t.name.startswith("Thread-")]
        assert all(t.daemon for t in workers if t.is_alive())

    def test_a_raising_retry_predicate_does_not_decide_the_run(self):
        """A broken predicate defaults to retrying rather than failing fast."""
        task = sym("gyrfalcon.flow:task")
        attempts = {"n": 0}

        def broken(t, engine, state):
            raise ValueError("predicate exploded")

        @task(retries=2, retry_condition_fn=broken)
        def t2():
            attempts["n"] += 1
            raise RuntimeError("boom")

        state = t2(return_type="state")
        assert state.type.name == "FAILED"
        assert attempts["n"] == 3

    def test_failure_hook_that_raises_does_not_mask_the_failure(self):
        flow = sym("gyrfalcon.flow:flow")

        def bad_hook(f, r, s):
            raise ValueError("hook boom")

        @flow(on_failure=[bad_hook])
        def f2():
            raise RuntimeError("original")

        state = f2(return_type="state")
        assert state.type.name == "FAILED"
        assert isinstance(state.exception, RuntimeError)
        assert str(state.exception) == "original"
