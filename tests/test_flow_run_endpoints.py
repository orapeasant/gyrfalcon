"""Flow Instances actions over HTTP — retry, delete, bulk delete (§14.12).

Delete refuses a live run rather than racing the engine still writing to it,
and retry re-submits through `run_definition_now` rather than reopening a
terminal run. Both of those are decisions a plausible "simplification" would
undo, so they are pinned here at the layer an operator actually hits them.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi.testclient", reason="needs fastapi's TestClient")


@pytest.fixture(scope="module")
def app_client(tmp_path_factory):
    import os

    from gyrfalcon import gyrfalcon_constants as gc

    home = tmp_path_factory.mktemp("gyrfalcon_home")
    os.environ["GYRFALCON_HOME"] = str(home)
    gc.get_gyrfalcon_home.cache_clear()

    from fastapi.testclient import TestClient

    from gyrfalcon_cli import web_server

    with TestClient(web_server.app) as c:
        yield c, web_server

    gc.get_gyrfalcon_home.cache_clear()


@pytest.fixture()
def headers(app_client):
    _, web_server = app_client
    return {"X-Gyrfalcon-Session-Token": web_server._session_token}


@pytest.fixture()
def store(app_client, tmp_path):
    """A per-test SQLite store injected as the process-wide flow store, so the
    endpoints under test read and write this file and nothing else."""
    from gyrfalcon.flow.store import RunStore, set_store

    s = RunStore(db_path=tmp_path / "flow.db")
    set_store(s)
    yield s
    set_store(None)
    s.close()


def _terminal(store, run_id: str, name: str = "sample") -> None:
    from gyrfalcon.flow import states

    store.create_run(run_id, name, "flow")
    store.record_transition(run_id, states.Completed())


def _running(store, run_id: str, name: str = "sample") -> None:
    from gyrfalcon.flow import states

    store.create_run(run_id, name, "flow")
    store.record_transition(run_id, states.Running())


class TestDeleteEndpoint:
    def test_deleting_a_finished_run_succeeds(self, app_client, headers, store):
        c, _ = app_client
        _terminal(store, "done")

        r = c.delete("/api/flow/runs/done", headers=headers)

        assert r.status_code == 200
        assert store.get_run("done") is None

    def test_deleting_an_unknown_run_is_404(self, app_client, headers, store):
        c, _ = app_client
        assert c.delete("/api/flow/runs/nope", headers=headers).status_code == 404

    def test_deleting_a_live_run_is_400_not_404(self, app_client, headers, store):
        """A refusal and an absence are different answers, and the UI shows
        different messages for them."""
        c, _ = app_client
        _running(store, "live")

        r = c.delete("/api/flow/runs/live", headers=headers)

        assert r.status_code == 400
        assert "cancel" in r.json()["detail"].lower(), "the error must say what to do instead"
        assert store.get_run("live") is not None

    def test_delete_requires_a_credential(self, app_client, store):
        c, _ = app_client
        _terminal(store, "done")

        assert c.delete("/api/flow/runs/done").status_code == 401
        assert store.get_run("done") is not None


class TestBulkDeleteEndpoint:
    def test_the_response_partitions_the_outcomes(self, app_client, headers, store):
        """One click over a mixed selection has three possible per-row results;
        collapsing them into a single ok/failed would leave the user unable to
        tell "still running" from "already gone"."""
        c, _ = app_client
        _terminal(store, "gone1")
        _terminal(store, "gone2")
        _running(store, "busy")

        r = c.post(
            "/api/flow/runs/delete",
            headers=headers,
            json={"run_ids": ["gone1", "gone2", "busy", "never-existed"]},
        )

        body = r.json()
        assert r.status_code == 200
        assert sorted(body["deleted"]) == ["gone1", "gone2"]
        assert body["refused"] == ["busy"]
        assert body["missing"] == ["never-existed"]

    def test_a_refusal_does_not_block_the_rest_of_the_batch(self, app_client, headers, store):
        c, _ = app_client
        _running(store, "busy")
        _terminal(store, "deletable")

        c.post("/api/flow/runs/delete", headers=headers,
               json={"run_ids": ["busy", "deletable"]})

        assert store.get_run("busy") is not None
        assert store.get_run("deletable") is None

    def test_an_empty_selection_is_a_no_op(self, app_client, headers, store):
        c, _ = app_client
        r = c.post("/api/flow/runs/delete", headers=headers, json={"run_ids": []})

        assert r.status_code == 200
        assert r.json() == {"deleted": [], "refused": [], "missing": []}


class TestRetryEndpoint:
    def test_retrying_an_unknown_run_is_404(self, app_client, headers, store):
        c, _ = app_client
        assert c.post("/api/flow/runs/nope/retry", headers=headers).status_code == 404

    def test_retrying_a_live_run_is_refused(self, app_client, headers, store):
        """Retry starts a *new* run; doing that while the original is still
        going would quietly create a second execution under a name that
        implies it replaced the first."""
        c, _ = app_client
        _running(store, "live")

        assert c.post("/api/flow/runs/live/retry", headers=headers).status_code == 400

    def test_retrying_a_task_run_is_refused(self, app_client, headers, store):
        """Only flow runs are re-submittable — a task has no definition of its
        own to re-run, it belongs to its parent flow."""
        from gyrfalcon.flow import states

        c, _ = app_client
        store.create_run("t1", "some_task", "task")
        store.record_transition("t1", states.Completed())

        r = c.post("/api/flow/runs/t1/retry", headers=headers)
        assert r.status_code == 400

    def test_retrying_a_run_whose_definition_is_gone_is_404(self, app_client, headers, store):
        c, _ = app_client
        _terminal(store, "orphan", name="deleted_flow_definition")

        r = c.post("/api/flow/runs/orphan/retry", headers=headers)

        assert r.status_code == 404
        assert "definition" in r.json()["detail"].lower()

    def test_retry_creates_a_new_run_and_leaves_the_original(self, app_client, headers, store):
        c, _ = app_client
        from gyrfalcon.flow import flow, registry

        @flow(name="retryable")
        def retryable():
            return "ok"

        registry.register(retryable)
        try:
            _terminal(store, "first", name="retryable")

            r = c.post("/api/flow/runs/first/retry", headers=headers)

            assert r.status_code == 200
            new_id = r.json()["run_id"]
            assert new_id != "first", "retry re-submits; it never reopens the original"
            assert store.get_run("first") is not None, "the original run is untouched"

            for _ in range(200):          # the new run executes on its own thread
                row = store.get_run(new_id)
                if row and row["is_final"]:
                    break
                time.sleep(0.01)
            assert store.get_run(new_id) is not None
        finally:
            registry._REGISTRY.pop("retryable", None)


class TestBestEffortParams:
    """Stored parameters are `repr()` text for display (`flow/engine.py:148`),
    not replayable data. Retry recovers what it can and passes the rest
    through rather than failing the whole attempt."""

    @pytest.fixture()
    def recover(self, app_client):
        _, web_server = app_client
        return web_server._best_effort_params

    @pytest.mark.parametrize("stored,expected", [
        ("'hello'", "hello"),
        ("42", 42),
        ("3.5", 3.5),
        ("True", True),
        ("None", None),
        ("[1, 2, 3]", [1, 2, 3]),
        ("{'a': 1}", {"a": 1}),
    ])
    def test_json_like_reprs_round_trip(self, recover, stored, expected):
        assert recover({"x": stored}) == {"x": expected}

    def test_a_non_literal_repr_is_passed_through_unchanged(self, recover):
        """`<MyObject at 0x7f…>` is not a literal. Passing the raw string on is
        worse than the real value but better than 500-ing the retry."""
        raw = "<gyrfalcon.Thing object at 0x7f0000000000>"

        assert recover({"x": raw}) == {"x": raw}

    def test_non_string_values_are_left_alone(self, recover):
        assert recover({"x": 7, "y": None}) == {"x": 7, "y": None}

    def test_empty_params_stay_empty(self, recover):
        assert recover({}) == {}
