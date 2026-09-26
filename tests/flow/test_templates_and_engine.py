"""Phases 2–3 — templates, the engine loop, retries, timeouts, hooks.

Spec: 15-flow.md §1.1, §1.2, §3.1–3.5, §13.1(1,3,6), §13.2.
"""

from __future__ import annotations

import threading

from _spec import requires, sym

pytestmark = requires(
    "gyrfalcon.flow:flow",
    "gyrfalcon.flow:activity",
    section="§1.2, §3.1",
)


# ── §1.1 templates carry no run state ─────────────────────────────────────────

class TestTemplateRunSplit:
    def test_template_has_no_mutable_execution_state(self):
        """§1.1: 'do not put mutable execution state on your flow object.'"""
        flow = sym("gyrfalcon.flow:flow")

        @flow
        def f():
            return 1

        f()
        f()
        for banned in ("state", "_state", "run_id", "_return_value", "_raised"):
            assert not hasattr(f, banned), f"template exposes run state: {banned}"

    def test_template_is_reentrant_under_concurrency(self):
        """One template, many concurrent runs, no cross-talk."""
        flow = sym("gyrfalcon.flow:flow")

        @flow
        def echo(x):
            return x * 2

        results: dict[int, int] = {}
        errors: list[BaseException] = []

        def worker(i: int):
            try:
                results[i] = echo(i)
            except BaseException as e:      # noqa: BLE001 - surfacing to assert
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert results == {i: i * 2 for i in range(16)}

    def test_with_options_returns_a_copy(self):
        """§1.2: callers reconfigure without redefining."""
        flow = sym("gyrfalcon.flow:flow")

        @flow(retries=0, name="base")
        def f():
            return 1

        variant = f.with_options(retries=3, name="variant")
        assert variant is not f
        assert f.retries == 0 and f.name == "base"
        assert variant.retries == 3 and variant.name == "variant"


class TestHookRegistration:
    def test_hooks_accepted_as_decorator_kwargs(self):
        activity = sym("gyrfalcon.flow:activity")
        calls = []

        @activity(on_rollback=[lambda t: calls.append("rollback")])
        def t():
            return 1

        assert t.on_rollback_hooks

    def test_hooks_accepted_as_decorator_methods(self):
        """§13.2: fix the kwarg/method asymmetry — both forms must work."""
        activity = sym("gyrfalcon.flow:activity")

        @activity
        def t():
            return 1

        @t.on_rollback
        def _rb(txn):
            ...

        assert t.on_rollback_hooks


# ── §3.1–3.2 the loop ─────────────────────────────────────────────────────────

class TestEngineLoop:
    def test_returned_none_is_distinct_from_never_returned(self):
        """§3.2: the NotSet sentinel pair. None is a legitimate result."""
        flow = sym("gyrfalcon.flow:flow")

        @flow
        def returns_none():
            return None

        state = returns_none(return_type="state")
        assert state.is_completed()
        assert state.result() is None

    def test_retry_is_a_state_transition_not_recursion(self, flaky):
        """§3.1: a failure handler re-satisfies `while engine.is_running()`."""
        flow = sym("gyrfalcon.flow:flow")
        fn = flaky(fail_times=2)

        @flow(retries=3)
        def f():
            return fn()

        assert f() == "ok"
        assert fn.attempts == 3

    def test_retries_exhausted_yields_failed(self, flaky):
        flow = sym("gyrfalcon.flow:flow")
        fn = flaky(fail_times=5)

        @flow(retries=2)
        def f():
            return fn()

        state = f(return_type="state")
        assert state.type.name == "FAILED"
        assert fn.attempts == 3          # initial + 2 retries


# ── §3.3 the retry decision ───────────────────────────────────────────────────

