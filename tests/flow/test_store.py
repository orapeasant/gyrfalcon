"""Phase 4 — durable runs and the external control they enable.

Spec: §13.5 ("Phase 4 is where it becomes a *platform*"), §2.3 (two-phase
cancellation), §11 (filter-by-POST, pre-bucketed history).

Until runs are durable there is nothing for an operator to cancel and no history
to query, so this is the phase that turns a library into a control plane.
"""

from __future__ import annotations

import threading
import time

import pytest
from _spec import requires, sym

pytestmark = requires("gyrfalcon.flow.store:RunStore", section="§13.5 phase 4")


@pytest.fixture()
def store(make_store):
    """Runs once per configured backend (§15.9) — see conftest.store_target."""
    set_store = sym("gyrfalcon.flow.store:set_store")
    s = make_store()
    set_store(s)
    yield s
    set_store(None)
    s.close()


@pytest.fixture()
def persisted(store):
    """Turn on persistence for the duration of a test."""
    engine = sym("gyrfalcon.flow.engine:_BaseRunEngine")
    engine.persist = True
    yield store
    engine.persist = False


class TestRunRecords:
    def test_a_run_is_persisted_with_its_terminal_state(self, persisted):
        flow = sym("gyrfalcon.flow:flow")

        @flow
        def f(x):
            return x * 2

        state = f(21, return_type="state")
        row = persisted.get_run(state.id)
        assert row["name"] == "f"
        assert row["kind"] == "flow"
        assert row["state_type"] == "COMPLETED"
        assert row["is_final"] is True
        assert row["finished_at"] is not None

    def test_a_failure_persists_the_error(self, persisted):
        flow = sym("gyrfalcon.flow:flow")

        @flow
        def bad():
            raise ValueError("recorded")

        state = bad(return_type="state")
        row = persisted.get_run(state.id)
        assert row["state_type"] == "FAILED"
        assert "recorded" in str(row["error"])

    def test_transitions_are_appended_in_order(self, persisted):
        flow = sym("gyrfalcon.flow:flow")

        @flow(retries=1)
        def flaky():
            raise RuntimeError("always")

        state = flaky(return_type="state")
        history = persisted.get_history(state.id)
        assert [h["seq"] for h in history] == list(range(1, len(history) + 1))
        names = [h["state_name"] for h in history]
        assert names[0] == "Running"
        assert "Retrying" in names, "the retry must be visible in history"
        assert names[-1] == "Failed"

    def test_tasks_are_linked_to_their_flow_run(self, persisted):
        flow = sym("gyrfalcon.flow:flow")
        activity = sym("gyrfalcon.flow:activity")

        @activity
        def child(i):
            return i

        @flow
        def parent():
            return [child(i) for i in range(3)]

        state = parent(return_type="state")
        children, total = persisted.list_runs(flow_run_id=state.id)
        assert total == 3
        assert {c["kind"] for c in children} == {"task"}


class TestQuerying:
    def test_filter_by_state_and_name(self, persisted):
        flow = sym("gyrfalcon.flow:flow")

        @flow
        def good():
            return 1

        @flow
        def bad():
            raise RuntimeError("x")

        good()
        bad(return_type="state")

        failed, _ = persisted.list_runs(state_types=["FAILED"])
        assert [r["name"] for r in failed] == ["bad"]

        by_name, _ = persisted.list_runs(name="goo")
        assert [r["name"] for r in by_name] == ["good"]

    def test_pagination_reports_the_unpaged_total(self, persisted):
        flow = sym("gyrfalcon.flow:flow")

        @flow
        def f():
            return 1

        for _ in range(5):
            f()

        page, total = persisted.list_runs(limit=2, offset=0)
        assert len(page) == 2
        assert total == 5

    def test_counts_by_state(self, persisted):
        flow = sym("gyrfalcon.flow:flow")

        @flow
        def ok():
            return 1

        ok()
        ok()
        assert persisted.counts_by_state().get("COMPLETED") == 2

    def test_history_is_pre_bucketed(self, persisted):
        """§11: the UI must never aggregate."""
        flow = sym("gyrfalcon.flow:flow")

        @flow
        def f():
            return 1

        f()
        buckets = persisted.history_buckets(hours=1, buckets=4)
        assert len(buckets) == 4
        assert sum(sum(b["counts"].values()) for b in buckets) == 1

    def test_graph_returns_nodes_and_edges(self, persisted):
        flow = sym("gyrfalcon.flow:flow")
        activity = sym("gyrfalcon.flow:activity")

        @activity
        def child():
            return 1

        @flow
        def parent():
            return child()

        state = parent(return_type="state")
        graph = persisted.get_graph(state.id)
        assert len(graph["nodes"]) == 2
        assert {n["kind"] for n in graph["nodes"]} == {"flow", "task"}


