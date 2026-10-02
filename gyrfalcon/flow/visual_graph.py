"""Validation of version 2 visual flow graphs before publication or execution."""

from __future__ import annotations

import math
from typing import Any, Callable


ATTRIBUTE_TYPES = {"string", "number", "boolean", "json"}
NODE_TYPES = {"start", "python", "notification", "process", "end", "agent", "a2a"}


def _attribute_value(attribute: dict, value: Any) -> bool:
    kind = attribute["type"]
    if kind == "string":
        return isinstance(value, str)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if kind == "boolean":
        return isinstance(value, bool)
    return True


def validate_attribute_value(attribute: dict, value: Any) -> None:
    """Enforce a design attribute's type and optional candidate list."""
    if not _attribute_value(attribute, value):
        raise ValueError(f"Attribute {attribute['name']!r} has an invalid value")
    if attribute.get("candidates") and value not in attribute["candidates"]:
        raise ValueError(f"Attribute {attribute['name']!r} is outside its allowed values")


def _attributes(raw: Any, owner: str) -> dict[str, dict]:
    if not isinstance(raw, list):
        raise ValueError(f"{owner} attributes must be a list")
    found: dict[str, dict] = {}
    ids: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise ValueError(f"{owner} has an invalid attribute")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip() or name in found or item["id"] in ids:
            raise ValueError(f"{owner} has a duplicate or unnamed attribute")
        if item.get("type") not in ATTRIBUTE_TYPES or not isinstance(item.get("candidates"), list):
            raise ValueError(f"Attribute {name!r} has an invalid type or candidates")
        if "defaultValue" not in item:
            raise ValueError(f"Attribute {name!r} needs a default")
        validate_attribute_value(item, item["defaultValue"])
        for candidate in item["candidates"]:
            if not _attribute_value(item, candidate):
                raise ValueError(f"Attribute {name!r} has an invalid candidate")
        ids.add(item["id"])
        found[name] = item
    return found


def _transients(node: dict) -> list[str]:
    values = node.get("transients")
    if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v for v in values):
        raise ValueError(f"Node {node['id']!r} needs declared transients")
    if len(values) != len(set(values)):
        raise ValueError(f"Node {node['id']!r} has duplicate transients")
    return values