class TestRetryPolicy:
    def test_delay_list_is_a_backoff_schedule_whose_last_value_repeats(self):
        """§3.3: retry_delay_seconds=[1,2,4] → 1, 2, 4, 4, 4…"""
        compute = sym("gyrfalcon.flow.engine:compute_retry_delay")
        schedule = [1, 2, 4]
        assert [compute(schedule, n) for n in range(6)] == [1, 2, 4, 4, 4, 4]

    def test_scalar_delay_applies_to_every_attempt(self):
        compute = sym("gyrfalcon.flow.engine:compute_retry_delay")
        assert [compute(5, n) for n in range(3)] == [5, 5, 5]

    def test_delayed_retry_is_a_scheduled_state_not_a_sleep(self, clock):
        """§3.3: survives a process restart; renders as 'will retry at 14:32'."""
        handle_retry = sym("gyrfalcon.flow.engine:build_retry_state")
        state = handle_retry(delay_seconds=90, now=clock.now())

        assert state.type.name == "SCHEDULED"
        assert state.name == "AwaitingRetry"
        assert state.state_details.scheduled_time == clock.now() + 90

    def test_zero_delay_retries_immediately_as_running(self):
        handle_retry = sym("gyrfalcon.flow.engine:build_retry_state")
        state = handle_retry(delay_seconds=None, now=0)
        assert state.type.name == "RUNNING"
        assert state.name == "Retrying"

    def test_retry_condition_fn_can_veto(self, flaky):
        """§3.3: 'don't retry a 400, do retry a 429.'"""
        activity = sym("gyrfalcon.flow:activity")

        class NotRetryable(Exception):
            ...

        fn = flaky(fail_times=99, exc=NotRetryable("400"))

        @activity(retries=5, retry_condition_fn=lambda t, tr, st: not isinstance(st.exception, NotRetryable))
        def t():
            return fn()

        state = t(return_type="state")
        assert state.type.name == "FAILED"
        assert fn.attempts == 1, "veto must prevent any retry"


# ── §3.4 timeouts and cancellation ────────────────────────────────────────────

class TestTimeouts:
    def test_timeout_produces_timedout_not_a_generic_failure(self):
        flow = sym("gyrfalcon.flow:flow")

        @flow(timeout_seconds=0.05)
        def slow():
            import time
            time.sleep(5)

        state = slow(return_type="state")
        assert state.name == "TimedOut"
        assert state.type.name == "FAILED"

    def test_engine_timeout_is_distinguishable_from_user_timeout_error(self):
        """§3.4: subclass internally so 'we killed it' != 'it raised the same class'."""
        flow = sym("gyrfalcon.flow:flow")
        FlowTimeout = sym("gyrfalcon.flow.exceptions:FlowRunTimeoutError")

        assert issubclass(FlowTimeout, Exception)
        assert not isinstance(TimeoutError(), FlowTimeout)

        @flow(timeout_seconds=10)
        def raises_user_timeout():
            raise TimeoutError("upstream socket timeout")

        state = raises_user_timeout(return_type="state")
        assert state.name == "Failed", "a user TimeoutError must not be reported as TimedOut"


# ── §3.5 hooks ────────────────────────────────────────────────────────────────

class TestHooks:
    def test_hooks_fire_for_their_state(self, recorder):
        flow = sym("gyrfalcon.flow:flow")

        @flow(
            on_running=[lambda f, r, s: recorder.record("running")],
            on_completion=[lambda f, r, s: recorder.record("completion")],
        )
        def f():
            return 1

        f()
        assert recorder.labels == ["running", "completion"]

    def test_failure_hook_fires_on_failure_only(self, recorder):
        flow = sym("gyrfalcon.flow:flow")

        @flow(
            on_completion=[lambda f, r, s: recorder.record("completion")],
            on_failure=[lambda f, r, s: recorder.record("failure")],
        )
        def f():
            raise RuntimeError("boom")

        f(return_type="state")
        assert "failure" in recorder.labels
        assert "completion" not in recorder.labels

    def test_raising_hook_is_logged_and_swallowed(self, recorder):
        """§3.5: a raising hook must not corrupt the run's state."""
        flow = sym("gyrfalcon.flow:flow")

        def bad_hook(f, r, s):
            recorder.record("bad")
            raise ValueError("hook exploded")

        @flow(on_completion=[bad_hook])
        def f():
            return 42

        state = f(return_type="state")
        assert state.is_completed()
        assert state.result() == 42
        assert "bad" in recorder.labels
