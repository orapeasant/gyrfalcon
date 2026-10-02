"""Sequential, checkpointed interpreter for published visual flow graphs."""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from gyrfalcon.flow.graph_context import ActivityContext, ActivityResult, WaitResult
from gyrfalcon.flow.registry import get_activity
from gyrfalcon.flow.visual_graph import validate_attribute_value


class RuntimePort(Protocol):
    def get_run(self, run_id: str) -> dict | None: ...
    def update_run(self, run_id: str, **fields: Any) -> None: ...
    def create_node_visit(self, run_id: str, node_path: str, node_id: str,
                          node_kind: str, input_value: Any = None,
                          state: str = "running") -> Any: ...
    def update_node_visit(self, visit_id: str, **fields: Any) -> None: ...
    def get_waiting_visit(self, run_id: str) -> dict | None: ...
    def list_node_visits(self, run_id: str) -> list[dict]: ...
    def get_attrs(self, run_id: str, scope_path: str) -> dict[str, Any]: ...
    def put_attrs(self, run_id: str, scope_path: str, values: dict[str, Any]) -> None: ...


Dispatcher = Callable[[dict, ActivityContext, str], ActivityResult | WaitResult]
ReferenceResolver = Callable[[str, int], dict]


def node_at_path(graph: dict, node_path: str,
                 reference_resolver: ReferenceResolver | None = None) -> dict:
    """Find a node in a pinned graph by ``flow/process/flow/node`` path."""
    parts = node_path.split("/")
    if len(parts) < 2 or parts[0] != "flow" or len(parts) % 2 != 0:
        raise ValueError("Invalid visual node path")
    current = graph
    for index in range(1, len(parts) - 1, 2):
        process = next((n for n in current["nodes"] if n["id"] == parts[index]), None)
        if process is None or process["type"] != "process" or parts[index + 1] != "flow":
            raise ValueError("Invalid visual Process path")
        config = process["process"]
        if config["mode"] == "inline":
            current = config["graph"]
        else:
            if reference_resolver is None:
                raise ValueError("Process reference resolver is unavailable")
            current = reference_resolver(config["definitionId"], config["version"])
    node = next((n for n in current["nodes"] if n["id"] == parts[-1]), None)
    if node is None:
        raise ValueError("Visual node path is missing from pinned graph")
    return node


@dataclass(frozen=True)
class ExecutionOutcome:
    state: str
    run_id: str
    result: Any = None
    waiting_visit_id: str | None = None


