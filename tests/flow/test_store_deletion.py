"""Run deletion and date-range filtering — the Instances page's search and
bulk-delete actions (§14.12).

Deletion is the counterpart to §2.3's two-phase cancel: a run that is still
live belongs to an engine that is still writing transitions against its id, so
removing the row is refused rather than racing it. These tests pin that
refusal, the cascade to history/edges, and the `created_at` window the search
bar filters on.
"""

from __future__ import annotations

import pytest
from _spec import requires, sym

pytestmark = requires("gyrfalcon.flow.store:RunStore", section="§14.12")


@pytest.fixture()
def store(make_store):
    """Runs once per configured backend (§15.9) — see conftest.store_target."""
    set_store = sym("gyrfalcon.flow.store:set_store")
    s = make_store()
    set_store(s)
    yield s
    set_store(None)
    s.close()


def _terminal_run(store, run_id: str, name: str = "f") -> None:
    states = sym("gyrfalcon.flow.states")
    store.create_run(run_id, name, "flow")
    store.record_transition(run_id, states.Completed())


def _live_run(store, run_id: str, name: str = "f") -> None:
    states = sym("gyrfalcon.flow.states")
    store.create_run(run_id, name, "flow")
    store.record_transition(run_id, states.Running())


class TestDeleteRun:
    def test_deleting_a_finished_run_removes_it(self, store):
        _terminal_run(store, "r1")

        assert store.delete_run("r1") is True
        assert store.get_run("r1") is None

    def test_deleting_an_unknown_run_returns_none(self, store):
        """None and False mean different things: absent vs. refused."""
        assert store.delete_run("nope") is None

    def test_deleting_a_live_run_is_refused_not_raised(self, store):
        """The run's own engine is still writing transitions against this id —
        removing the row would surface as a confusing write-to-nothing rather
        than the clean cancellation the operator actually wanted (§2.3)."""
        _live_run(store, "r2")

        assert store.delete_run("r2") is False
        assert store.get_run("r2") is not None, "a refused delete must not partially apply"

    def test_cancelling_then_deleting_works(self, store):
        """The documented path out of the refusal above."""
        states = sym("gyrfalcon.flow.states")
        _live_run(store, "r3")
        assert store.delete_run("r3") is False

        store.record_transition("r3", states.Cancelled())
        assert store.delete_run("r3") is True
        assert store.get_run("r3") is None

    def test_deletion_takes_the_history_with_it(self, store):
        _terminal_run(store, "r4")
        assert store.get_history("r4"), "precondition: transitions were recorded"

        store.delete_run("r4")
        assert store.get_history("r4") == []

    def test_deletion_removes_edges_in_both_directions(self, store):
        """`DELETE_EDGES_FOR` matches downstream OR upstream — an edge kept on
        either side would outlive the run it describes."""
        _terminal_run(store, "parent")
        _terminal_run(store, "child")
        store.record_edge("child", "parent")
        _terminal_run(store, "grandchild")
        store.record_edge("grandchild", "child")

        store.delete_run("child")

        graph = store.get_graph("parent")
        edges = [(e["upstream"], e["downstream"]) for e in graph["edges"]]
        assert ("parent", "child") not in edges
        assert ("child", "grandchild") not in edges

    def test_deleting_one_run_leaves_the_others(self, store):
        _terminal_run(store, "keep1")
        _terminal_run(store, "drop")
        _terminal_run(store, "keep2")

        store.delete_run("drop")

        rows, total = store.list_runs()
        ids = {r["id"] for r in rows}
        assert ids == {"keep1", "keep2"}
        assert total == 2, "the count must drop with the row, not just the page"


class TestCreatedAtFiltering:
    """The Instances page's from/to date pickers (§14.12)."""

    def _run_at(self, store, run_id: str, created_at: float) -> None:
        _terminal_run(store, run_id)
        # create_run stamps "now"; rewrite it so the window has something
        # deterministic to select on.
        with store._db.connect() as conn:
            conn.execute(
                "UPDATE flow_runs SET created_at = ? WHERE id = ?", (created_at, run_id)
            )

    def test_created_from_excludes_older_runs(self, store):
        self._run_at(store, "old", 1_000.0)
        self._run_at(store, "new", 2_000.0)

        rows, total = store.list_runs(created_from=1_500.0)
        assert [r["id"] for r in rows] == ["new"]
        assert total == 1

    def test_created_to_excludes_newer_runs(self, store):
        self._run_at(store, "old", 1_000.0)
        self._run_at(store, "new", 2_000.0)

        rows, _ = store.list_runs(created_to=1_500.0)
        assert [r["id"] for r in rows] == ["old"]

    def test_both_bounds_select_the_window(self, store):
        self._run_at(store, "before", 1_000.0)
        self._run_at(store, "inside", 2_000.0)
        self._run_at(store, "after", 3_000.0)

        rows, _ = store.list_runs(created_from=1_500.0, created_to=2_500.0)
        assert [r["id"] for r in rows] == ["inside"]

    def test_bounds_are_inclusive(self, store):
        """A picked date covers its own boundary — an exclusive bound would
        silently drop runs created exactly at midnight."""
        self._run_at(store, "edge", 2_000.0)

        rows, _ = store.list_runs(created_from=2_000.0, created_to=2_000.0)
        assert [r["id"] for r in rows] == ["edge"]

    def test_date_filter_composes_with_the_name_search(self, store):
        """Both predicates come from one search bar and must AND together."""
        self._run_at(store, "a", 2_000.0)
        self._run_at(store, "b", 2_000.0)
        with store._db.connect() as conn:
            conn.execute("UPDATE flow_runs SET name = ? WHERE id = ?", ("ingest", "a"))
            conn.execute("UPDATE flow_runs SET name = ? WHERE id = ?", ("report", "b"))

        rows, _ = store.list_runs(name="ing", created_from=1_500.0, created_to=2_500.0)
        assert [r["id"] for r in rows] == ["a"]

    def test_no_bounds_returns_everything(self, store):
        self._run_at(store, "x", 1_000.0)
        self._run_at(store, "y", 9_000.0)

        _, total = store.list_runs()
        assert total == 2
