import React, { useCallback, useMemo, useState } from "react";
import {
  Background, Controls, Handle, Position, ReactFlow,
  type Edge, type Node, type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { FlowGraph, FlowNode } from "./flowDesignerTypes";

export type FlowVisit = {
  id: string;
  node_id: string;
  node_path: string;
  node_kind: string;
  state: string;
  attempt: number;
  visit_seq?: number;
  transient?: string;
  error?: unknown;
  input_value?: unknown;
  output_value?: unknown;
  context_snapshot?: {
    incoming_value?: unknown;
    inputs?: unknown;
    outputs?: unknown;
    flow_attributes?: Record<string, unknown>;
    attributes?: Record<string, unknown>;
    attributes_after?: Record<string, unknown>;
    flow_context?: Record<string, unknown>;
  };
  started_at?: number;
  finished_at?: number;
};

type RunNodeData = {
  title: string;
  kind: string;
  state: string;
  transient?: string;
  attempts: number;
  onOpenProcess?: () => void;
};

const stateColor: Record<string, string> = {
  completed: "var(--green)", running: "var(--blue)", waiting: "var(--warning)",
  failed: "var(--red)", crashed: "var(--red)", skipped: "var(--fg-muted)", queued: "var(--purple)",
};

function RunNode({ data, selected }: NodeProps<Node<RunNodeData>>) {
  const color = stateColor[data.state] || "var(--fg-muted)";
  return <div onDoubleClick={(event) => {
    if (data.kind === "process" && data.onOpenProcess) {
      event.stopPropagation();
      data.onOpenProcess();
    }
  }} style={{ width: 178, minHeight: data.kind === "process" ? 76 : 62, padding: "8px 10px", boxSizing: "border-box",
    border: `1px solid ${selected ? "var(--btn-bg)" : color}`, borderRadius: 9,
    background: "var(--card)", color: "var(--fg)", boxShadow: selected ? "0 0 0 2px color-mix(in srgb, var(--btn-bg) 28%, transparent)" : "var(--shadow-popover)" }}>
    <Handle type="target" position={Position.Left} style={{ background: "var(--fg-muted)" }} />
    <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
      <span style={{ width: 8, height: 8, borderRadius: 8, background: color, flex: "none" }} />
      <strong style={{ fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{data.title}</strong>
    </div>
    <div style={{ display: "flex", justifyContent: "space-between", gap: 6, marginTop: 7, fontSize: 10, color: "var(--fg-muted)" }}>
      <span>{data.kind} · {data.state.replaceAll("_", " ")}</span>
      {data.attempts > 1 && <span>{data.attempts} visits</span>}
    </div>
    {data.kind === "process" && <div style={{ marginTop: 5, fontSize: 9.5, color: "var(--btn-bg)" }}>Double-click to open</div>}
    {data.transient && <div style={{ marginTop: 5, fontSize: 10, color: "var(--fg-muted)" }}>→ {data.transient}</div>}
    <Handle type="source" position={Position.Right} style={{ background: "var(--fg-muted)" }} />
  </div>;
}

const nodeTypes = { run: RunNode };
const pretty = (value: unknown) => JSON.stringify(value ?? {}, null, 2);

function fallbackPositions(graph: FlowGraph) {
  const nodes = new Map(graph.nodes.map((node) => [node.id, node]));
  const next = new Map<string, string[]>();
  for (const edge of graph.edges) next.set(edge.from, [...(next.get(edge.from) || []), edge.to]);
  const start = graph.nodes.find((node) => node.type === "start")?.id;
  const layer = new Map<string, number>();
  const queue = start ? [start] : [];
  if (start) layer.set(start, 0);
  while (queue.length) {
    const current = queue.shift()!;
    for (const target of next.get(current) || []) {
      if (!layer.has(target)) { layer.set(target, (layer.get(current) || 0) + 1); queue.push(target); }
    }
  }
  const byLayer = new Map<number, string[]>();
  for (const node of graph.nodes) {
    const value = layer.get(node.id) ?? Math.max(0, graph.nodes.indexOf(node));
    byLayer.set(value, [...(byLayer.get(value) || []), node.id]);
  }
  const positions = new Map<string, { x: number; y: number }>();
  for (const [column, ids] of byLayer) ids.forEach((id, row) =>
    positions.set(id, nodes.get(id)?.position || { x: 40 + column * 250, y: 40 + row * 130 }));
  return positions;
}

export function FlowRunGraph({ graph, visits }: { graph: FlowGraph; visits: FlowVisit[] }) {
  const [processPath, setProcessPath] = useState<string[]>([]);
  const [positionOverrides, setPositionOverrides] = useState<Record<string, { x: number; y: number }>>({});
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedVisitId, setSelectedVisitId] = useState<string | null>(null);
  const scopePath = ["flow", ...processPath.flatMap((id) => [id, "flow"])].join("/");
  let activeGraph = graph;
  const processLabels: string[] = [];
  for (const processId of processPath) {
    const processNode = activeGraph.nodes.find((node) => node.id === processId);
    const process = processNode?.process;
    const child = process?.mode === "inline" ? process.graph : process?.resolvedGraph;
    if (!processNode || processNode.type !== "process" || !child) break;
    processLabels.push(processNode.label || processNode.id);
    activeGraph = child;
  }
  const flatVisits = visits.filter((visit) => {
    const parts = visit.node_path.split("/");
    return visit.node_path.startsWith(`${scopePath}/`) &&
      parts.length === scopePath.split("/").length + 1;
  });
  const visitsByNode = useMemo(() => {
    const grouped = new Map<string, FlowVisit[]>();
    for (const visit of flatVisits) grouped.set(visit.node_id, [...(grouped.get(visit.node_id) || []), visit]);
    return grouped;
  }, [flatVisits]);
  const positions = useMemo(() => fallbackPositions(activeGraph), [activeGraph]);
  const openProcess = useCallback((node: FlowNode) => {
    if (node.type !== "process") return;
    const child = node.process?.mode === "inline" ? node.process.graph : node.process?.resolvedGraph;
    if (!child) return;
    setSelectedNodeId(null);
    setSelectedVisitId(null);
    setProcessPath((current) => [...current, node.id]);
  }, []);
  const nodes = useMemo<Node<RunNodeData>[]>(() => activeGraph.nodes.map((node: FlowNode) => {
    const history = visitsByNode.get(node.id) || [];
    const last = history.at(-1);
    const positionKey = `${scopePath}/${node.id}`;
    return { id: node.id, type: "run", position: positionOverrides[positionKey] || positions.get(node.id) || { x: 0, y: 0 },
      data: { title: node.label || node.id, kind: node.type, state: last?.state || "skipped",
        transient: last?.transient, attempts: history.length,
        onOpenProcess: node.type === "process" ? () => openProcess(node) : undefined } };
  }), [activeGraph, positions, visitsByNode, positionOverrides, scopePath, openProcess]);
  const edges = useMemo<Edge[]>(() => activeGraph.edges.map((edge) => {
    const taken = (visitsByNode.get(edge.from) || []).some((visit) =>
      visit.state === "completed" && visit.transient === edge.transient);
    return { id: edge.id, source: edge.from, target: edge.to, label: edge.transient,
      animated: taken && edge.loop === true,
      style: { stroke: taken ? "var(--green)" : "var(--border)", strokeWidth: taken ? 2.5 : 1.25 },
      labelStyle: { fill: taken ? "var(--green)" : "var(--fg-muted)", fontSize: 10 },
      labelBgStyle: { fill: "var(--card)" },
      markerEnd: { type: "arrowclosed" as const, color: taken ? "var(--green)" : "var(--border)" } };
  }), [activeGraph, visitsByNode]);
  const selectedNode = activeGraph.nodes.find((node) => node.id === selectedNodeId);
  const selectedVisits = selectedNode ? visitsByNode.get(selectedNode.id) || [] : [];
  const visit = selectedVisits.find((item) => item.id === selectedVisitId) || selectedVisits.at(-1);
  const snapshot = visit?.context_snapshot;
  return <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1.8fr) minmax(270px, 1fr)", minHeight: 440, border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden" }}>
    <div style={{ gridColumn: "1 / -1", display: "flex", alignItems: "center", gap: 7, minHeight: 36, padding: "0 12px", borderBottom: "1px solid var(--border)", background: "var(--card)", color: "var(--fg)", fontSize: 11 }}>
      <button type="button" onClick={() => { setProcessPath([]); setSelectedNodeId(null); setSelectedVisitId(null); }} disabled={!processPath.length} style={crumbButton(!processPath.length)}>Flow</button>
      {processLabels.map((name, index) => <React.Fragment key={`${processPath[index]}-${index}`}>
        <span style={{ color: "var(--fg-muted)" }}>›</span>
        <button type="button" onClick={() => { setProcessPath((current) => current.slice(0, index + 1)); setSelectedNodeId(null); setSelectedVisitId(null); }} disabled={index === processPath.length - 1} style={crumbButton(index === processPath.length - 1)}>{name}</button>
      </React.Fragment>)}
    </div>
    <div style={{ minWidth: 0, minHeight: 440, background: "var(--bg)" }}>
      <ReactFlow key={scopePath} nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView fitViewOptions={{ padding: 0.22 }}
        onNodeClick={(_, node) => { setSelectedNodeId(node.id); setSelectedVisitId(null); }}
        onNodeDrag={(_, node) => setPositionOverrides((current) => {
          const key = `${scopePath}/${node.id}`;
          if (current[key]?.x === node.position.x && current[key]?.y === node.position.y) return current;
          return { ...current, [key]: node.position };
        })}
        nodesDraggable nodesConnectable={false} elementsSelectable proOptions={{ hideAttribution: true }}>
        <Background color="var(--border)" gap={20} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
    <aside style={{ padding: 14, borderLeft: "1px solid var(--border)", overflow: "auto", maxHeight: 580, background: "var(--card)", color: "var(--fg)" }}>
      {!selectedNode ? <div style={{ color: "var(--fg-muted)", fontSize: 12 }}>Select a node to inspect its saved run context and attributes.</div> : <>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, fontWeight: 700, fontSize: 13 }}>
          <span>{selectedNode.label || selectedNode.id}</span>
          {selectedNode.type === "process" && <button type="button" onClick={() => openProcess(selectedNode)} style={openButton}>Open process ↗</button>}
        </div>
        <div style={{ color: "var(--fg-muted)", fontSize: 11, margin: "3px 0 12px" }}>{selectedNode.type} · {visit?.state || "not visited"}</div>
        {selectedVisits.length > 1 && <label style={{ display: "grid", gap: 4, marginBottom: 12, color: "var(--fg-muted)", fontSize: 11 }}>Visit / attempt
          <select value={visit?.id || ""} onChange={(event) => setSelectedVisitId(event.target.value)} style={{ padding: 6, border: "1px solid var(--border)", borderRadius: 5, background: "var(--bg)", color: "var(--fg)" }}>
            {selectedVisits.map((item) => <option key={item.id} value={item.id}>Attempt {item.attempt} · {item.state}</option>)}
          </select>
        </label>}
        {!visit ? <div style={{ fontSize: 12, color: "var(--fg-muted)" }}>This branch was not taken in this run.</div> : <>
          {snapshot ? <>
            <section style={{ marginBottom: 12 }}><strong style={{ fontSize: 11 }}>Activity attributes</strong><pre style={jsonStyle}>{pretty(snapshot.attributes)}</pre></section>
            {snapshot.attributes_after && <section style={{ marginBottom: 12 }}><strong style={{ fontSize: 11 }}>Activity attributes after completion</strong><pre style={jsonStyle}>{pretty(snapshot.attributes_after)}</pre></section>}
            <section style={{ marginBottom: 12 }}><strong style={{ fontSize: 11 }}>Flow attributes</strong><pre style={jsonStyle}>{pretty(snapshot.flow_attributes)}</pre></section>
            <section style={{ marginBottom: 12 }}><strong style={{ fontSize: 11 }}>Incoming value</strong><pre style={jsonStyle}>{pretty(snapshot.incoming_value)}</pre></section>
            <section style={{ marginBottom: 12 }}><strong style={{ fontSize: 11 }}>Available context</strong><pre style={jsonStyle}>{pretty({ inputs: snapshot.inputs, outputs: snapshot.outputs })}</pre></section>
            {snapshot.flow_context && <section style={{ marginBottom: 12 }}><strong style={{ fontSize: 11 }}>Flow context</strong><pre style={jsonStyle}>{pretty(snapshot.flow_context)}</pre></section>}
          </> : <div style={{ marginBottom: 12, color: "var(--fg-muted)", fontSize: 11 }}>No context snapshot is stored for this visit.</div>}
          {visit.transient && <div style={{ fontSize: 11, marginBottom: 10 }}>Selected output: <strong>{visit.transient}</strong></div>}
          {visit.output_value !== undefined && <section style={{ marginBottom: 12 }}><strong style={{ fontSize: 11 }}>Activity result</strong><pre style={jsonStyle}>{pretty(visit.output_value)}</pre></section>}
          {visit.error != null && <section><strong style={{ fontSize: 11, color: "var(--red)" }}>Failure details</strong><pre style={{ ...jsonStyle, color: "var(--red)" }}>{pretty(visit.error)}</pre></section>}
        </>}
      </>}
    </aside>
  </div>;
}

const jsonStyle: React.CSSProperties = {
  whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: 180, overflow: "auto",
  margin: "5px 0 0", padding: 8, border: "1px solid var(--border)", borderRadius: 5,
  background: "var(--bg)", color: "var(--fg)", fontSize: 10.5,
};

const crumbButton = (active: boolean): React.CSSProperties => ({
  border: 0, padding: "4px 6px", background: active ? "transparent" : "var(--sidebar-active)",
  color: active ? "var(--fg)" : "var(--btn-bg)", borderRadius: 4,
  font: "inherit", cursor: active ? "default" : "pointer", opacity: active ? 1 : 0.9,
});

const openButton: React.CSSProperties = {
  border: "1px solid var(--border)", borderRadius: 5, padding: "4px 7px",
  background: "var(--bg)", color: "var(--btn-bg)", fontSize: 10.5,
  cursor: "pointer", whiteSpace: "nowrap",
};