class VisualExecutor:
    """Run one graph with one active path, persisting after every transition.

    ``context`` stores a frame stack. A waiting Notification releases its
    worker; resume loads the stack and finishes that same node visit.
    """

    def __init__(self, store: RuntimePort, run_id: str, graph: dict, *,
                 resolve_reference: ReferenceResolver | None = None,
                 activity_dispatcher: Dispatcher | None = None,
                 notification_dispatcher: Dispatcher | None = None,
                 clock: Callable[[], float] = time.time):
        self.graph = graph
        self.store = store
        self.run_id = run_id
        self.resolve_reference = resolve_reference
        self.activity_dispatcher = activity_dispatcher
        self.notification_dispatcher = notification_dispatcher
        self.clock = clock

    def run(self, inputs: dict[str, Any] | None = None) -> ExecutionOutcome:
        run_id = self.run_id
        row = self.store.get_run(run_id)
        if row is None:
            raise KeyError(run_id)
        if row.get("state") == "completed":
            return ExecutionOutcome("completed", run_id, row.get("result"))
        context = row.get("context") or {}
        if not context.get("frames"):
            if inputs is None:
                inputs = row.get("parameters") or {}
            if not isinstance(inputs, dict):
                raise ValueError("Flow inputs must be an object")
            start = next(node for node in self.graph["nodes"] if node["type"] == "start")
            context = {"inputs": inputs, "outputs": {}, "frames": [
                {"graph_path": "flow", "current": start["id"], "incoming": inputs,
                 "parent_visit": None, "parent_node": None}]}
            self._initialize_attrs(run_id, "flow", self.graph, inputs)
        waiting_visit_id = context.get("waiting_visit_id")
        if waiting_visit_id:
            visits = self.store.list_node_visits(run_id)
            visit = next((item for item in visits if item["id"] == waiting_visit_id), None)
            if visit is None or visit["state"] != "completed":
                raise ValueError("Saved waiting visit has no accepted response or timeout")
            frame = context["frames"][-1]
            graph = self._graph_at(frame["graph_path"])
            node = self._nodes(graph)[frame["current"]]
            if node["type"] == "notification":
                if not visit.get("transient"):
                    raise ValueError("Notification response has no selected transient")
                result = ActivityResult(visit["transient"], visit.get("response_value"))
            else:
                if self.activity_dispatcher is None:
                    raise ValueError("Activity dispatcher is unavailable for resume")
                resumed_context = self._activity_context(run_id, context, frame, node,
                                                         human_response=visit.get("response_value"))
                result = self.activity_dispatcher(node, resumed_context, waiting_visit_id)
                if isinstance(result, WaitResult):
                    self.store.update_node_visit(waiting_visit_id, state="waiting",
                                                 wait_deadline=result.deadline)
                    self.store.update_run(run_id, state="waiting", context=context)
                    return ExecutionOutcome("waiting", run_id, waiting_visit_id=waiting_visit_id)
            self._check_result(node, result)
            self.store.update_node_visit(waiting_visit_id, state="completed",
                                         transient=result.transient, output_value=result.output)
            if visit.get("ai_session_id"):
                self.store.update_visit_session(run_id, waiting_visit_id, "completed")
            context.pop("waiting_visit_id", None)
            self._finish_node(run_id, context, row.get("loop_state") or {}, frame,
                              graph, node, result, waiting_visit_id)
            completed = self.store.get_run(run_id)
            if completed and completed.get("state") == "completed":
                return ExecutionOutcome("completed", run_id, completed.get("result"))
        self.store.update_run(run_id, state="running", context=context)
        return self._advance(run_id, context, row.get("loop_state") or {})

    def resume(self, run_id: str, transient: str, response: Any = None,
               user_id: str | None = None) -> ExecutionOutcome:
        if run_id != self.run_id:
            raise ValueError("Executor run ID does not match resume run ID")
        row = self.store.get_run(run_id)
        if row is None:
            raise KeyError(run_id)
        if row.get("state") != "waiting":
            raise ValueError("Flow is not waiting")
        visit = self.store.get_waiting_visit(run_id)
        if visit is None:
            raise ValueError("Flow has no waiting node visit")
        context = row.get("context") or {}
        frames = context.get("frames") or []
        if not frames:
            raise ValueError("Waiting flow has no saved execution frame")
        frame = frames[-1]
        graph = self._graph_at(frame["graph_path"])
        node = self._nodes(graph)[frame["current"]]
        if node["type"] != "notification":
            raise ValueError("Agent Activity replies must resume through the dispatcher")
        path = self._node_path(frame, node)
        if visit["node_path"] != path:
            raise ValueError("Waiting node does not match saved context")
        result = ActivityResult(transient=transient, output=response)
        self._check_result(node, result)
        self.store.update_node_visit(visit["id"], state="completed", transient=transient,
                                     output_value=response, response_value=response,
                                     response_user_id=user_id)
        context.pop("waiting_visit_id", None)
        self._finish_node(run_id, context, row.get("loop_state") or {}, frame,
                          graph, node, result, visit["id"])
        current = self.store.get_run(run_id)
        if current and current.get("state") == "completed":
            return ExecutionOutcome("completed", run_id, current.get("result"))
        self.store.update_run(run_id, state="running", context=context)
        return self._advance(run_id, context, row.get("loop_state") or {})

    def _advance(self, run_id: str, context: dict, loop_state: dict) -> ExecutionOutcome:
        while context["frames"]:
            frame = context["frames"][-1]
            graph = self._graph_at(frame["graph_path"])
            node = self._nodes(graph)[frame["current"]]
            node_path = self._node_path(frame, node)
            visit = self.store.create_node_visit(run_id, node_path, node["id"],
                                                 node["type"], input_value=frame.get("incoming"))
            visit_id = visit["id"] if isinstance(visit, dict) else str(visit)
            self.store.update_run(run_id, current_node_path=node_path)
            try:
                if node["type"] == "process":
                    inner = self._process_graph(node)
                    self._initialize_attrs(run_id, node_path, node, {})
                    child_path = f"{node_path}/flow"
                    self._initialize_attrs(run_id, child_path, inner, {})
                    start = next(n for n in inner["nodes"] if n["type"] == "start")
                    context["frames"].append({"graph_path": child_path,
                                              "current": start["id"],
                                              "incoming": frame.get("incoming"),
                                              "parent_visit": visit_id,
                                              "parent_node": node_path})
                    self.store.update_run(run_id, context=context,
                                          current_node_path=f"{child_path}/{start['id']}")
                    continue
                result = self._execute_node(run_id, context, frame, node, visit_id)
                if isinstance(result, WaitResult):
                    context["waiting_visit_id"] = visit_id
                    self.store.update_node_visit(visit_id, state="waiting",
                                                 wait_deadline=result.deadline)
                    self.store.update_run(run_id, state="waiting", context=context,
                                          loop_state=loop_state, current_node_path=node_path)
                    return ExecutionOutcome("waiting", run_id, waiting_visit_id=visit_id)
                self._check_result(node, result)
                self.store.update_node_visit(visit_id, state="completed",
                                             output_value=result.output,
                                             transient=result.transient)
                self._finish_node(run_id, context, loop_state, frame, graph,
                                  node, result, visit_id)
            except Exception as exc:
                error = {"type": type(exc).__name__, "message": str(exc), "node_path": node_path}
                self.store.update_node_visit(visit_id, state="failed", error=error)
                for ancestor in context["frames"][:-1]:
                    parent_visit = ancestor.get("parent_visit")
                    if parent_visit:
                        self.store.update_node_visit(parent_visit, state="failed", error=error)
                self.store.update_run(run_id, state="failed", error=error,
                                      context=context, loop_state=loop_state,
                                      current_node_path=node_path)
                raise
            current = self.store.get_run(run_id)
            if current and current.get("state") == "completed":
                return ExecutionOutcome("completed", run_id, current.get("result"))
        raise RuntimeError("Flow ended without reaching End")

    def _execute_node(self, run_id: str, context: dict, frame: dict,
                      node: dict, visit_id: str) -> ActivityResult | WaitResult:
        kind = node["type"]
        if kind == "start":
            self._initialize_attrs(run_id, self._node_path(frame, node), node, {})
            return ActivityResult(node["transients"][0], frame.get("incoming"))
        if kind == "end":
            self._initialize_attrs(run_id, self._node_path(frame, node), node, {})
            values = node["transients"]
            selected = node.get("selectedTransient") or node.get("transient")
            if selected is None and len(values) == 1:
                selected = values[0]
            if selected is None and isinstance(frame.get("incoming"), dict):
                selected = frame["incoming"].get("transient")
            if selected is None:
                raise ValueError(f"End {node['id']!r} has ambiguous transient")
            return ActivityResult(selected, frame.get("incoming"))
        path = self._node_path(frame, node)
        self._initialize_attrs(run_id, path, node, {})
        activity_context = self._activity_context(run_id, context, frame, node)
        if kind == "notification":
            if self.notification_dispatcher is None:
                raise ValueError("Notification dispatcher is unavailable")
            return self.notification_dispatcher(node, activity_context, visit_id)
        implementation = node.get("implementation", kind)
        if implementation in {"agent", "a2a"} or kind in {"agent", "a2a"}:
            if self.activity_dispatcher is None:
                raise ValueError(f"{implementation} dispatcher is unavailable")
            return self.activity_dispatcher(node, activity_context, visit_id)
        if kind != "python":
            raise ValueError(f"Unsupported node type {kind!r}")
        activity = get_activity(node["activity"], node["version"])
        if activity is None:
            raise ValueError(f"Activity {node['activity']!r} version {node['version']!r} is unavailable")
        fn = activity.fn
        parameters = self._python_parameters(fn, node, activity_context)
        value = fn(**parameters)
        if isinstance(value, ActivityResult):
            return value
        if isinstance(value, dict) and "transient" in value:
            return ActivityResult(value["transient"], value.get("output", value.get("value")),
                                  value.get("attributes", {}))
        if len(node["transients"]) != 1:
            raise ValueError(f"Activity {node['id']!r} must return one declared transient")
        return ActivityResult(node["transients"][0], value)

    def _activity_context(self, run_id: str, context: dict, frame: dict,
                          node: dict, human_response: Any = None) -> ActivityContext:
        path = self._node_path(frame, node)
        graph = self._graph_at(frame["graph_path"])
        all_flow_attrs = self.store.get_attrs(run_id, frame["graph_path"])
        referenced_ids = set(node.get("flowAttributeRefs") or [])
        allowed_names = {item["name"] for item in graph.get("attributes", [])
                         if item.get("id") in referenced_ids}
        flow_attrs = {name: value for name, value in all_flow_attrs.items()
                      if name in allowed_names}
        outputs = dict(context["outputs"])
        prefix = f"{frame['graph_path']}/"
        outputs.update({key[len(prefix):]: value for key, value in context["outputs"].items()
                        if key.startswith(prefix) and "/" not in key[len(prefix):]})
        return ActivityContext(
            run_id=run_id, node_path=path, incoming_value=frame.get("incoming"),
            inputs=context["inputs"], outputs=outputs,
            flow_attributes=flow_attrs,
            attributes=self.store.get_attrs(run_id, path),
            human_response=human_response,
        )

    def _python_parameters(self, fn: Callable, node: dict, ctx: ActivityContext) -> dict:
        declared = node.get("inputs") or {}
        if not isinstance(declared, dict):
            raise ValueError("Activity inputs must be an object")
        values = {key: self._resolve_input(value, ctx) for key, value in declared.items()}
        signature = inspect.signature(fn)
        if not values:
            params = list(signature.parameters.values())
            required = [p for p in params if p.default is inspect.Parameter.empty
                        and p.kind in {inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                       inspect.Parameter.KEYWORD_ONLY}]
            if len(required) == 1:
                param = required[0]
                values[param.name] = ctx if param.name in {"ctx", "context"} or param.annotation is ActivityContext else ctx.incoming_value
        signature.bind(**values)
        return values

    @staticmethod
    def _resolve_input(value: Any, ctx: ActivityContext) -> Any:
        if not isinstance(value, dict) or set(value) != {"ref"}:
            return value
        reference = value["ref"]
        if not isinstance(reference, str):
            raise ValueError("Activity reference must be a string")
        root, _, tail = reference.partition(".")
        source = ctx.inputs if root == "inputs" else ctx.outputs.get(root)
        if source is None and root != "inputs":
            raise ValueError(f"Activity reference {reference!r} is unavailable")
        for part in tail.split(".") if tail else []:
            if not isinstance(source, dict) or part not in source:
                raise ValueError(f"Activity reference {reference!r} is unavailable")
            source = source[part]
        return source

    def _check_result(self, node: dict, result: ActivityResult) -> None:
        if not isinstance(result, ActivityResult) or not isinstance(result.transient, str):
            raise ValueError(f"Node {node['id']!r} did not return one transient")
        if result.transient not in node["transients"]:
            raise ValueError(f"Node {node['id']!r} returned undeclared transient {result.transient!r}")
        if not isinstance(result.attributes, dict):
            raise ValueError("Activity attribute writes must be an object")
        definitions = {a["name"]: a for a in node.get("attributes", [])}
        for name, value in result.attributes.items():
            if name not in definitions:
                raise ValueError(f"Node {node['id']!r} wrote undeclared attribute {name!r}")
            validate_attribute_value(definitions[name], value)

    def _finish_node(self, run_id: str, context: dict, loop_state: dict,
                     frame: dict, graph: dict, node: dict, result: ActivityResult,
                     visit_id: str) -> None:
        path = self._node_path(frame, node)
        if result.attributes:
            self.store.put_attrs(run_id, path, result.attributes)
        context["outputs"][path] = result.output
        if node["type"] == "end":
            context["frames"].pop()
            if not context["frames"]:
                self.store.update_run(run_id, state="completed", result=result.output,
                                      context=context, loop_state=loop_state,
                                      current_node_path=path)
                return
            parent = context["frames"][-1]
            parent_graph = self._graph_at(parent["graph_path"])
            parent_node = self._nodes(parent_graph)[parent["current"]]
            parent_result = ActivityResult(result.transient, result.output)
            self._check_result(parent_node, parent_result)
            self.store.update_node_visit(frame["parent_visit"], state="completed",
                                         transient=result.transient, output_value=result.output)
            self._finish_node(run_id, context, loop_state, parent, parent_graph,
                              parent_node, parent_result, frame["parent_visit"])
            return
        edges = [edge for edge in graph["edges"] if edge["from"] == node["id"]
                 and edge.get("transient") == result.transient]
        if len(edges) != 1:
            raise ValueError(f"Node {node['id']!r} has no unique link for {result.transient!r}")
        edge = edges[0]
        if edge.get("loop"):
            edge = self._select_loop_edge(run_id, node, graph, edge, path, loop_state)
            if edge.get("transient") != result.transient:
                self.store.update_node_visit(visit_id, transient=edge["transient"])
        frame["current"] = edge["to"]
        frame["incoming"] = result.output
        next_path = self._node_path(frame, self._nodes(graph)[edge["to"]])
        self.store.update_run(run_id, context=context, loop_state=loop_state,
                              current_node_path=next_path)

    def _select_loop_edge(self, run_id: str, node: dict, graph: dict, edge: dict,
                          path: str, loop_state: dict) -> dict:
        key = f"{path}:{edge.get('id', edge['transient'])}"
        state = loop_state.setdefault(key, {"count": 0, "started_at": self.clock()})
        controls = node.get("loopControl") or {}
        attributes = self.store.get_attrs(run_id, path)
        alternate = None
        max_timeout = attributes.get("max_timeout")
        if max_timeout is not None:
            state.setdefault("deadline", state["started_at"] + max_timeout)
            if self.clock() >= state["deadline"]:
                alternate = controls.get("timeoutTransient")
        max_loop = attributes.get("max_loop")
        if alternate is None and max_loop is not None and state["count"] + 1 >= max_loop:
            alternate = controls.get("maxCountTransient")
        if alternate is not None:
            choices = [e for e in graph["edges"] if e["from"] == node["id"]
                       and e.get("transient") == alternate and not e.get("loop")]
            if len(choices) != 1:
                raise ValueError(f"Loop at {node['id']!r} has no unique limit exit")
            return choices[0]
        state["count"] += 1
        return edge

    def _initialize_attrs(self, run_id: str, path: str, owner: dict, overrides: dict) -> None:
        existing = self.store.get_attrs(run_id, path)
        values = {}
        for attribute in owner.get("attributes", []):
            name = attribute["name"]
            if name in existing:
                continue
            value = overrides.get(name, attribute["defaultValue"])
            validate_attribute_value(attribute, value)
            values[name] = value
        if values:
            self.store.put_attrs(run_id, path, values)

    def _process_graph(self, node: dict) -> dict:
        process = node["process"]
        if process["mode"] == "inline":
            return process["graph"]
        if self.resolve_reference is None:
            raise ValueError("Process reference resolver is unavailable")
        return self.resolve_reference(process["definitionId"], process["version"])

    def _graph_at(self, path: str) -> dict:
        graph = self.graph
        if path == "flow":
            return graph
        parts = path.split("/")
        if not parts or parts[0] != "flow" or len(parts) % 2 != 1:
            raise ValueError("Invalid saved Process path")
        for index in range(1, len(parts), 2):
            if parts[index + 1] != "flow":
                raise ValueError("Invalid saved Process path")
            node = self._nodes(graph)[parts[index]]
            graph = self._process_graph(node)
        return graph

    @staticmethod
    def _nodes(graph: dict) -> dict[str, dict]:
        return {node["id"]: node for node in graph["nodes"]}

    @staticmethod
    def _node_path(frame: dict, node: dict) -> str:
        return f"{frame['graph_path']}/{node['id']}"