def validate_visual_graph(
    graph: dict,
    *,
    resolve_reference: Callable[[str, int], dict] | None = None,
    resolve_notification: Callable[[str], Any] | None = None,
    activity_available: Callable[[str, str], bool] | None = None,
    ancestry: tuple[str, ...] = (),
) -> None:
    """Reject incomplete graphs, unbounded loops, and mutable child references.

    Resolvers are required for external references at publish time. A pinned
    Process version is part of the graph snapshot, never inferred at runtime.
    """
    if not isinstance(graph, dict) or graph.get("version") != 2:
        raise ValueError("A visual graph must have version 2")
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not nodes or not isinstance(edges, list):
        raise ValueError("A visual graph needs nodes and links")
    flow_attrs = _attributes(graph.get("attributes", []), "Flow")
    by_id: dict[str, dict] = {}
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not node["id"]:
            raise ValueError("Every node needs an id")
        node_id = node["id"]
        if node_id in by_id or node_id == "inputs" or "/" in node_id:
            raise ValueError(f"Duplicate or reserved node id {node_id!r}")
        if node.get("type") not in NODE_TYPES:
            raise ValueError(f"Node {node_id!r} has an unsupported type")
        by_id[node_id] = node
    starts = [n for n in nodes if n["type"] == "start"]
    if len(starts) != 1 or not any(n["type"] == "end" for n in nodes):
        raise ValueError("A graph needs exactly one Start and at least one End")

    outgoing: dict[str, dict[str, dict]] = {node_id: {} for node_id in by_id}
    for edge in edges:
        if not isinstance(edge, dict) or edge.get("from") not in by_id or edge.get("to") not in by_id:
            raise ValueError("A link must connect existing nodes")
        source, target = by_id[edge["from"]], by_id[edge["to"]]
        if source["type"] == "end" or target["type"] == "start":
            raise ValueError("A link cannot leave End or enter Start")
        transient = edge.get("transient")
        if not isinstance(transient, str) or not transient or transient not in _transients(source):
            raise ValueError(f"Link from {source['id']!r} has an undeclared transient")
        if transient in outgoing[source["id"]]:
            raise ValueError(f"Node {source['id']!r} has multiple links for {transient!r}")
        outgoing[source["id"]][transient] = edge

    for node in nodes:
        node_id, kind = node["id"], node["type"]
        values = _transients(node)
        attrs = _attributes(node.get("attributes", []), f"Node {node_id!r}")
        if kind != "end" and set(outgoing[node_id]) != set(values):
            raise ValueError(f"Node {node_id!r} needs one link per declared transient")
        if kind == "end" and outgoing[node_id]:
            raise ValueError("End cannot have outgoing links")
        refs = node.get("flowAttributeRefs", [])
        if not isinstance(refs, list) or any(ref not in {a["id"] for a in flow_attrs.values()} for ref in refs):
            raise ValueError(f"Node {node_id!r} references an unknown flow attribute")
        if kind in {"python", "agent", "a2a"}:
            implementation = node.get("implementation", kind)
            if implementation not in {"python", "agent", "a2a"}:
                raise ValueError(f"Node {node_id!r} has an invalid implementation")
            if implementation == "python":
                name, version = node.get("activity"), node.get("version")
                if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
                    raise ValueError(f"Python Activity {node_id!r} needs a pinned name and version")
                if activity_available is not None and not activity_available(name, version):
                    raise ValueError(f"Activity {name!r} version {version!r} is unavailable")
            else:
                agent = node.get("agent") or {}
                if not isinstance(agent, dict):
                    raise ValueError(f"Agent Activity {node_id!r} has invalid configuration")
                identity = agent.get("id")
                version = agent.get("version")
                if agent.get("kind") != implementation:
                    raise ValueError(f"Agent Activity {node_id!r} has mismatched implementation")
                if not isinstance(identity, str) or not identity or not isinstance(version, str) or not version:
                    raise ValueError(f"Agent Activity {node_id!r} needs pinned identity and version")
        if kind == "notification":
            config = node.get("notification")
            if not isinstance(config, dict) or config.get("mode") not in {"send", "wait"}:
                raise ValueError(f"Notification {node_id!r} needs a mode")
            template_id = config.get("templateId") or config.get("template_id")
            if not isinstance(template_id, str) or not template_id:
                raise ValueError(f"Notification {node_id!r} needs a template")
            if resolve_notification is None or resolve_notification(template_id) is None:
                raise ValueError(f"Notification template {template_id!r} is unavailable")
            if config["mode"] == "wait":
                timeout = config.get("timeoutSeconds") or config.get("timeout_seconds")
                exit_name = config.get("timeoutTransient") or config.get("timeout_transient")
                if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
                    raise ValueError(f"Waiting Notification {node_id!r} needs a positive timeout")
                if exit_name not in outgoing[node_id]:
                    raise ValueError(f"Waiting Notification {node_id!r} needs a timeout link")
            elif len(values) != 1:
                raise ValueError(f"Send-and-continue Notification {node_id!r} needs exactly one transient")
        if kind == "process":
            process = node.get("process")
            if not isinstance(process, dict):
                raise ValueError(f"Process {node_id!r} needs a flow")
            if process.get("mode") == "inline":
                inner = process.get("graph")
            elif process.get("mode") == "reference":
                definition_id, version = process.get("definitionId"), process.get("version")
                if not isinstance(definition_id, str) or not definition_id or not isinstance(version, int) or version < 1:
                    raise ValueError(f"Process {node_id!r} needs a pinned child version")
                if definition_id in ancestry:
                    raise ValueError("Recursive Process reference")
                if resolve_reference is None:
                    raise ValueError("A Process reference resolver is required")
                inner = resolve_reference(definition_id, version)
            else:
                raise ValueError(f"Process {node_id!r} has an invalid source")
            validate_visual_graph(inner, resolve_reference=resolve_reference,
                                  resolve_notification=resolve_notification,
                                  activity_available=activity_available,
                                  ancestry=(*ancestry, process.get("definitionId", node_id)))
            ends = {value for child in inner["nodes"] if child["type"] == "end" for value in child["transients"]}
            if set(values) != ends:
                raise ValueError(f"Process {node_id!r} outputs must match its inner Ends")
        for key, transient_key in (("max_loop", "maxCountTransient"), ("max_timeout", "timeoutTransient")):
            if key in attrs:
                limit = attrs[key]["defaultValue"]
                if not isinstance(limit, (int, float)) or isinstance(limit, bool) or limit <= 0:
                    raise ValueError(f"Node {node_id!r} needs a positive {key}")
                exit_name = node.get("loopControl", {}).get(transient_key)
                if exit_name not in outgoing[node_id] or outgoing[node_id][exit_name].get("loop"):
                    raise ValueError(f"Node {node_id!r} needs a {key} exit link")

    seen: set[str] = set()
    active: set[str] = set()

    def walk(node_id: str) -> None:
        seen.add(node_id)
        active.add(node_id)
        for edge in outgoing[node_id].values():
            target = edge["to"]
            if target in active:
                if not edge.get("loop"):
                    raise ValueError(f"Backward link from {node_id!r} must be marked as a loop")
                attrs = {a["name"] for a in by_id[node_id].get("attributes", [])}
                if not attrs.intersection({"max_loop", "max_timeout"}):
                    raise ValueError(f"Loop at {node_id!r} needs a count or time limit")
            elif target not in seen:
                walk(target)
        active.remove(node_id)

    walk(starts[0]["id"])
    if seen != set(by_id):
        raise ValueError("Graph contains unreachable nodes")
    for edge in edges:
        if edge.get("loop") and not _can_reach(edge["to"], edge["from"], outgoing):
            raise ValueError("A loop link must return to an earlier reachable node")


def _can_reach(start: str, target: str, outgoing: dict[str, dict[str, dict]]) -> bool:
    pending, seen = [start], set()
    while pending:
        current = pending.pop()
        if current == target:
            return True
        if current not in seen:
            seen.add(current)
            pending.extend(edge["to"] for edge in outgoing[current].values())
    return False
