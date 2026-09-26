"""Database-backed graph definitions and the first graph interpreter.

The initial executable graph is a directed path of registered Python
activities. Conditional edges select one next node; parallel joins and human
steps require their own durable node orchestration and are rejected here.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from gyrfalcon.db import open_database, resolve_target, sql
from gyrfalcon.db.migrations import ensure_schema
from gyrfalcon.db.scope import Scope, current_scope
from gyrfalcon.flow.engine import FlowRunEngine, TaskRunEngine
from gyrfalcon.flow.registry import get_activity
from gyrfalcon.flow.store import RunStore
from gyrfalcon.flow.templates import Flow
from gyrfalcon.identity import require_principal


def _path(value: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"Unknown graph value {path!r}")
        value = value[part]
    return value


def _resolve(value: Any, inputs: dict, outputs: dict) -> Any:
    if not isinstance(value, dict) or set(value) != {"ref"}:
        return value
    source, _, path = value["ref"].partition(".")
    if source == "inputs":
        return _path(inputs, path) if path else inputs
    if source not in outputs:
        raise ValueError(f"Graph reference {value['ref']!r} is not available")
    return _path(outputs[source], path) if path else outputs[source]


def _matches(rule: dict, output: Any) -> bool:
    if not isinstance(rule, dict) or set(rule) != {"path", "op", "value"}:
        raise ValueError("Edge condition needs path, op, and value")
    actual = _path(output, rule["path"]) if rule["path"] else output
    expected = rule["value"]
    operators = {
        "eq": lambda a, b: a == b,
        "ne": lambda a, b: a != b,
        "lt": lambda a, b: a < b,
        "lte": lambda a, b: a <= b,
        "gt": lambda a, b: a > b,
        "gte": lambda a, b: a >= b,
    }
    if rule["op"] not in operators:
        raise ValueError(f"Unsupported edge operator {rule['op']!r}")
    return bool(operators[rule["op"]](actual, expected))


def validate_graph(graph: dict) -> None:
    """Reject graphs the current interpreter cannot execute safely."""
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) or not graph["nodes"]:
        raise ValueError("Graph needs at least one node")
    if not isinstance(graph.get("edges"), list):
        raise ValueError("Graph needs an edges list")
    nodes = graph["nodes"]
    ids = [node.get("id") for node in nodes if isinstance(node, dict)]
    if len(ids) != len(nodes) or any(not isinstance(node_id, str) or not node_id for node_id in ids):
        raise ValueError("Every node needs a nonempty id")
    if len(set(ids)) != len(ids) or "inputs" in ids:
        raise ValueError("Node ids must be unique and cannot be 'inputs'")
    for node in nodes:
        if node.get("type") != "python":
            raise ValueError(f"Unsupported node type {node.get('type')!r}")
        if not isinstance(node.get("activity"), str) or not isinstance(node.get("version"), str):
            raise ValueError(f"Node {node['id']!r} needs an activity name and version")
        if not isinstance(node.get("inputs", {}), dict):
            raise ValueError(f"Node {node['id']!r} inputs must be an object")
        if get_activity(node["activity"], node["version"]) is None:
            raise ValueError(f"Activity {node['activity']!r} version {node['version']!r} is unavailable")
    outgoing: dict[str, list[dict]] = {node_id: [] for node_id in ids}
    incoming = {node_id: 0 for node_id in ids}
    for edge in graph["edges"]:
        if not isinstance(edge, dict) or edge.get("from") not in outgoing or edge.get("to") not in outgoing:
            raise ValueError("Edge must connect existing nodes")
        outgoing[edge["from"]].append(edge)
        incoming[edge["to"]] += 1
        if "when" in edge:
            _validate_rule(edge["when"])
    if sum(count == 0 for count in incoming.values()) != 1:
        raise ValueError("Graph needs exactly one entry node")
    if any(count > 1 for count in incoming.values()):
        raise ValueError("Joins are not supported by this interpreter yet")
    for edges in outgoing.values():
        if len(edges) > 1 and any("when" not in edge for edge in edges):
            raise ValueError("A decision with multiple edges needs a condition on each edge")
    entry = next(node_id for node_id, count in incoming.items() if count == 0)
    visited: set[str] = set()

    def walk(node_id: str) -> None:
        if node_id in visited:
            raise ValueError("Graph contains a cycle")
        visited.add(node_id)
        for edge in outgoing[node_id]:
            walk(edge["to"])

    walk(entry)
    if len(visited) != len(nodes):
        raise ValueError("Graph contains unreachable nodes")


def _validate_rule(rule: Any) -> None:
    if not isinstance(rule, dict) or set(rule) != {"path", "op", "value"}:
        raise ValueError("Edge condition needs path, op, and value")
    if not isinstance(rule["path"], str) or rule["op"] not in {"eq", "ne", "lt", "lte", "gt", "gte"}:
        raise ValueError("Invalid edge condition")


class GraphStore:
    def __init__(self, db_path: Path | str | None = None, backend: str | None = None,
                 dsn: str | None = None):
        self.backend, resolved, self.dsn = resolve_target(db_path, backend, dsn)
        self.db_path = resolved
        self._db = open_database(path=resolved, backend=self.backend, dsn=self.dsn)
        ensure_schema(self._db)

    def create(self, name: str, draft: dict) -> str:
        if not name.strip() or not isinstance(draft, dict):
            raise ValueError("A definition needs a name and graph object")
        who = require_principal()
        definition_id = str(uuid.uuid4())
        now = time.time()
        with self._db.connect() as conn:
            conn.execute(sql.INSERT_GRAPH_DEFINITION,
                         (definition_id, who.tenant_id, who.user_id, name, json.dumps(draft), now, now))
        return definition_id

    def get(self, definition_id: str, scope: Scope | None = None) -> dict | None:
        statement, params = sql.graph_definition(current_scope(scope))
        with self._db.connect() as conn:
            row = conn.fetchone(statement, (*params, definition_id))
        return {**row, "draft": json.loads(row["draft"])} if row else None

    def list(self, scope: Scope | None = None) -> list[dict]:
        statement, params = sql.graph_definitions(current_scope(scope))
        with self._db.connect() as conn:
            rows = conn.fetchall(statement, params)
        return [{**row, "draft": json.loads(row["draft"])} for row in rows]

    def save_draft(self, definition_id: str, draft: dict) -> None:
        if not isinstance(draft, dict):
            raise ValueError("Draft must be a graph object")
        row = self.get(definition_id)
        if row is None:
            raise KeyError(definition_id)
        with self._db.connect() as conn:
            conn.execute(sql.UPDATE_GRAPH_DRAFT,
                         (json.dumps(draft), time.time(), row["tenant_id"], definition_id))

    def delete(self, definition_id: str) -> None:
        row = self.get(definition_id)
        if row is None:
            raise KeyError(definition_id)
        with self._db.connect() as conn:
            conn.execute(sql.DELETE_GRAPH_VERSIONS, (row["tenant_id"], definition_id))
            conn.execute(sql.DELETE_GRAPH_DEFINITION, (row["tenant_id"], definition_id))

    def publish(self, definition_id: str) -> int:
        row = self.get(definition_id)
        if row is None:
            raise KeyError(definition_id)
        validate_graph(row["draft"])
        scope = current_scope().tenant_wide()
        statement, params = sql.latest_graph_version(scope)
        with self._db.connect() as conn:
            latest = conn.fetchone(statement, (*params, definition_id))
            version = int(latest["version"] or 0) + 1
            conn.execute(sql.INSERT_GRAPH_VERSION,
                         (row["tenant_id"], definition_id, version,
                          json.dumps(row["draft"], sort_keys=True), time.time()))
        return version

    def version(self, definition_id: str, version: int) -> dict:
        statement, params = sql.graph_version(current_scope())
        with self._db.connect() as conn:
            row = conn.fetchone(statement, (*params, definition_id, version))
        if row is None:
            raise KeyError(f"No published graph {definition_id} version {version}")
        return json.loads(row["graph"])

    def latest_version(self, definition_id: str) -> int | None:
        if self.get(definition_id) is None:
            raise KeyError(definition_id)
        statement, params = sql.latest_graph_version(current_scope())
        with self._db.connect() as conn:
            row = conn.fetchone(statement, (*params, definition_id))
        return int(row["version"]) if row and row["version"] is not None else None

    def close(self) -> None:
        self._db.close()

    def run(self, definition_id: str, version: int, inputs: dict) -> dict:
        """Run a published version and return its run id and final output."""
        graph = self.version(definition_id, version)
        validate_graph(graph)
        if not isinstance(inputs, dict):
            raise ValueError("Graph inputs must be an object")
        run_store = RunStore(db_path=self.db_path, backend=self.backend, dsn=self.dsn,
                             reconcile=False, emit_events=False)
        run_id = str(uuid.uuid4())
        run_store.create_run(run_id, "graph.run", "flow", inputs)
        who = require_principal()
        with self._db.connect() as conn:
            conn.execute(sql.BIND_RUN_GRAPH, (definition_id, version, run_id, who.tenant_id))

        def execute() -> Any:
            nodes = {node["id"]: node for node in graph["nodes"]}
            outgoing: dict[str, list[dict]] = {node_id: [] for node_id in nodes}
            incoming = set()
            for edge in graph["edges"]:
                outgoing[edge["from"]].append(edge)
                incoming.add(edge["to"])
            current = next(node_id for node_id in nodes if node_id not in incoming)
            outputs: dict[str, Any] = {}
            while True:
                node = nodes[current]
                activity = get_activity(node["activity"], node["version"])
                if activity is None:
                    raise ValueError(f"Activity {node['activity']!r} version {node['version']!r} is unavailable")
                parameters = {key: _resolve(value, inputs, outputs)
                              for key, value in node.get("inputs", {}).items()}
                state = TaskRunEngine(activity, parameters, store=run_store).run()
                outputs[current] = state.result()
                choices = [edge for edge in outgoing[current]
                           if "when" not in edge or _matches(edge["when"], outputs[current])]
                if len(choices) > 1:
                    raise ValueError(f"Node {current!r} selected multiple outgoing edges")
                if not choices:
                    return outputs[current]
                current = choices[0]["to"]

        template = Flow(execute, name="graph.run")
        try:
            state = FlowRunEngine(template, {}, run_id=run_id, store=run_store).run()
            return {"run_id": run_id, "result": state.result()}
        finally:
            run_store.close()
