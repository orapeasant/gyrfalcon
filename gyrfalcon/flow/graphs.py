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


def _attribute_rows(graph: dict) -> list[tuple[str, str, str, str, str, str]]:
    """Flatten draft attribute definitions for fnd_flow_attrs.

    The graph remains the editable document; the table provides scoped,
    queryable attribute definitions and immutable published snapshots.
    """
    rows: list[tuple[str, str, str, str, str, str]] = []

    def add(attributes: Any, path: list[str]) -> None:
        if attributes is None:
            return
        if not isinstance(attributes, list):
            raise ValueError("Attributes must be a list")
        names: set[str] = set()
        ids: set[str] = set()
        for attribute in attributes:
            if not isinstance(attribute, dict):
                raise ValueError("Each attribute must be an object")
            attr_id = attribute.get("id")
            name = attribute.get("name")
            value_type = attribute.get("type")
            candidates = attribute.get("candidates", [])
            default = attribute.get("defaultValue")
            if not isinstance(attr_id, str) or not attr_id or not isinstance(name, str) or not name.strip():
                raise ValueError("Attributes need an id and name")
            if name in names:
                raise ValueError(f"Duplicate attribute {name!r}")
            names.add(name)
            if attr_id in ids:
                raise ValueError(f"Duplicate attribute id {attr_id!r}")
            ids.add(attr_id)
            if not isinstance(value_type, str) or value_type not in {"string", "number", "boolean", "json"} or not isinstance(candidates, list):
                raise ValueError(f"Attribute {name!r} has an invalid type or candidate list")
            valid_type = {
                "string": lambda value: isinstance(value, str),
                "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
                "boolean": lambda value: isinstance(value, bool),
                "json": lambda value: True,
            }[value_type]
            if not valid_type(default) or any(not valid_type(value) for value in candidates):
                raise ValueError(f"Attribute {name!r} has a value outside its declared type")
            if candidates and default not in candidates:
                raise ValueError(f"Attribute {name!r} default must be an allowed value")
            rows.append((json.dumps(path), attr_id, name, value_type,
                         json.dumps(default), json.dumps(candidates)))

    def walk(current: dict, path: list[str]) -> None:
        add(current.get("attributes"), path)
        for node in current.get("nodes", []):
            if not isinstance(node, dict) or not isinstance(node.get("id"), str):
                continue
            node_path = [*path, "node", node["id"]]
            add(node.get("attributes"), node_path)
            process = node.get("process")
            if isinstance(process, dict) and process.get("mode") == "inline" and isinstance(process.get("graph"), dict):
                walk(process["graph"], [*node_path, "flow"])

    walk(graph, ["flow"])
    return rows


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
        attributes = _attribute_rows(draft)
        with self._db.connect() as conn:
            conn.execute(sql.INSERT_GRAPH_DEFINITION,
                         (definition_id, who.tenant_id, who.user_id, name, json.dumps(draft), now, now))
            for scope_path, attr_id, attr_name, value_type, default, candidates in attributes:
                conn.execute(sql.INSERT_GRAPH_ATTR,
                             (who.tenant_id, definition_id, 0, scope_path, attr_id, attr_name,
                              value_type, default, candidates))
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
        attributes = _attribute_rows(draft)
        with self._db.connect() as conn:
            conn.execute(sql.UPDATE_GRAPH_DRAFT,
                         (json.dumps(draft), time.time(), row["tenant_id"], definition_id))
            conn.execute(sql.DELETE_GRAPH_ATTRS_VERSION, (row["tenant_id"], definition_id, 0))
            for scope_path, attr_id, attr_name, value_type, default, candidates in attributes:
                conn.execute(sql.INSERT_GRAPH_ATTR,
                             (row["tenant_id"], definition_id, 0, scope_path, attr_id,
                              attr_name, value_type, default, candidates))

    def delete(self, definition_id: str) -> None:
        row = self.get(definition_id)
        if row is None:
            raise KeyError(definition_id)
        with self._db.connect() as conn:
            conn.execute(sql.DELETE_GRAPH_ATTRS, (row["tenant_id"], definition_id))
            conn.execute(sql.DELETE_GRAPH_VERSIONS, (row["tenant_id"], definition_id))
            conn.execute(sql.DELETE_GRAPH_DEFINITION, (row["tenant_id"], definition_id))

    def publish(self, definition_id: str) -> int:
        row = self.get(definition_id)
        if row is None:
            raise KeyError(definition_id)
        validate_graph(row["draft"])
        attributes = _attribute_rows(row["draft"])
        scope = current_scope().tenant_wide()
        statement, params = sql.latest_graph_version(scope)
        with self._db.connect() as conn:
            latest = conn.fetchone(statement, (*params, definition_id))
            version = int(latest["version"] or 0) + 1
            conn.execute(sql.INSERT_GRAPH_VERSION,
                         (row["tenant_id"], definition_id, version,
                          json.dumps(row["draft"], sort_keys=True), time.time()))
            for scope_path, attr_id, attr_name, value_type, default, candidates in attributes:
                conn.execute(sql.INSERT_GRAPH_ATTR,
                             (row["tenant_id"], definition_id, version, scope_path, attr_id,
                              attr_name, value_type, default, candidates))
        return version

    def attributes(self, definition_id: str, version: int = 0) -> list[dict]:
        if self.get(definition_id) is None:
            raise KeyError(definition_id)
        statement, params = sql.graph_attrs(current_scope())
        with self._db.connect() as conn:
            rows = conn.fetchall(statement, (*params, definition_id, version))
        return [{**row, "default_value": json.loads(row["default_value"]),
                 "candidates": json.loads(row["candidates"])} for row in rows]

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


