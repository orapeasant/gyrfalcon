"""A GUI-authored graph must publish and run as a pinned database version."""

import pytest

from gyrfalcon.flow import sample_activities  # noqa: F401 - registers catalog entries
from gyrfalcon.flow.graphs import GraphStore
from gyrfalcon.flow.sample_activities import sample_graph
from gyrfalcon.flow.store import RunStore
from gyrfalcon.identity import Principal, use_principal


def test_sample_graph_publishes_runs_and_pins_version(store_target):
    graph_store = GraphStore(**store_target)
    definition_id = graph_store.create("hello", sample_graph())
    assert graph_store.publish(definition_id) == 1

    changed = sample_graph()
    changed["nodes"] = changed["nodes"][:1]
    changed["edges"] = []
    graph_store.save_draft(definition_id, changed)
    assert graph_store.publish(definition_id) == 2

    old = graph_store.run(definition_id, 1, {"name": "Ada"})
    new = graph_store.run(definition_id, 2, {"name": "Ada"})
    assert old["result"] == {"message": "hello1 Ada → hello2 → hello3 → hello4 → hello5"}
    assert new["result"] == {"message": "hello1 Ada"}

    run_store = RunStore(**store_target, reconcile=False, emit_events=False)
    assert run_store.get_run(old["run_id"])["definition_version"] == 1
    assert run_store.get_run(new["run_id"])["definition_version"] == 2
    run_store.close()
    graph_store.close()


def test_graphs_are_tenant_scoped_and_invalid_graphs_do_not_publish(store_target):
    graph_store = GraphStore(**store_target)
    alice = Principal(user_id="alice", tenant_id="one")
    bob = Principal(user_id="bob", tenant_id="two")
    with use_principal(alice):
        definition_id = graph_store.create("hello", sample_graph())
        graph_store.save_draft(definition_id, {"nodes": [], "edges": []})
        with pytest.raises(ValueError, match="at least one node"):
            graph_store.publish(definition_id)
        graph_store.save_draft(definition_id, sample_graph())
        graph_store.publish(definition_id)
    with use_principal(bob):
        assert graph_store.list() == []
        with pytest.raises(KeyError):
            graph_store.version(definition_id, 1)
    graph_store.close()


def test_delete_removes_definition_and_versions_but_keeps_run_history(store_target):
    graph_store = GraphStore(**store_target)
    definition_id = graph_store.create("hello", sample_graph())
    graph_store.publish(definition_id)
    result = graph_store.run(definition_id, 1, {"name": "Ada"})

    graph_store.delete(definition_id)
    assert graph_store.get(definition_id) is None
    assert graph_store.list() == []
    with pytest.raises(KeyError):
        graph_store.version(definition_id, 1)
    with pytest.raises(KeyError):
        graph_store.delete(definition_id)

    run_store = RunStore(**store_target, reconcile=False, emit_events=False)
    assert run_store.get_run(result["run_id"])["definition_id"] == definition_id
    run_store.close()
    graph_store.close()