class TestExternalCancellation:
    def test_cancel_moves_a_live_run_to_cancelling_not_cancelled(self, persisted):
        """§2.3: cancellation is two-phase — only the run confirms it stopped."""
        persisted.create_run("r1", "long", "flow")
        states = sym("gyrfalcon.flow.states")
        persisted.record_transition("r1", states.Running())

        row = persisted.request_cancel("r1")
        assert row["state_type"] == "CANCELLING"
        assert row["is_final"] is False

    def test_cancelling_a_terminal_run_is_a_no_op(self, persisted):
        states = sym("gyrfalcon.flow.states")
        persisted.create_run("r2", "done", "flow")
        persisted.record_transition("r2", states.Completed())

        row = persisted.request_cancel("r2")
        assert row["state_type"] == "COMPLETED", "a finished run must not be reopened"

    def test_cancelling_an_unknown_run_returns_none(self, persisted):
        assert persisted.request_cancel("nope") is None

    def test_a_running_flow_observes_an_operator_cancel(self, persisted):
        """The whole point of phase 4: control from outside the process."""
        flow = sym("gyrfalcon.flow:flow")
        seen: dict[str, str] = {}
        release = threading.Event()

        @flow
        def long_running():
            seen["run_id"] = _current_run_id()
            release.wait(timeout=5)
            raise RuntimeError("should retry, but cancel wins")

        result: dict[str, object] = {}

        def run():
            result["state"] = long_running(return_type="state")

        worker = threading.Thread(target=run, daemon=True)
        worker.start()

        for _ in range(200):
            if "run_id" in seen:
                break
            time.sleep(0.01)

        persisted.request_cancel(seen["run_id"])
        release.set()
        worker.join(timeout=5)

        assert result["state"].name == "Cancelled"
        assert persisted.get_run(seen["run_id"])["state_type"] == "CANCELLED"


def _current_run_id() -> str:
    from gyrfalcon.flow.context import get_flow_run_context

    ctx = get_flow_run_context()
    return ctx.run_id or ""


class TestSubmittedTaskParentLinkage:
    """Regression: ThreadPoolExecutor does not propagate contextvars, so a task
    submitted from inside a flow lost get_flow_run_context() on the worker
    thread — silently dropping persistence and parent linkage.
    """

    def test_submitted_task_is_linked_to_its_parent_flow(self, persisted):
        flow = sym("gyrfalcon.flow:flow")
        activity = sym("gyrfalcon.flow:activity")

        @activity
        def child(i):
            return i * 10

        @flow
        def parent():
            futs = [child.submit(i) for i in range(3)]
            return [f.result() for f in futs]

        state = parent(return_type="state")
        graph = persisted.get_graph(state.id)

        assert len(graph["nodes"]) == 4
        tasks = [n for n in graph["nodes"] if n["kind"] == "task"]
        assert len(tasks) == 3
        assert all(t["flow_run_id"] == state.id for t in tasks)

    def test_mapped_tasks_are_linked_to_their_parent_flow(self, persisted):
        flow = sym("gyrfalcon.flow:flow")
        activity = sym("gyrfalcon.flow:activity")

        @activity
        def double(x):
            return x * 2

        @flow
        def parent():
            futs = double.map([1, 2, 3])
            return [f.result() for f in futs]

        state = parent(return_type="state")
        children, total = persisted.list_runs(flow_run_id=state.id)
        assert total == 3


class TestCrashRecovery:
    """Spec §9.2 CancelFinalizer principle, extended to process restarts:
    'never claim a clean outcome you did not verify'.
    """

    def test_running_row_from_a_dead_process_is_marked_crashed_on_reopen(self, make_store):
        states = sym("gyrfalcon.flow.states")

        first = make_store()
        first.create_run("r1", "orphaned", "flow")
        first.record_transition("r1", states.Running())
        first.close()  # simulates the process dying without settling the run

        second = make_store()
        row = second.get_run("r1")
        assert row["state_type"] == "CRASHED"
        assert row["is_final"] is True
        second.close()

    def test_scheduled_and_cancelling_rows_are_also_reconciled(self, make_store):
        states = sym("gyrfalcon.flow.states")

        first = make_store()
        first.create_run("r-sched", "a", "flow")
        first.record_transition("r-sched", states.Scheduled())
        first.create_run("r-cancel", "b", "flow")
        first.record_transition("r-cancel", states.Running())
        first.record_transition("r-cancel", states.Cancelling())
        first.close()

        second = make_store()
        assert second.get_run("r-sched")["state_type"] == "CRASHED"
        assert second.get_run("r-cancel")["state_type"] == "CRASHED"
        second.close()

    def test_terminal_rows_are_left_untouched(self, make_store):
        states = sym("gyrfalcon.flow.states")

        first = make_store()
        first.create_run("r-done", "done", "flow")
        first.record_transition("r-done", states.Completed(data=42))
        first.close()

        second = make_store()
        row = second.get_run("r-done")
        assert row["state_type"] == "COMPLETED"
        assert row["result"] == 42
        second.close()

    def test_reconcile_can_be_disabled(self, make_store):
        """A test harness reopening its own store mid-test must not self-crash
        its still-legitimately-running rows."""
        states = sym("gyrfalcon.flow.states")

        first = make_store()
        first.create_run("r1", "still-going", "flow")
        first.record_transition("r1", states.Running())

        second = make_store(reconcile=False)
        assert second.get_run("r1")["state_type"] == "RUNNING"
        first.close()
        second.close()

    def test_reconciliation_is_visible_in_history(self, make_store):
        states = sym("gyrfalcon.flow.states")

        first = make_store()
        first.create_run("r1", "orphaned", "flow")
        first.record_transition("r1", states.Running())
        first.close()

        second = make_store()
        history = second.get_history("r1")
        assert history[-1]["state_name"] == "Crashed"
        second.close()
