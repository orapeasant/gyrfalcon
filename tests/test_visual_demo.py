from copy import deepcopy

import pytest

from gyrfalcon.flow import demo_activities  # noqa: F401
from gyrfalcon.flow.graph_context import ActivityContext, ActivityResult
from gyrfalcon.flow.registry import get_activity
from gyrfalcon.flow.templates import activity
from gyrfalcon.flow.visual_executor import VisualExecutor
from gyrfalcon.flow.visual_graph import validate_visual_graph


class MemoryRuntime:
    def __init__(self, run_id="demo-run", inputs=None):
        self.run = {"id": run_id, "state": "queued", "parameters": inputs or {},
                    "context": {}, "loop_state": {}, "current_node_path": None}
        self.visits = []
        self.attrs = {}
        self._seq = 0

    def get_run(self, run_id):
        return deepcopy(self.run) if run_id == self.run["id"] else None

    def update_run(self, run_id, **fields):
        assert run_id == self.run["id"]
        self.run.update(deepcopy(fields))
        return deepcopy(self.run)

    def create_node_visit(self, run_id, node_path, node_id, node_kind,
                          input_value=None, state="running", context_snapshot=None):
        self._seq += 1
        prior = [visit for visit in self.visits if visit["node_path"] == node_path]
        attempt = prior[-1]["attempt"] + (1 if prior[-1]["state"] in {"failed", "crashed"} else 0) if prior else 1
        visit = {"id": f"visit-{self._seq}", "run_id": run_id,
                 "node_path": node_path, "node_id": node_id, "node_kind": node_kind,
                 "state": state, "attempt": attempt, "input_value": deepcopy(input_value),
                 "context_snapshot": deepcopy(context_snapshot)}
        self.visits.append(visit)
        return visit

    def update_node_visit(self, visit_id, **fields):
        visit = next(item for item in self.visits if item["id"] == visit_id)
        visit.update(deepcopy(fields))
        return deepcopy(visit)

    def get_waiting_visit(self, run_id):
        return None

    def list_node_visits(self, run_id):
        return deepcopy(self.visits)

    def get_attrs(self, run_id, scope_path):
        return deepcopy(self.attrs.get(scope_path, {}))

    def put_attrs(self, run_id, scope_path, values):
        self.attrs.setdefault(scope_path, {}).update(deepcopy(values))


def _executor(graph, store):
    return VisualExecutor(store, store.run["id"], graph)


def test_complex_demo_has_ten_activities_and_executes_each_branch():
    graph = demo_activities.sample_graph()
    validate_visual_graph(graph, activity_available=lambda name, version: get_activity(name, version) is not None)
    activity_ids = {node["id"] for node in graph["nodes"] if node["type"] == "python"}
    assert len(activity_ids) == 10

    cases = [
        {"customer_tier": "gold", "risk_score": 0.9, "inventory_count": 0, "shipping_method": "parcel"},
        {"customer_tier": "standard", "risk_score": 0.1, "inventory_count": 4, "shipping_method": "pickup"},
        {"customer_tier": "gold", "risk_score": 0.1, "inventory_count": 2, "shipping_method": "hold"},
        {"customer_tier": "standard", "risk_score": 0.9, "inventory_count": 0, "shipping_method": "hold"},
    ]
    completed = set()
    for index, case in enumerate(cases):
        store = MemoryRuntime(f"demo-{index}", {**demo_activities.SAMPLE_INPUTS, **case})
        outcome = _executor(graph, store).run()
        assert outcome.state == "completed"
        completed.update(visit["node_id"] for visit in store.visits
                         if visit["node_kind"] == "python" and visit["state"] == "completed")
        assert all(visit.get("context_snapshot") is not None for visit in store.visits)
    assert completed == activity_ids


_retry_attempts = 0


@activity(name="test.retry_context", version="1")
def _retry_context_activity(ctx: ActivityContext) -> ActivityResult:
    global _retry_attempts
    _retry_attempts += 1
    observed = ctx.inputs["checkpoint_value"]
    ctx.inputs["checkpoint_value"] = "mutated by failed attempt"
    if _retry_attempts == 1:
        raise RuntimeError("simulated activity failure")
    return ActivityResult("done", {"observed": observed})


def test_failed_activity_can_retry_from_its_exact_saved_context():
    global _retry_attempts
    _retry_attempts = 0
    graph = {"version": 2, "attributes": [], "nodes": [
        {"id": "start", "type": "start", "transients": ["next"], "attributes": []},
        {"id": "work", "type": "python", "activity": "test.retry_context", "version": "1",
         "transients": ["done"], "attributes": []},
        {"id": "end", "type": "end", "transients": ["done"], "attributes": []},
    ], "edges": [
        {"id": "a", "from": "start", "to": "work", "transient": "next"},
        {"id": "b", "from": "work", "to": "end", "transient": "done"},
    ]}
    validate_visual_graph(graph, activity_available=lambda name, version: get_activity(name, version) is not None)
    store = MemoryRuntime("retry-demo", {"checkpoint_value": "original"})
    with pytest.raises(RuntimeError, match="simulated activity failure"):
        _executor(graph, store).run()
    failed = next(visit for visit in store.visits if visit["node_id"] == "work")
    checkpoint = failed["context_snapshot"]
    assert store.run["context"]["inputs"]["checkpoint_value"] == "mutated by failed attempt"

    # This is the state restoration performed by FlowRuntimeStore.retry_run.
    store.run["context"] = deepcopy(checkpoint["run_context"])
    store.run["loop_state"] = deepcopy(checkpoint["loop_state"])
    store.run["context"]["_retry_checkpoint"] = {
        "node_path": failed["node_path"],
        "activity_context": {key: deepcopy(checkpoint.get(key)) for key in
                             ("incoming_value", "inputs", "outputs", "flow_attributes", "attributes")},
    }
    store.run["state"] = "queued"
    outcome = _executor(graph, store).run()
    assert outcome.state == "completed"
    attempts = [visit for visit in store.visits if visit["node_id"] == "work"]
    assert [visit["attempt"] for visit in attempts] == [1, 2]
    assert attempts[1]["context_snapshot"]["inputs"]["checkpoint_value"] == "original"