# The v2 designer's persistence is the visual-flow schema; the earlier
# Python-only GraphStore above is retained as a compatibility reference while
# callers transition. This public class shadows it and no longer writes legacy
# fnd_flow_definitions/fnd_flow_attrs rows.
_LegacyGraphStore = GraphStore


class GraphStore:
    """Tenant-scoped design store for version 2 visual flow graphs."""

    def __init__(self, db_path=None, backend=None, dsn=None):
        if db_path is not None or backend not in (None, "postgres") or dsn is not None:
            raise ValueError("Visual flows use the configured PostgreSQL SQLAlchemy database")
        from gyrfalcon.flow.runtime_store import FlowRuntimeStore

        who = require_principal()
        self._store = FlowRuntimeStore(tenant_id=who.tenant_id, migrate=True)
        self._user_id = who.user_id

    @staticmethod
    def _flatten_attributes(graph: dict) -> list[dict]:
        rows = []

        def add(items, path):
            for item in items or []:
                rows.append({
                    "scope_path": json.dumps(path),
                    "attribute_id": item["id"],
                    "name": item["name"],
                    "value_type": item["type"],
                    "default_value": item.get("defaultValue"),
                    "candidates": item.get("candidates", []),
                })

        def walk(current, path):
            add(current.get("attributes"), path)
            for node in current.get("nodes", []):
                node_path = [*path, "node", node["id"]]
                add(node.get("attributes"), node_path)
                process = node.get("process") or {}
                if process.get("mode") == "inline":
                    walk(process["graph"], [*node_path, "flow"])

        walk(graph, ["flow"])
        return rows

    def create(self, name: str, draft: dict) -> str:
        if not name.strip() or not isinstance(draft, dict):
            raise ValueError("A definition needs a name and graph object")
        row = self._store.create_definition(name.strip(), draft, self._user_id)
        self._store.replace_draft_attrs(row["id"], self._flatten_attributes(draft))
        return row["id"]

    def get(self, definition_id: str, scope=None) -> dict | None:
        row = self._store.get_definition(definition_id)
        if row is None:
            return None
        return {**row, "id": row["id"], "draft": row["draft"]}

    def list(self, scope=None) -> list[dict]:
        return self._store.list_definitions()

    def save_draft(self, definition_id: str, draft: dict) -> None:
        if not isinstance(draft, dict):
            raise ValueError("Draft must be a graph object")
        self._store.save_draft(definition_id, draft)
        self._store.replace_draft_attrs(definition_id, self._flatten_attributes(draft))

    def rename(self, definition_id: str, name: str) -> None:
        name = name.strip()
        if not name:
            raise ValueError("A definition needs a name")
        if any(row["id"] != definition_id and row["name"] == name for row in self._store.list_definitions()):
            raise ValueError(f"A flow named {name!r} already exists")
        try:
            self._store.rename_definition(definition_id, name)
        except KeyError as exc:
            raise KeyError(definition_id) from exc

    def set_enabled(self, definition_id: str, enabled: bool) -> None:
        try:
            self._store.set_definition_enabled(definition_id, bool(enabled))
        except KeyError as exc:
            raise KeyError(definition_id) from exc

    def delete(self, definition_id: str) -> None:
        if not self._store.delete_definition(definition_id):
            raise KeyError(definition_id)

    def _pin_graph(self, definition_id: str, graph: dict,
                   notifications: dict, ancestry: tuple[str, ...] = ()) -> dict:
        from copy import deepcopy
        from gyrfalcon.agents import agent_config_version, get_agent
        from gyrfalcon.flow.registry import get_activity

        result = deepcopy(graph)
        for node in result.get("nodes", []):
            implementation = node.get("implementation", node.get("type"))
            if implementation == "python":
                activity = get_activity(node.get("activity", ""), node.get("version", ""))
                if activity is None:
                    raise ValueError(f"Activity {node.get('activity')!r} is unavailable")
            elif implementation == "agent":
                agent_config = node.get("agent") or {}
                agent = get_agent(agent_config.get("id", ""))
                if agent is None or not agent.get("enabled", True):
                    raise ValueError(f"Gyrfalcon agent {agent_config.get('id')!r} is unavailable")
                actual = agent_config_version(agent)
                configured = agent_config.get("version")
                if configured not in (None, "", "latest", actual):
                    raise ValueError(f"Pinned agent version mismatch for {agent_config.get('id')!r}")
                agent_config["kind"] = "agent"
                agent_config["version"] = actual
            elif implementation == "a2a":
                agent_config = node.get("agent") or {}
                if (not agent_config.get("id") or not agent_config.get("version")
                        or not (agent_config.get("cardUrl") or agent_config.get("card_url"))):
                    raise ValueError(f"A2A node {node['id']!r} needs an agent id, pinned card version, and Agent Card URL")
            if node.get("type") == "notification":
                config = node.get("notification") or {}
                template_id = config.get("templateId") or config.get("template_id")
                template = self._store.get_notification_template(template_id) if template_id else None
                if template is None:
                    raise ValueError(f"Notification template {template_id!r} is unavailable")
                notifications[template_id] = template
            process = node.get("process") or {}
            if process.get("mode") == "reference":
                child_id = process.get("definitionId")
                if child_id in ancestry or child_id == definition_id:
                    raise ValueError("Recursive Process reference")
                if process.get("version") in (None, "", "latest"):
                    versions = self._store.list_versions(child_id)
                    if not versions:
                        raise ValueError(f"Process flow {child_id!r} has no published version")
                    process["version"] = versions[0]["version"]
                child_version = self._store.get_version(child_id, process["version"])
                if child_version is None:
                    raise ValueError(f"Process flow {child_id!r} version {process['version']} is unavailable")
                notifications.update(child_version.get("notification_snapshot") or {})
            elif process.get("mode") == "inline":
                process["graph"] = self._pin_graph(definition_id, process["graph"],
                                                    notifications, ancestry)
        return result

    def publish(self, definition_id: str) -> int:
        from gyrfalcon.flow.visual_graph import validate_visual_graph

        row = self.get(definition_id)
        if row is None:
            raise KeyError(definition_id)
        notifications: dict[str, dict] = {}
        graph = self._pin_graph(definition_id, row["draft"], notifications,
                                ancestry=(definition_id,))
        validate_visual_graph(
            graph,
            resolve_reference=lambda child_id, version: self._version_graph(child_id, version),
            resolve_notification=lambda template_id: notifications.get(template_id),
            activity_available=lambda name, version: get_activity(name, version) is not None,
            ancestry=(definition_id,),
        )
        attrs = self._flatten_attributes(graph)
        published = self._store.publish_definition(
            definition_id, graph, attrs, notifications, self._user_id)
        return int(published["version"])

    def unpublish(self, definition_id: str) -> None:
        if self.get(definition_id) is None:
            raise KeyError(definition_id)
        self._store.unpublish_definition(definition_id)

    def _version_graph(self, definition_id: str, version: int) -> dict:
        row = self._store.get_version(definition_id, version)
        if row is None:
            raise KeyError(f"No published graph {definition_id} version {version}")
        return row["graph"]

    def attributes(self, definition_id: str, version: int = 0) -> list[dict]:
        if self.get(definition_id) is None:
            raise KeyError(definition_id)
        if version == 0:
            rows = self._store.list_draft_attrs(definition_id)
            return [{**row, "default_value": row["default_value"]} for row in rows]
        published = self._store.get_version(definition_id, version)
        if published is None:
            raise KeyError(f"No published graph {definition_id} version {version}")
        return published["attribute_snapshot"]

    def version(self, definition_id: str, version: int) -> dict:
        return self._version_graph(definition_id, version)

    def latest_version(self, definition_id: str) -> int | None:
        definition = self.get(definition_id)
        if definition is None:
            raise KeyError(definition_id)
        if not definition.get("published", False):
            return None
        versions = self._store.list_versions(definition_id)
        return int(versions[0]["version"]) if versions else None

    def run(self, definition_id: str, version: int, inputs: dict,
            *, caller_key: str | None = None, trigger: str = "manual") -> dict:
        definition = self.get(definition_id)
        if definition is None:
            raise KeyError(definition_id)
        if not definition.get("published", False):
            raise ValueError("This flow is unpublished")
        if not definition.get("enabled", True):
            raise ValueError("This flow is disabled")
        if self._store.get_version(definition_id, version) is None:
            raise KeyError(f"No published graph {definition_id} version {version}")
        if not isinstance(inputs, dict):
            raise ValueError("Graph inputs must be an object")
        run = self._store.create_run(definition_id, version, inputs, trigger,
                                     user_id=self._user_id, caller_key=caller_key)
        self._store.record_event(run["id"], "flow.queued", {"trigger": trigger})
        return {"run_id": run["id"], "state": run["state"]}

    def close(self) -> None:
        self._store.close()
