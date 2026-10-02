import { useEffect, useMemo, useRef, useState, type Dispatch, type DragEvent, type MouseEvent, type PointerEvent, type SetStateAction } from "react";
import {
  Background, BaseEdge, Controls, EdgeLabelRenderer, Handle, MarkerType, MiniMap, NodeResizer, Position, ReactFlow, ReactFlowProvider,
  getBezierPath, getSmoothStepPath, getStraightPath,
  useReactFlow, type Connection, type Edge as CanvasEdge, type Node as CanvasNode,
  type EdgeProps, type NodeChange, type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import gearIcon from "../../../assets/blue/svg/gear.svg";
import developmentIcon from "../../../assets/blue/svg/development.svg";
import milestonesIcon from "../../../assets/blue/svg/milestones.svg";
import progressIcon from "../../../assets/blue/svg/progress.svg";
import diagramIcon from "../../../assets/blue/svg/diagram.svg";
import collaborationIcon from "../../../assets/blue/svg/collaboration.svg";
import chatIcon from "../../../assets/blue/svg/chat.svg";
import { ArrowLeft, ChevronDown, ChevronRight, Grip, Map as MapIcon, Maximize2, Minimize2, PanelLeftClose, PanelLeftOpen, PanelRightClose, PanelRightOpen, Plus, Trash2, X } from "lucide-react";
import { api } from "../lib/api";
import type { Activity, FlowAttribute, FlowEdge, FlowGraph, FlowNode, FlowNodeKind, GraphDefinition } from "./flowDesignerTypes";
import "./FlowCanvasEditor.css";

type Props = {
  graph: FlowGraph;
  setGraph: Dispatch<SetStateAction<FlowGraph>>;
  activities: Activity[];
  onActivitiesChange?: (activities: Activity[]) => void;
  onCodeEditorDirtyChange?: (dirty: boolean, save: () => Promise<boolean>) => void;
  definitions: GraphDefinition[];
  currentDefinitionId?: string;
  onOpenAgent?: (agentId: string) => void;
  onMessage: (message: string) => void;
  depth?: number;
};

type CardData = { kind: FlowNodeKind; title: string; subtitle: string; transients: string[]; width: number; height: number };
type FlowLinkData = {
  label: string;
  routing?: FlowEdge["routing"];
  labelPosition?: { x: number; y: number };
  onPositionChange: (position: { x: number; y: number }) => void;
  onSelect: () => void;
};
const nodeIcons: Partial<Record<FlowNodeKind, string>> = {
  process: gearIcon,
  python: developmentIcon,
  end: milestonesIcon,
  start: progressIcon,
  agent: diagramIcon,
  a2a: collaborationIcon,
  notification: chatIcon,
};
const iconFor = (kind: FlowNodeKind) => {
  const icon = nodeIcons[kind];
  return icon ? <img className={`flow-node-icon-${kind}`} src={icon} alt="" draggable={false} /> : null;
};
const nodeKindLabel = (kind: FlowNodeKind) => kind === "a2a" ? "A2A" : kind === "python" ? "Activity" : kind[0].toUpperCase() + kind.slice(1);
const defaultTransients = (kind: FlowNodeKind) => kind === "end" ? ["done"] : ["next"];
const endTransients = (graph: FlowGraph) => [...new Set(graph.nodes.filter((node) => node.type === "end").flatMap((node) => node.transients || []))];
const uid = (prefix: string) => `${prefix}_${crypto.randomUUID().slice(0, 8)}`;
const transientValues = (node: FlowNode, flowAttributes: FlowAttribute[] = []) => {
  const reference = node.transientAttribute;
  if (!reference) return node.transients || [];
  const attributes = reference.scope === "flow" ? flowAttributes : node.attributes || [];
  const attribute = attributes.find((item) => item.id === reference.id);
  return (attribute?.candidates || []).filter((value): value is string => typeof value === "string" && !!value.trim());
};
const syncOutgoingLinks = (edges: FlowEdge[], nodeId: string, values: string[]) => {
  const used = new Set<string>();
  return edges.map((edge) => {
    if (edge.from !== nodeId) return edge;
    const existing = edge.transient && values.includes(edge.transient) && !used.has(edge.transient)
      ? edge.transient : values.find((value) => !used.has(value));
    if (!existing) return edge;
    used.add(existing);
    return { ...edge, transient: existing };
  });
};
const display = (value: unknown) => typeof value === "string" ? value : JSON.stringify(value ?? "");
const parseValue = (raw: string, type: string): unknown => {
  if (type === "string") return raw;
  if (type === "number") { const value = Number(raw); if (!Number.isFinite(value)) throw new Error("Enter a valid number."); return value; }
  if (type === "boolean") { if (raw !== "true" && raw !== "false") throw new Error("Use true or false."); return raw === "true"; }
  return JSON.parse(raw);
};

function arrangedGraph(graph: FlowGraph): FlowGraph {
  const nodeById = new Map(graph.nodes.map((node) => [node.id, node]));
  const levels = new Map<string, number>();
  const queue = graph.nodes.filter((node) => node.type === "start").map((node) => node.id);
  for (const id of queue) levels.set(id, 0);
  for (let index = 0; index < queue.length; index++) {
    const from = queue[index];
    for (const edge of graph.edges) {
      if (edge.from !== from || edge.loop || !nodeById.has(edge.to) || levels.has(edge.to)) continue;
      levels.set(edge.to, (levels.get(from) || 0) + 1);
      queue.push(edge.to);
    }
  }
  const lastLevel = Math.max(0, ...levels.values()) + 1;
  graph.nodes.forEach((node) => { if (!levels.has(node.id)) levels.set(node.id, lastLevel); });
  const columns = new Map<number, FlowNode[]>();
  for (const node of graph.nodes) {
    const level = levels.get(node.id)!;
    columns.set(level, [...(columns.get(level) || []), node]);
  }
  const positions = new Map<string, { x: number; y: number }>();
  for (const [level, column] of [...columns].sort(([a], [b]) => a - b)) {
    column.sort((a, b) => (a.position?.y || 0) - (b.position?.y || 0));
    column.forEach((node, index) => positions.set(node.id, { x: 100 + level * 230, y: 100 + index * 130 }));
  }
  return { ...graph, nodes: graph.nodes.map((node) => ({ ...node, position: positions.get(node.id) })) };
}

function NodeCard({ data, selected }: NodeProps) {
  const card = data as CardData;
  return <div className="flow-node-wrap" style={{ width: card.width, height: card.height }} title={`${card.title} · ${card.subtitle}`} aria-label={`${card.subtitle}: ${card.title}`}>
    <NodeResizer isVisible={!!selected} minWidth={50} minHeight={50} color="var(--btn-bg)" handleClassName="flow-node-resize-handle" lineClassName="flow-node-resize-line" />
    <div className={`flow-card flow-card-${card.kind} ${selected ? "selected" : ""}`} style={{ width: card.width, height: card.height }}>
      {card.kind !== "start" && <Handle type="target" position={Position.Left} />}
      <span className="flow-card-icon">{iconFor(card.kind)}</span>
      {card.kind !== "end" && <Handle type="source" position={Position.Right} />}
    </div>
    <span className="flow-node-label">{card.title}</span>
  </div>;
}

function FlowLinkEdge(props: EdgeProps) {
  const flow = useReactFlow();
  const { id, sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition, markerEnd, style, data } = props;
  const edgeData = data as FlowLinkData | undefined;
  const [dragPosition, setDragPosition] = useState<{ x: number; y: number } | null>(null);
  // Use React Flow's measured handle coordinates directly. Recomputing them
  // from internal node measurements can produce invalid paths while imported
  // nodes are being initialized, leaving otherwise valid edges invisible.
  const pathArgs = { sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition };
  const [edgePath, defaultX, defaultY] = edgeData?.routing === "straight"
    ? getStraightPath(pathArgs)
    : edgeData?.routing === "step"
      ? getSmoothStepPath({ ...pathArgs, borderRadius: 0 })
      : edgeData?.routing === "smoothstep"
        ? getSmoothStepPath({ ...pathArgs, borderRadius: 12 })
        : getBezierPath(pathArgs);
  const labelPosition = dragPosition || edgeData?.labelPosition || { x: defaultX, y: defaultY };
  const onPointerDown = (event: PointerEvent<HTMLButtonElement>) => {
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    edgeData?.onSelect();
    setDragPosition(flow.screenToFlowPosition({ x: event.clientX, y: event.clientY }));
  };
  const onPointerMove = (event: PointerEvent<HTMLButtonElement>) => {
    if (!event.currentTarget.hasPointerCapture(event.pointerId)) return;
    event.preventDefault();
    setDragPosition(flow.screenToFlowPosition({ x: event.clientX, y: event.clientY }));
  };
  const onPointerUp = (event: PointerEvent<HTMLButtonElement>) => {
    if (!event.currentTarget.hasPointerCapture(event.pointerId)) return;
    const position = flow.screenToFlowPosition({ x: event.clientX, y: event.clientY });
    event.currentTarget.releasePointerCapture(event.pointerId);
    edgeData?.onPositionChange(position);
    setDragPosition(null);
  };
  return <>
    <BaseEdge id={id} path={edgePath} markerEnd={markerEnd} style={style} />
    <EdgeLabelRenderer>
      <button type="button" className="nodrag nopan flow-edge-label" style={{
        position: "absolute", transform: `translate(-50%, -50%) translate(${labelPosition.x}px, ${labelPosition.y}px)`,
        pointerEvents: "all",
      }} onClick={(event) => { event.stopPropagation(); edgeData?.onSelect(); }}
        onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp}
        aria-label={`Transient ${edgeData?.label || "next"}; drag to move label`}>
        {edgeData?.label || "next"}
      </button>
    </EdgeLabelRenderer>
  </>;
}

const nodeTypes = { flowCard: NodeCard };
const edgeTypes = { flowLink: FlowLinkEdge };

export function AttributeEditor({ attributes, onChange, onMessage }: {
  attributes: FlowAttribute[];
  onChange: (attributes: FlowAttribute[]) => void;
  onMessage: (message: string) => void;
}) {
  const [expanded, setExpanded] = useState(true);
  const [collapsedAttributes, setCollapsedAttributes] = useState<Record<string, boolean>>({});
  const update = (index: number, patch: Partial<FlowAttribute>) => onChange(attributes.map((attribute, i) => i === index ? { ...attribute, ...patch } : attribute));
  return <div className="flow-attributes">
    <button className="flow-attributes-heading" type="button" onClick={() => setExpanded(!expanded)}>
      {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />} Attributes <span>{attributes.length}</span>
    </button>
    {expanded && <>
      {attributes.map((attribute, index) => <div className="flow-attribute" key={attribute.id}>
        <button className="flow-attribute-heading" type="button" aria-expanded={!collapsedAttributes[attribute.id]} onClick={() => setCollapsedAttributes((current) => ({ ...current, [attribute.id]: !current[attribute.id] }))}>
          {collapsedAttributes[attribute.id] ? <ChevronRight size={13} /> : <ChevronDown size={13} />}<span>{attribute.name || "Unnamed attribute"}</span>
        </button>
        {!collapsedAttributes[attribute.id] && <div className="flow-attribute-fields">
        <div className="flow-attribute-top"><input aria-label="Attribute name" value={attribute.name} onChange={(event) => update(index, { name: event.target.value })} /><button type="button" title="Delete attribute" aria-label={`Delete ${attribute.name}`} onClick={() => onChange(attributes.filter((_, i) => i !== index))}><Trash2 size={14} /></button></div>
        <select aria-label={`${attribute.name} type`} value={attribute.type} onChange={(event) => update(index, { type: event.target.value, defaultValue: event.target.value === "number" ? 0 : event.target.value === "boolean" ? false : "", candidates: [] })}>
          <option value="string">Text</option><option value="number">Number</option><option value="boolean">Boolean</option><option value="json">JSON</option>
        </select>
        <label>Default<input key={`${attribute.id}-${attribute.type}-${display(attribute.defaultValue)}`} defaultValue={display(attribute.defaultValue)} onBlur={(event) => { try { update(index, { defaultValue: parseValue(event.target.value, attribute.type) }); } catch (error) { onMessage(String(error)); } }} /></label>
        <label>Allowed values, one per line<textarea key={`${attribute.id}-${attribute.type}-candidates`} defaultValue={attribute.candidates.map(display).join("\n")} onBlur={(event) => {
          try { update(index, { candidates: event.target.value.split("\n").map((item) => item.trim()).filter(Boolean).map((item) => parseValue(item, attribute.type)) }); }
          catch (error) { onMessage(String(error)); }
        }} /></label>
        </div>}
      </div>)}
      <button className="flow-small-action" type="button" onClick={() => onChange([...attributes, { id: uid("attr"), name: `attribute_${attributes.length + 1}`, type: "string", defaultValue: "", candidates: [] }])}><Plus size={13} /> Add attribute</button>
    </>}
  </div>;
}

function EditorBody({ graph, setGraph, activities, onActivitiesChange, onCodeEditorDirtyChange, definitions, currentDefinitionId, onOpenAgent, onMessage, depth = 0 }: Props) {
  const flow = useReactFlow();
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [linkSource, setLinkSource] = useState<string | null>(null);
  const [linkTransient, setLinkTransient] = useState<string | null>(null);
  const [contextMenu, setContextMenu] = useState<{ nodeId: string; x: number; y: number } | null>(null);
  const [edgeMenu, setEdgeMenu] = useState<{ edgeId: string; x: number; y: number } | null>(null);
  const [paneMenu, setPaneMenu] = useState<{ x: number; y: number } | null>(null);
  const [inlineId, setInlineId] = useState<string | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(true);
  const [propertiesOpen, setPropertiesOpen] = useState(true);
  const [paletteSections, setPaletteSections] = useState({ nodes: true, catalog: true, attributes: true });
  const [codeEditorOpen, setCodeEditorOpen] = useState(false);
  const [activityModules, setActivityModules] = useState<string[]>([]);
  const [expandedActivityModules, setExpandedActivityModules] = useState<Record<string, boolean>>({});
  const [agentCatalog, setAgentCatalog] = useState<{ id: string; name: string; enabled?: boolean; flow_version?: string }[]>([]);
  const [notificationTemplates, setNotificationTemplates] = useState<{ id: string; name: string }[]>([]);
  const [moduleName, setModuleName] = useState("");
  const [moduleCode, setModuleCode] = useState("");
  const [moduleBaseline, setModuleBaseline] = useState({ name: "", code: "" });
  const [moduleBusy, setModuleBusy] = useState(false);
  const [moduleError, setModuleError] = useState("");
  const codeDirty = codeEditorOpen && (moduleName !== moduleBaseline.name || moduleCode !== moduleBaseline.code);
  const codeSaveRef = useRef<() => Promise<boolean>>(async () => false);
  const [miniMapPosition, setMiniMapPosition] = useState({ x: 16, y: 72 });
  const [miniMapMode, setMiniMapMode] = useState<"open" | "minimized" | "closed">("closed");
  const [miniMapSize, setMiniMapSize] = useState({ width: 200, height: 150 });
  const canvasRef = useRef<HTMLDivElement>(null);
  const miniMapDrag = useRef<{ x: number; y: number; left: number; top: number } | null>(null);
  const miniMapResize = useRef<{ x: number; y: number; width: number; height: number } | null>(null);
  const selectedNode = graph.nodes.find((node) => node.id === selectedNodeId);
  const selectedEdge = graph.edges.find((edge) => edge.id === selectedEdgeId);
  const inlineNode = graph.nodes.find((node) => node.id === inlineId);
  const sourceNode = graph.nodes.find((node) => node.id === selectedEdge?.from);
  const menuEdge = graph.edges.find((edge) => edge.id === edgeMenu?.edgeId);
  const menuSource = graph.nodes.find((node) => node.id === menuEdge?.from);
  const attrScope = selectedNode || null;
  const attributes = attrScope?.attributes || graph.attributes || [];
  const transientAttributeOptions = [
    ...(graph.attributes || []).map((attribute) => ({ scope: "flow" as const, attribute, label: `Flow · ${attribute.name}` })),
    ...(selectedNode?.attributes || []).map((attribute) => ({ scope: "node" as const, attribute, label: `${selectedNode?.type === "python" || selectedNode?.type === "agent" || selectedNode?.type === "a2a" ? "Activity" : "Node"} · ${attribute.name}` })),
  ].filter(({ attribute }) => attribute.type === "string" && attribute.candidates.length > 0 && attribute.candidates.every((value) => typeof value === "string" && !!value.trim()));
  const transientAttributeSelection = selectedNode?.transientAttribute
    ? `${selectedNode.transientAttribute.scope}:${selectedNode.transientAttribute.id}` : "";
  const hasLoop = !!selectedNode && graph.edges.some((edge) => edge.from === selectedNode.id && edge.loop);
  const limitTransients = selectedNode?.transients?.filter((value) => !graph.edges.some((edge) => edge.from === selectedNode.id && edge.loop && edge.transient === value)) || [];
  const nodes = useMemo<CanvasNode[]>(() => graph.nodes.map((node, index) => ({
    id: node.id,
    type: "flowCard",
    position: node.position || { x: 110 + (index % 3) * 240, y: 100 + Math.floor(index / 3) * 140 },
    width: node.width || 50,
    height: node.height || 50,
    data: { kind: node.type, title: node.type === "a2a" && node.label?.toLowerCase() === "a2a" ? "A2A" : node.label || node.id, subtitle: node.type === "python" ? "Activity" : node.type === "agent" ? "Agent" : node.type === "process" ? (node.process?.mode === "reference" ? "Saved flow" : "Inline flow") : nodeKindLabel(node.type), transients: node.transients || defaultTransients(node.type), width: node.width || 50, height: node.height || 50 } satisfies CardData,
  })), [graph.nodes]);
  const edges = useMemo<CanvasEdge[]>(() => graph.edges.map((edge) => ({
    id: edge.id,
    source: edge.from,
    target: edge.to,
    type: "flowLink",
    animated: !!edge.loop,
    reconnectable: true,
    markerEnd: { type: MarkerType.ArrowClosed },
    labelX: edge.labelPosition?.x,
    labelY: edge.labelPosition?.y,
    data: {
      label: edge.transient || (edge.when ? "condition" : "next"),
      routing: edge.routing || graph.edgeRouting || "bezier",
      labelPosition: edge.labelPosition,
      onPositionChange: (position: { x: number; y: number }) => patchEdge(edge.id, { labelPosition: position }),
      onSelect: () => { setSelectedEdgeId(edge.id); setSelectedNodeId(null); },
    },
    style: { stroke: "var(--fg)", strokeWidth: selectedEdgeId === edge.id ? 2.5 : 2 },
  })), [graph.edgeRouting, graph.edges, selectedEdgeId]);

  const patchNode = (id: string, patch: Partial<FlowNode>) => setGraph((current) => ({ ...current, nodes: current.nodes.map((node) => node.id === id ? { ...node, ...patch } : node) }));
  const setProcessSource = (id: string, process: FlowNode["process"], outputs: string[]) => setGraph((current) => ({
    ...current,
    nodes: current.nodes.map((node) => node.id === id ? { ...node, process, transients: outputs } : node),
    edges: current.edges.map((edge) => edge.from === id && outputs.length && !outputs.includes(edge.transient || "") ? { ...edge, transient: outputs[0] } : edge),
  }));
  const patchEdge = (id: string, patch: Partial<FlowEdge>) => setGraph((current) => ({ ...current, edges: current.edges.map((edge) => edge.id === id ? { ...edge, ...patch } : edge) }));
  const removeLink = (id: string) => {
    setGraph((current) => ({ ...current, edges: current.edges.filter((edge) => edge.id !== id) }));
    setSelectedEdgeId((current) => current === id ? null : current);
  };
  const addLink = (from: string, to: string, transient?: string) => {
    if (from === to) { onMessage("Choose a different target node."); return; }
    const source = graph.nodes.find((node) => node.id === from);
    if (!source || source.type === "end") { onMessage("An End activity cannot have outgoing links."); return; }
    if (graph.nodes.find((node) => node.id === to)?.type === "start") { onMessage("Start cannot have an incoming link."); return; }
    const values = source.transients || ["next"];
    const value = transient || values.find((candidate) => !graph.edges.some((edge) => edge.from === from && edge.transient === candidate)) || values[0];
    if (!value) { onMessage("Declare an output transient before linking this activity."); return; }
    const existing = graph.edges.find((edge) => edge.from === from && edge.transient === value);
    if (existing) {
      setGraph((current) => ({ ...current, edges: current.edges.map((edge) => edge.id === existing.id ? { ...edge, to } : edge) }));
      setSelectedEdgeId(existing.id);
      setSelectedNodeId(null);
      return;
    }
    const edge: FlowEdge = { id: uid("link"), from, to, transient: value, routing: graph.edgeRouting || "bezier" };
    setGraph((current) => ({ ...current, edges: [...current.edges, edge] }));
    setSelectedEdgeId(edge.id);
    setSelectedNodeId(null);
  };

  const makeNode = (kind: FlowNodeKind, position?: { x: number; y: number }, activityKey?: string) => {
    if (kind === "start" && graph.nodes.some((node) => node.type === "start")) { onMessage("A flow can have only one Start activity."); return; }
    const activity = activities.find((item) => `${item.name}@${item.version}` === activityKey) || activities[0];
    if (kind === "python" && !activity) { onMessage("No registered Python activities are available."); return; }
    const node: FlowNode = {
      id: uid(kind === "python" ? "activity" : kind), type: kind, label: nodeKindLabel(kind),
      position: position || flow.screenToFlowPosition({ x: window.innerWidth / 2, y: window.innerHeight / 2 }),
      transients: defaultTransients(kind), attributes: [],
      ...(kind === "python" ? { activity: activity!.name, version: activity!.version, implementation: "python" as const, annotation: activity!.description || "", inputs: {} } : {}),
      ...(kind === "agent" || kind === "a2a" ? { implementation: kind, agent: { id: "", version: "", kind } } : {}),
      ...(kind === "notification" ? { notification: { mode: "send" as const, templateId: "" } } : {}),
      ...(kind === "process" ? { process: { mode: "inline" as const, graph: { version: 2, nodes: [], edges: [], attributes: [] } } } : {}),
    };
    setGraph((current) => {
      const existing = current.nodes;
      const last = [...existing].reverse().find((item) => item.type !== "end");
      const end = existing.find((item) => item.type === "end");
      const nextEdges = [...current.edges];
      if (kind === "start" && existing.length) nextEdges.push({ id: uid("link"), from: node.id, to: existing[0].id, transient: "next" });
      else if (last && kind !== "start") {
        const direct = end && nextEdges.findIndex((edge) => edge.from === last.id && edge.to === end.id);
        if (direct !== undefined && direct >= 0 && kind !== "end") {
          nextEdges.splice(direct, 1);
          nextEdges.push({ id: uid("link"), from: last.id, to: node.id, transient: last.transients?.[0] || "next" });
          nextEdges.push({ id: uid("link"), from: node.id, to: end!.id, transient: node.transients?.[0] || "next" });
        } else if (!nextEdges.some((edge) => edge.from === last.id && edge.transient === (last.transients?.[0] || "next"))) {
          nextEdges.push({ id: uid("link"), from: last.id, to: node.id, transient: last.transients?.[0] || "next" });
        }
      }
      return { ...current, version: 2, nodes: [...existing, node], edges: nextEdges };
    });
    setSelectedNodeId(node.id);
    setSelectedEdgeId(null);
  };

  const onDrop = (event: DragEvent) => {
    event.preventDefault();
    const kind = event.dataTransfer.getData("application/gyrfalcon-node") as FlowNodeKind;
    if (!["start", "python", "agent", "a2a", "notification", "process", "end"].includes(kind)) return;
    makeNode(kind, flow.screenToFlowPosition({ x: event.clientX, y: event.clientY }), event.dataTransfer.getData("application/gyrfalcon-activity") || undefined);
  };
  const onNodeClick = (event: MouseEvent, node: CanvasNode) => {
    setContextMenu(null);
    if (event.ctrlKey || event.metaKey || linkTransient) {
      if (linkSource && linkSource !== node.id) { addLink(linkSource, node.id, linkTransient || undefined); setLinkSource(null); setLinkTransient(null); }
      else { setLinkSource(node.id); setSelectedNodeId(node.id); onMessage("Select the target activity to create a transient link."); }
      return;
    }
    setSelectedNodeId(node.id);
    setSelectedEdgeId(null);
  };
  const onConnect = (connection: Connection) => { if (connection.source && connection.target) addLink(connection.source, connection.target); };
  const onNodesChange = (changes: NodeChange[]) => {
    const moved = new Map<string, { x: number; y: number }>();
    const resized = new Map<string, { width: number; height: number }>();
    for (const change of changes) {
      if (change.type === "position" && change.position) moved.set(change.id, change.position);
      // Initial ResizeObserver measurements belong to React Flow. Writing them
      // back into our graph creates new node objects before handleBounds can be
      // retained, so edges never get endpoints. Persist only user resizes.
      if (change.type === "dimensions" && change.dimensions && change.resizing) resized.set(change.id, change.dimensions);
    }
    if (!moved.size && !resized.size) return;
    setGraph((current) => ({ ...current, nodes: current.nodes.map((node) => {
      const position = moved.get(node.id);
      const dimensions = resized.get(node.id);
      return position || dimensions ? { ...node, ...(position ? { position } : {}), ...(dimensions ? dimensions : {}) } : node;
    }) }));
  };
  const removeNode = (id: string) => { setGraph((current) => ({ ...current, nodes: current.nodes.filter((node) => node.id !== id), edges: current.edges.filter((edge) => edge.from !== id && edge.to !== id) })); setSelectedNodeId(null); setSelectedEdgeId(null); setContextMenu(null); };
  useEffect(() => {
    if (!selectedNodeId) return;
    const removeSelectedNode = (event: KeyboardEvent) => {
      if (event.key !== "Delete" && event.key !== "Backspace") return;
      const target = event.target;
      if (target instanceof HTMLElement && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))) return;
      event.preventDefault();
      removeNode(selectedNodeId);
    };
    window.addEventListener("keydown", removeSelectedNode);
    return () => window.removeEventListener("keydown", removeSelectedNode);
  }, [removeNode, selectedNodeId]);
  const changeNodeId = (id: string, next: string) => {
    if (!next.trim() || graph.nodes.some((node) => node.id === next && node.id !== id)) return;
    setGraph((current) => ({ ...current, nodes: current.nodes.map((node) => node.id === id ? { ...node, id: next } : node), edges: current.edges.map((edge) => ({ ...edge, from: edge.from === id ? next : edge.from, to: edge.to === id ? next : edge.to })) }));
    setSelectedNodeId(next);
  };
  const setTransientAttribute = (nodeId: string, reference?: { scope: "flow" | "node"; id: string }) => {
    setGraph((current) => {
      const node = current.nodes.find((item) => item.id === nodeId);
      if (!node) return current;
      const updatedNode = { ...node, transientAttribute: reference };
      const values = reference ? transientValues(updatedNode, current.attributes || []) : [];
      updatedNode.transients = values;
      return {
        ...current,
        nodes: current.nodes.map((item) => item.id === nodeId ? updatedNode : item),
        edges: syncOutgoingLinks(current.edges, nodeId, values),
      };
    });
  };
  const setAttributes = (updated: FlowAttribute[]) => setGraph((current) => {
    if (attrScope) {
      const nodes = current.nodes.map((node) => {
        if (node.id !== attrScope.id) return node;
        const next = { ...node, attributes: updated };
        if (node.transientAttribute?.scope === "node") next.transients = transientValues(next, current.attributes || []);
        return next;
      });
      const node = nodes.find((item) => item.id === attrScope.id)!;
      return { ...current, nodes, edges: node.transientAttribute?.scope === "node"
        ? syncOutgoingLinks(current.edges, node.id, node.transients || []) : current.edges };
    }
    const nodes = current.nodes.map((node) => node.transientAttribute?.scope === "flow"
      ? { ...node, transients: transientValues(node, updated) } : node);
    const edges = nodes.reduce((items, node) => node.transientAttribute?.scope === "flow"
      ? syncOutgoingLinks(items, node.id, node.transients || []) : items, current.edges);
    return { ...current, attributes: updated, nodes, edges };
  });
  const toggleFlowAttribute = (nodeId: string, attributeId: string) => {
    setGraph((current) => ({ ...current, nodes: current.nodes.map((node) => {
      if (node.id !== nodeId) return node;
      const refs = node.flowAttributeRefs || [];
      return { ...node, flowAttributeRefs: refs.includes(attributeId) ? refs.filter((id) => id !== attributeId) : [...refs, attributeId] };
    }) }));
  };
  const toggleSection = (section: keyof typeof paletteSections) => setPaletteSections((current) => ({ ...current, [section]: !current[section] }));
  const moveMiniMap = (event: PointerEvent<HTMLButtonElement>) => {
    if (!miniMapDrag.current || !canvasRef.current) return;
    const bounds = canvasRef.current.getBoundingClientRect();
    setMiniMapPosition({
      x: Math.max(0, Math.min(bounds.width - miniMapSize.width - 2, miniMapDrag.current.left + event.clientX - miniMapDrag.current.x)),
      y: Math.max(0, Math.min(bounds.height - miniMapSize.height - 28, miniMapDrag.current.top + event.clientY - miniMapDrag.current.y)),
    });
  };
  const resizeMiniMap = (event: PointerEvent<HTMLButtonElement>) => {
    if (!miniMapResize.current || !canvasRef.current) return;
    const bounds = canvasRef.current.getBoundingClientRect();
    setMiniMapSize({
      width: Math.max(140, Math.min(bounds.width - miniMapPosition.x - 2, miniMapResize.current.width + event.clientX - miniMapResize.current.x)),
      height: Math.max(100, Math.min(bounds.height - miniMapPosition.y - 28, miniMapResize.current.height + event.clientY - miniMapResize.current.y)),
    });
  };
  const rearrange = () => {
    setGraph((current) => arrangedGraph(current));
    setPaneMenu(null);
    requestAnimationFrame(() => requestAnimationFrame(() => flow.fitView({ padding: 0.18, duration: 350 })));
  };
  const openCodeEditor = async () => {
    setCodeEditorOpen(true);
    setModuleName("");
    const starter = 'from gyrfalcon.flow import activity\n\n\n@activity(name="my_activity", version="1")\ndef my_activity(previous=None):\n    return {"message": "done"}\n';
    setModuleCode(starter);
    setModuleBaseline({ name: "", code: starter });
    setModuleError("");
    try {
      const result = await api.getActivityModules();
      setActivityModules(result.modules || []);
    } catch (error) { setModuleError(String(error)); }
  };
  const selectActivityModule = async (name: string) => {
    setModuleName(name);
    setModuleError("");
    if (!name) return;
    setModuleBusy(true);
    try {
      const result = await api.getActivityModule(name);
      setModuleCode(result.content);
      setModuleBaseline({ name, code: result.content });
    } catch (error) { setModuleError(String(error)); }
    finally { setModuleBusy(false); }
  };
  const openActivityModule = async (name: string) => {
    if (codeDirty && !window.confirm("Discard unsaved activity code and open this module?")) return;
    setModuleError("");
    setCodeEditorOpen(true);
    await selectActivityModule(name);
  };
  const saveActivityModule = async (): Promise<boolean> => {
    setModuleError("");
    setModuleBusy(true);
    try {
      const result = await api.saveActivityModule(moduleName.trim(), moduleCode);
      onActivitiesChange?.(result.activities || []);
      const modules = await api.getActivityModules();
      setActivityModules(modules.modules || []);
      setModuleBaseline({ name: moduleName.trim(), code: moduleCode });
      setCodeEditorOpen(false);
      onMessage(`${moduleName.trim()}.py saved. Activities refreshed.`);
      return true;
    } catch (error) { setModuleError(String(error)); return false; }
    finally { setModuleBusy(false); }
  };
  codeSaveRef.current = saveActivityModule;
  useEffect(() => { onCodeEditorDirtyChange?.(codeDirty, () => codeSaveRef.current()); }, [codeDirty, onCodeEditorDirtyChange]);
  useEffect(() => () => { onCodeEditorDirtyChange?.(false, async () => true); }, [onCodeEditorDirtyChange]);
  useEffect(() => {
    api.getAgents().then((result) => setAgentCatalog(result.agents || [])).catch(() => setAgentCatalog([]));
    api.getFlowNotificationTemplates().then((result) => setNotificationTemplates(result.templates || [])).catch(() => setNotificationTemplates([]));
    api.getActivityModules().then((result) => setActivityModules(result.modules || [])).catch(() => setActivityModules([]));
  }, []);

  if (inlineNode?.process?.mode === "inline") return <div className="flow-process-view" onContextMenu={(event) => event.preventDefault()}>
    <header className="flow-process-header"><button type="button" onClick={() => setInlineId(null)}><ArrowLeft size={16} /> Back to flow</button><span>{inlineNode.label || inlineNode.id}</span></header>
    <FlowCanvasEditor graph={inlineNode.process.graph} setGraph={(next) => {
      setGraph((current) => {
        const existing = current.nodes.find((node) => node.id === inlineNode.id);
        if (!existing || existing.process?.mode !== "inline") return current;
        const inner = typeof next === "function" ? next(existing.process.graph) : next;
        const outputs = endTransients(inner);
        return {
          ...current,
          nodes: current.nodes.map((node) => node.id === inlineNode.id ? { ...node, process: { mode: "inline", graph: inner }, transients: outputs } : node),
          edges: current.edges.map((edge) => edge.from === inlineNode.id && outputs.length && !outputs.includes(edge.transient || "") ? { ...edge, transient: outputs[0] } : edge),
        };
      });
    }} activities={activities} onActivitiesChange={onActivitiesChange} onCodeEditorDirtyChange={onCodeEditorDirtyChange} definitions={definitions} currentDefinitionId={currentDefinitionId} onOpenAgent={onOpenAgent} onMessage={onMessage} depth={depth + 1} />
  </div>;

  return <div className={`flow-designer-workspace ${paletteOpen ? "" : "palette-collapsed"} ${propertiesOpen ? "" : "properties-collapsed"}`} onContextMenu={(event) => event.preventDefault()}>
    <aside className="flow-palette" aria-label="Flow components">
      <div className="flow-palette-header"><h2>{paletteOpen ? "Components" : ""}</h2><button type="button" className="flow-palette-toggle" aria-label={paletteOpen ? "Collapse components panel" : "Expand components panel"} title={paletteOpen ? "Collapse panel" : "Expand panel"} onClick={() => setPaletteOpen(!paletteOpen)}>{paletteOpen ? <PanelLeftClose size={17} /> : <PanelLeftOpen size={17} />}</button></div>
      {paletteOpen && <>
        <button className="flow-palette-section" type="button" aria-expanded={paletteSections.nodes} onClick={() => toggleSection("nodes")}>{paletteSections.nodes ? <ChevronDown size={14} /> : <ChevronRight size={14} />} Nodes</button>
        {paletteSections.nodes && <div className="flow-palette-icons">{(["start", "python", "agent", "a2a", "notification", "process", "end"] as FlowNodeKind[]).map((kind) => <button key={kind} type="button" className="flow-palette-item" title={nodeKindLabel(kind)} aria-label={`Add ${nodeKindLabel(kind)}`} draggable onDragStart={(event) => event.dataTransfer.setData("application/gyrfalcon-node", kind)} onClick={() => makeNode(kind)}>{iconFor(kind)}</button>)}</div>}
        <div className="flow-palette-section-row"><button className="flow-palette-section" type="button" aria-expanded={paletteSections.catalog} onClick={() => toggleSection("catalog")}>{paletteSections.catalog ? <ChevronDown size={14} /> : <ChevronRight size={14} />} Activities</button><button type="button" className="flow-palette-code-action" aria-label="Add or edit activity code" title="Add or edit activity code" onClick={openCodeEditor}><Plus size={14} /></button></div>
        {paletteSections.catalog && <div className="flow-activity-tree">
          {activityModules.map((module) => {
            const methods = activities.filter((activity) => activity.module === module && activity.editable !== false);
            const expanded = expandedActivityModules[module] ?? true;
            return <div className="flow-activity-module" key={module}>
              <button type="button" className="flow-activity-module-heading" aria-expanded={expanded}
                onClick={() => setExpandedActivityModules((current) => ({ ...current, [module]: !expanded }))}
                onDoubleClick={() => { void openActivityModule(module); }}>{expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}<span>{module}.py</span></button>
              {expanded && <div className="flow-activity-methods">{methods.length ? methods.map((activity) => <button key={`${activity.name}@${activity.version}`} type="button" className="flow-activity-method" draggable title={activity.description || `${activity.name} version ${activity.version}`}
                onDragStart={(event) => { event.dataTransfer.setData("application/gyrfalcon-node", "python"); event.dataTransfer.setData("application/gyrfalcon-activity", `${activity.name}@${activity.version}`); }}
                onDoubleClick={() => { void openActivityModule(module); }}><span>{activity.name}</span><small>v{activity.version}</small>{activity.description && <em>{activity.description}</em>}</button>) : <span className="flow-activity-empty">No published methods</span>}</div>}
            </div>;
          })}
          {!activityModules.length && <span className="flow-activity-empty">No Python modules</span>}
        </div>}
        <button className="flow-palette-section" type="button" aria-expanded={paletteSections.attributes} onClick={() => { toggleSection("attributes"); setSelectedNodeId(null); setSelectedEdgeId(null); }}> {paletteSections.attributes ? <ChevronDown size={14} /> : <ChevronRight size={14} />} Flow attributes</button>
        {paletteSections.attributes && <div className="flow-palette-attribute-list" onClick={() => { setSelectedNodeId(null); setSelectedEdgeId(null); }}>{(graph.attributes || []).map((attribute) => <button key={attribute.id} type="button" title={`View ${attribute.name}`}>{attribute.name}</button>)}{!graph.attributes?.length && <span>No flow attributes</span>}</div>}
      </>}
    </aside>
    {codeEditorOpen && <div className="flow-code-overlay" role="dialog" aria-modal="true" aria-label="Activity code"><div className="flow-code-dialog"><header><strong>Activity code</strong><button type="button" aria-label="Close code editor" onClick={() => { if (!codeDirty || window.confirm("Discard unsaved activity code?")) setCodeEditorOpen(false); }}><X size={17} /></button></header><label>Existing module<select value={activityModules.includes(moduleName) ? moduleName : ""} onChange={(event) => selectActivityModule(event.target.value)}><option value="">New module</option>{activityModules.map((name) => <option key={name} value={name}>{name}.py</option>)}</select></label><label>Module name<input value={moduleName} onChange={(event) => setModuleName(event.target.value)} placeholder="orders" aria-label="Python module name" /></label><p className="flow-hint">One Python file can define multiple decorated activities. Save it to refresh the activity list.</p><textarea aria-label="Activity source code" spellCheck={false} value={moduleCode} onChange={(event) => setModuleCode(event.target.value)} />{moduleError && <p className="flow-code-error" role="alert">{moduleError}</p>}<footer><button type="button" onClick={() => { if (!codeDirty || window.confirm("Discard unsaved activity code?")) setCodeEditorOpen(false); }}>Cancel</button><button type="button" disabled={moduleBusy || !moduleName.trim()} onClick={saveActivityModule}>{moduleBusy ? "Saving…" : "Save activity code"}</button></footer></div></div>}
    <div className="flow-canvas-shell" ref={canvasRef} onContextMenu={(event) => event.preventDefault()} onDrop={onDrop} onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = "move"; }}>
      <div className="flow-canvas-help">Drag to pan · Scroll to zoom · Ctrl-click two nodes to link</div>
      {linkSource && <div className="flow-link-prompt">Linking from {linkSource}{linkTransient ? ` · ${linkTransient}` : ""}<button type="button" onClick={() => { setLinkSource(null); setLinkTransient(null); }}><X size={13} /></button></div>}
      <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} onNodesChange={onNodesChange} onNodeClick={onNodeClick} onConnect={onConnect}
        onConnectEnd={(event, state) => {
          if (state.isValid || !state.fromNode || state.fromHandle?.type !== "source") return;
          const point = "changedTouches" in event ? event.changedTouches[0] : event;
          const target = document.elementFromPoint(point.clientX, point.clientY)?.closest<HTMLElement>(".react-flow__node[data-id]");
          if (target?.dataset.id) addLink(state.fromNode.id, target.dataset.id);
        }}
        onNodeContextMenu={(event, node) => { event.preventDefault(); setContextMenu({ nodeId: node.id, x: event.clientX, y: event.clientY }); setEdgeMenu(null); setPaneMenu(null); setSelectedNodeId(node.id); setSelectedEdgeId(null); }}
        onNodeDoubleClick={(_, node) => {
          const selected = graph.nodes.find((item) => item.id === node.id);
          if (!selected) return;
          const isAgentActivity = selected.type === "agent" || (selected.type === "python" && selected.implementation === "agent");
          if (isAgentActivity) {
            if (selected.agent?.id) onOpenAgent?.(selected.agent.id);
            else onMessage("Select an agent in the properties panel before opening it.");
            return;
          }
          if (selected.type !== "process") return;
          if (selected.process?.mode === "inline") setInlineId(node.id);
          else onMessage("This Process references a saved flow. Select Inline flow to edit it here.");
        }}
        onEdgeClick={(_, edge) => { setSelectedEdgeId(edge.id); setSelectedNodeId(null); setContextMenu(null); setEdgeMenu(null); setPaneMenu(null); }}
        onEdgeContextMenu={(event, edge) => { event.preventDefault(); setEdgeMenu({ edgeId: edge.id, x: event.clientX, y: event.clientY }); setContextMenu(null); setPaneMenu(null); setSelectedEdgeId(edge.id); setSelectedNodeId(null); }}
        onReconnect={(oldEdge, connection) => patchEdge(oldEdge.id, { from: connection.source, to: connection.target })}
        onReconnectEnd={(event, edge, stationaryHandleType) => {
          const pointer = "changedTouches" in event ? event.changedTouches[0] : event;
          const droppedNode = document.elementFromPoint(pointer.clientX, pointer.clientY)?.closest<HTMLElement>(".react-flow__node[data-id]")?.dataset.id;
          if (!droppedNode) return;
          const reconnectSource = stationaryHandleType === "target";
          const sourceId = reconnectSource ? droppedNode : edge.source;
          const targetId = reconnectSource ? edge.target : droppedNode;
          const source = graph.nodes.find((item) => item.id === sourceId);
          const target = graph.nodes.find((item) => item.id === targetId);
          if (!source || !target || source.type === "end" || target.type === "start" || sourceId === targetId) return;
          const currentEdge = graph.edges.find((item) => item.id === edge.id);
          const choices = source.transients || defaultTransients(source.type);
          const availableChoices = choices.filter((choice) => !graph.edges.some((item) => item.id !== edge.id && item.from === sourceId && item.transient === choice));
          const transient = currentEdge?.transient && availableChoices.includes(currentEdge.transient)
            ? currentEdge.transient : availableChoices[0];
          if (!transient) { onMessage("The new source has no unlinked transient for this connection."); return; }
          patchEdge(edge.id, { from: sourceId, to: targetId, transient });
        }}
        onEdgesChange={(changes) => { const removed = changes.filter((change) => change.type === "remove").map((change) => change.id); if (removed.length) setGraph((current) => ({ ...current, edges: current.edges.filter((edge) => !removed.includes(edge.id)) })); }}
        onPaneClick={() => { setSelectedNodeId(null); setSelectedEdgeId(null); setContextMenu(null); setEdgeMenu(null); setPaneMenu(null); }}
        onPaneContextMenu={(event) => { event.preventDefault(); setPaneMenu({ x: event.clientX, y: event.clientY }); setContextMenu(null); setEdgeMenu(null); }}
        onMoveEnd={(event, viewport) => {
          // React Flow calls this after its automatic initial fit. That is not
          // an edit, but later user pan/zoom changes need to mark the draft dirty.
          if (!event && !graph.viewport) return;
          setGraph((current) => current.viewport?.x === viewport.x && current.viewport?.y === viewport.y && current.viewport?.zoom === viewport.zoom
            ? current : { ...current, viewport });
        }}
        defaultViewport={graph.viewport || { x: 0, y: 0, zoom: 1 }} fitView={!graph.viewport} minZoom={0.2} maxZoom={2.5} panOnDrag zoomOnScroll selectionOnDrag={false} deleteKeyCode={null}>
        <Background gap={20} size={1} />{miniMapMode === "open" && <MiniMap pannable zoomable style={{ left: miniMapPosition.x, top: miniMapPosition.y + 26, right: "auto", bottom: "auto", margin: 0, width: miniMapSize.width, height: miniMapSize.height }} />}<Controls />
      </ReactFlow>
      {miniMapMode === "open" ? <><div className="flow-aerial-toolbar nodrag nopan" style={{ left: miniMapPosition.x, top: miniMapPosition.y, width: miniMapSize.width }}>
        <button type="button" className="flow-aerial-drag" aria-label="Drag aerial view" title="Drag aerial view" onPointerDown={(event) => { event.stopPropagation(); miniMapDrag.current = { x: event.clientX, y: event.clientY, left: miniMapPosition.x, top: miniMapPosition.y }; event.currentTarget.setPointerCapture(event.pointerId); }} onPointerMove={moveMiniMap} onPointerUp={(event) => { event.stopPropagation(); miniMapDrag.current = null; }} onPointerCancel={() => { miniMapDrag.current = null; }}><Grip size={14} /></button>
        <button type="button" aria-label="Minimize aerial view" title="Minimize aerial view" onClick={() => setMiniMapMode("minimized")}><Minimize2 size={13} /></button>
        <button type="button" aria-label="Close aerial view" title="Close aerial view" onClick={() => setMiniMapMode("closed")}><X size={13} /></button>
      </div><button type="button" className="flow-aerial-resize nodrag nopan" style={{ left: miniMapPosition.x + miniMapSize.width - 15, top: miniMapPosition.y + miniMapSize.height + 11 }} aria-label="Resize aerial view" title="Resize aerial view" onPointerDown={(event) => { event.stopPropagation(); miniMapResize.current = { x: event.clientX, y: event.clientY, width: miniMapSize.width, height: miniMapSize.height }; event.currentTarget.setPointerCapture(event.pointerId); }} onPointerMove={resizeMiniMap} onPointerUp={(event) => { event.stopPropagation(); miniMapResize.current = null; }} onPointerCancel={() => { miniMapResize.current = null; }}><Grip size={11} /></button></>
        : miniMapMode === "minimized" ? <button type="button" className="flow-aerial-expand nodrag nopan" style={{ left: miniMapPosition.x, top: miniMapPosition.y }} aria-label="Show aerial view" title="Show aerial view" onClick={() => setMiniMapMode("open")}><Maximize2 size={15} /></button>
        : <button type="button" className="flow-aerial-reopen nodrag nopan" aria-label="Open aerial view" title="Open aerial view" onClick={() => setMiniMapMode("open")}><MapIcon size={16} /></button>}
      {contextMenu && <div className="flow-node-context" style={{ left: contextMenu.x, top: contextMenu.y }} data-flow-menu>
        <strong>Link with transient</strong>
        {(graph.nodes.find((node) => node.id === contextMenu.nodeId)?.transients || []).map((value) => <button key={value} type="button" onClick={() => { setLinkSource(contextMenu.nodeId); setLinkTransient(value); setContextMenu(null); }}>{value}</button>)}
        <strong>Flow attributes</strong>
        {(graph.attributes || []).length ? (graph.attributes || []).map((attribute) => <button key={attribute.id} type="button" role="menuitemcheckbox" aria-checked={!!graph.nodes.find((node) => node.id === contextMenu.nodeId)?.flowAttributeRefs?.includes(attribute.id)} onClick={() => toggleFlowAttribute(contextMenu.nodeId, attribute.id)}>{graph.nodes.find((node) => node.id === contextMenu.nodeId)?.flowAttributeRefs?.includes(attribute.id) ? "✓ " : "+ "}{attribute.name}</button>) : <span className="flow-context-empty">No flow attributes</span>}
        <button type="button" className="flow-context-delete" onClick={() => removeNode(contextMenu.nodeId)}><Trash2 size={13} /> Delete node</button>
        <button type="button" onClick={() => setContextMenu(null)}>Close</button>
      </div>}
      {edgeMenu && menuEdge && <div className="flow-node-context" style={{ left: edgeMenu.x, top: edgeMenu.y }} data-flow-menu>
        <strong>Transient value</strong>
        {(menuSource?.transients || []).map((value) => <button key={value} type="button" disabled={graph.edges.some((edge) => edge.id !== menuEdge.id && edge.from === menuEdge.from && edge.transient === value)} onClick={() => { patchEdge(menuEdge.id, { transient: value }); setEdgeMenu(null); }}>{value === menuEdge.transient ? "✓ " : ""}{value}</button>)}
        <button type="button" className="flow-context-delete" onClick={() => { removeLink(menuEdge.id); setEdgeMenu(null); }}><Trash2 size={13} /> Delete link</button>
      </div>}
      {paneMenu && <div className="flow-node-context" style={{ left: paneMenu.x, top: paneMenu.y }} data-flow-menu><button type="button" onClick={rearrange}>Rearrange items</button></div>}
    </div>
    <aside className={`flow-properties ${propertiesOpen ? "" : "is-collapsed"}`} aria-label="Properties">
      <div className="flow-properties-header">
        {propertiesOpen && <h2>{selectedNode ? `${nodeKindLabel(selectedNode.type)} settings` : selectedEdge ? "Transient link" : "Flow settings"}</h2>}
        <button type="button" className="flow-properties-toggle" aria-label={propertiesOpen ? "Collapse properties panel" : "Expand properties panel"} title={propertiesOpen ? "Collapse properties panel" : "Expand properties panel"} onClick={() => setPropertiesOpen(!propertiesOpen)}>{propertiesOpen ? <PanelRightClose size={17} /> : <PanelRightOpen size={17} />}</button>
      </div>
      {propertiesOpen && <>
      {selectedNode ? <>
        <label>Node ID<input value={selectedNode.id} onChange={(event) => changeNodeId(selectedNode.id, event.target.value)} /></label>
        <label>Label<input value={selectedNode.label || ""} onChange={(event) => patchNode(selectedNode.id, { label: event.target.value })} /></label>
        {!!selectedNode.flowAttributeRefs?.length && <div className="flow-transient-list"><strong>Flow attributes</strong>{selectedNode.flowAttributeRefs.map((id) => { const attribute = graph.attributes?.find((item) => item.id === id); return <div key={id}><span>{attribute?.name || id}</span><button type="button" title="Remove flow attribute" aria-label={`Remove ${attribute?.name || id}`} onClick={() => toggleFlowAttribute(selectedNode.id, id)}><X size={13} /></button></div>; })}</div>}
        {["python", "agent", "a2a"].includes(selectedNode.type) && <>
          {selectedNode.type === "python" && <label>Implementation<select value={selectedNode.implementation || "python"} onChange={(event) => {
            const implementation = event.target.value as "python" | "agent" | "a2a";
            patchNode(selectedNode.id, { implementation, ...(implementation === "python" ? {} : { agent: { id: selectedNode.agent?.id || "", version: selectedNode.agent?.version || "", kind: implementation } }) });
          }}><option value="python">Python</option><option value="agent">Agent</option><option value="a2a">A2A</option></select></label>}
          {(selectedNode.type === "python" ? selectedNode.implementation || "python" : selectedNode.type) === "python" ? <>
            <label>Activity<select value={`${selectedNode.activity}@${selectedNode.version}`} onChange={(event) => { const item = activities.find((activity) => `${activity.name}@${activity.version}` === event.target.value); if (item) patchNode(selectedNode.id, { activity: item.name, version: item.version, implementation: "python", annotation: item.description || "" }); }}>{activities.map((item) => <option key={`${item.name}@${item.version}`} value={`${item.name}@${item.version}`}>{item.name} · v{item.version}</option>)}</select></label>
            <label>Annotation<textarea value={selectedNode.annotation || ""} onChange={(event) => patchNode(selectedNode.id, { annotation: event.target.value })} placeholder="Description from the Python method" /></label>
            <label>Inputs (JSON)<textarea key={selectedNode.id} defaultValue={JSON.stringify(selectedNode.inputs || {}, null, 2)} onBlur={(event) => { try { const inputs = JSON.parse(event.target.value); if (!inputs || Array.isArray(inputs) || typeof inputs !== "object") throw new Error("Inputs must be a JSON object."); patchNode(selectedNode.id, { inputs }); } catch (error) { onMessage(String(error)); } }} /></label>
          </> : <>
            {(selectedNode.implementation === "a2a" || selectedNode.type === "a2a") ? <>
              <label>A2A agent ID<input value={selectedNode.agent?.id || ""} onChange={(event) => patchNode(selectedNode.id, { agent: { ...selectedNode.agent, id: event.target.value, version: selectedNode.agent?.version || "", kind: "a2a" } })} /></label>
              <label>A2A pinned version<input value={selectedNode.agent?.version || ""} onChange={(event) => patchNode(selectedNode.id, { agent: { ...selectedNode.agent, id: selectedNode.agent?.id || "", version: event.target.value, kind: "a2a" } })} /></label>
              <label>A2A endpoint URL<input value={selectedNode.agent?.url || ""} onChange={(event) => patchNode(selectedNode.id, { agent: { ...selectedNode.agent, id: selectedNode.agent?.id || "", version: selectedNode.agent?.version || "", kind: "a2a", url: event.target.value } })} /></label>
              <label>Agent Card URL<input value={selectedNode.agent?.cardUrl || ""} onChange={(event) => patchNode(selectedNode.id, { agent: { ...selectedNode.agent, id: selectedNode.agent?.id || "", version: selectedNode.agent?.version || "", kind: "a2a", cardUrl: event.target.value } })} /></label>
              <label>Bearer secret name<input value={selectedNode.agent?.bearerSecret || ""} onChange={(event) => patchNode(selectedNode.id, { agent: { ...selectedNode.agent, id: selectedNode.agent?.id || "", version: selectedNode.agent?.version || "", kind: "a2a", bearerSecret: event.target.value } })} /></label>
              <label>Context mode<select value={selectedNode.agent?.contextMode || "full"} onChange={(event) => patchNode(selectedNode.id, { agent: { ...selectedNode.agent, id: selectedNode.agent?.id || "", version: selectedNode.agent?.version || "", kind: "a2a", contextMode: event.target.value as "full" | "prior" } })}><option value="full">Full flow context</option><option value="prior">Prior node output</option></select></label>
              {selectedNode.agent?.contextMode === "prior" && <label>Prior node<select value={selectedNode.agent?.priorNodeId || ""} onChange={(event) => patchNode(selectedNode.id, { agent: { ...selectedNode.agent!, priorNodeId: event.target.value } })}><option value="">Choose a prior node</option>{graph.nodes.filter((node) => node.id !== selectedNode.id).map((node) => <option key={node.id} value={node.id}>{node.label || node.id}</option>)}</select></label>}
              <label>Human recipient kind<select value={selectedNode.agent?.humanRecipientKind || "user"} onChange={(event) => patchNode(selectedNode.id, { agent: { ...selectedNode.agent, id: selectedNode.agent?.id || "", version: selectedNode.agent?.version || "", kind: "a2a", humanRecipientKind: event.target.value as "user" | "group" | "role" } })}><option value="user">User</option><option value="group">Group</option><option value="role">Role</option></select></label>
              <label>Human recipient reference<input value={selectedNode.agent?.humanRecipientRef || ""} onChange={(event) => patchNode(selectedNode.id, { agent: { ...selectedNode.agent, id: selectedNode.agent?.id || "", version: selectedNode.agent?.version || "", kind: "a2a", humanRecipientRef: event.target.value } })} /></label>
            </> : <>
              <label>Gyrfalcon agent<select value={selectedNode.agent?.id || ""} onChange={(event) => { const item = agentCatalog.find((candidate) => candidate.id === event.target.value); patchNode(selectedNode.id, { agent: { ...selectedNode.agent, id: event.target.value, version: item?.flow_version || "", kind: "agent" } }); }}><option value="">Choose an agent</option>{selectedNode.agent?.id && !agentCatalog.some((item) => item.id === selectedNode.agent?.id) && <option value={selectedNode.agent.id}>{selectedNode.agent.id} (unavailable)</option>}{agentCatalog.filter((item) => item.enabled !== false).map((item) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</select></label>
              <label>Pinned agent config version<input value={selectedNode.agent?.version || ""} onChange={(event) => patchNode(selectedNode.id, { agent: { ...selectedNode.agent, id: selectedNode.agent?.id || "", version: event.target.value, kind: "agent" } })} /></label>
              <label>Context mode<select value={selectedNode.agent?.contextMode || "full"} onChange={(event) => patchNode(selectedNode.id, { agent: { ...selectedNode.agent, id: selectedNode.agent?.id || "", version: selectedNode.agent?.version || "", kind: "agent", contextMode: event.target.value as "full" | "prior" } })}><option value="full">Full flow context</option><option value="prior">Prior node output</option></select></label>
              {selectedNode.agent?.contextMode === "prior" && <label>Prior node<select value={selectedNode.agent?.priorNodeId || ""} onChange={(event) => patchNode(selectedNode.id, { agent: { ...selectedNode.agent!, priorNodeId: event.target.value } })}><option value="">Choose a prior node</option>{graph.nodes.filter((node) => node.id !== selectedNode.id).map((node) => <option key={node.id} value={node.id}>{node.label || node.id}</option>)}</select></label>}
              <label>Activity instructions<textarea value={selectedNode.instructions || ""} onChange={(event) => patchNode(selectedNode.id, { instructions: event.target.value })} /></label>
              <label>Human recipient kind<select value={selectedNode.agent?.humanRecipientKind || "user"} onChange={(event) => patchNode(selectedNode.id, { agent: { ...selectedNode.agent, id: selectedNode.agent?.id || "", version: selectedNode.agent?.version || "", kind: "agent", humanRecipientKind: event.target.value as "user" | "group" | "role" } })}><option value="user">User</option><option value="group">Group</option><option value="role">Role</option></select></label>
              <label>Human recipient reference<input value={selectedNode.agent?.humanRecipientRef || ""} onChange={(event) => patchNode(selectedNode.id, { agent: { ...selectedNode.agent, id: selectedNode.agent?.id || "", version: selectedNode.agent?.version || "", kind: "agent", humanRecipientRef: event.target.value } })} /></label>
            </>}
          </>}
        </>}
        {selectedNode.type === "notification" && <>
          <label>Notification mode<select value={selectedNode.notification?.mode || "send"} onChange={(event) => patchNode(selectedNode.id, { notification: { ...selectedNode.notification, mode: event.target.value as "send" | "wait", templateId: selectedNode.notification?.templateId || "" } })}><option value="send">Send</option><option value="wait">Wait for response</option></select></label>
          <label>Notification template<select value={selectedNode.notification?.templateId || ""} onChange={(event) => patchNode(selectedNode.id, { notification: { ...selectedNode.notification, mode: selectedNode.notification?.mode || "send", templateId: event.target.value } })}><option value="">Choose a template</option>{selectedNode.notification?.templateId && !notificationTemplates.some((item) => item.id === selectedNode.notification?.templateId) && <option value={selectedNode.notification.templateId}>{selectedNode.notification.templateId} (unavailable)</option>}{notificationTemplates.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
          {selectedNode.notification?.mode === "wait" && <>
            <label>Wait timeout (seconds)<input type="number" min="1" value={selectedNode.notification.timeoutSeconds || ""} onChange={(event) => patchNode(selectedNode.id, { notification: { ...selectedNode.notification!, timeoutSeconds: Number(event.target.value) } })} /></label>
            <label>Timeout transient<select value={selectedNode.notification.timeoutTransient || ""} onChange={(event) => patchNode(selectedNode.id, { notification: { ...selectedNode.notification!, timeoutTransient: event.target.value } })}><option value="">Choose transient</option>{(selectedNode.transients || []).map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
          </>}
        </>}
        {selectedNode.type === "process" && <>
          <label>Process source<select value={selectedNode.process?.mode || "inline"} onChange={(event) => {
            const definition = definitions.find((item) => item.id !== currentDefinitionId);
            if (event.target.value === "reference") setProcessSource(selectedNode.id, { mode: "reference", definitionId: definition?.id || "", version: definition?.published_version || undefined }, definition ? endTransients(definition.draft) : []);
            else setProcessSource(selectedNode.id, { mode: "inline", graph: { version: 2, nodes: [], edges: [], attributes: [] } }, []);
          }}><option value="inline">Inline flow</option><option value="reference">Saved flow</option></select></label>
          {selectedNode.process?.mode === "reference" ? <label>Flow<select value={selectedNode.process.definitionId ? `${selectedNode.process.definitionId}@${selectedNode.process.version || ""}` : ""} onChange={(event) => {
            const [definitionId, versionText] = event.target.value.split("@");
            const definition = definitions.find((item) => item.id === definitionId);
            setProcessSource(selectedNode.id, { mode: "reference", definitionId, version: Number(versionText) || undefined }, definition ? endTransients(definition.draft) : []);
          }}><option value="">Choose a published flow</option>{definitions.filter((item) => item.id !== currentDefinitionId && item.published_version).map((item) => <option key={item.id} value={`${item.id}@${item.published_version}`}>{item.name} · v{item.published_version}</option>)}</select></label> : <button className="flow-small-action" type="button" onClick={() => setInlineId(selectedNode.id)}>Edit inner flow</button>}
        </>}
        {selectedNode.type !== "process" ? <div className="flow-transient-list"><strong>Allowed transients</strong>
          <label>Attribute<select value={transientAttributeSelection} onChange={(event) => {
            if (!event.target.value) { setTransientAttribute(selectedNode.id); return; }
            const [scope, id] = event.target.value.split(":", 2);
            if ((scope === "flow" || scope === "node") && id) setTransientAttribute(selectedNode.id, { scope, id });
          }}><option value="">Choose an attribute</option>{transientAttributeOptions.map(({ scope, attribute, label }) => <option key={`${scope}:${attribute.id}`} value={`${scope}:${attribute.id}`}>{label}</option>)}</select></label>
          <p className="flow-hint">{(selectedNode.transients || []).length ? `Allowed values: ${selectedNode.transients!.join(", ")}` : "Choose a text attribute with candidate values."}</p>
        </div> : <div className="flow-transient-list"><strong>Allowed transients</strong><p className="flow-hint">Process outputs come from its inner End activities.</p></div>}
        {graph.edges.some((edge) => edge.from === selectedNode.id) && <div className="flow-transient-list"><strong>Outgoing links</strong>{graph.edges.filter((edge) => edge.from === selectedNode.id).map((edge) => <div key={edge.id}><button type="button" className="flow-link-select" onClick={() => { setSelectedNodeId(null); setSelectedEdgeId(edge.id); }}>{edge.transient || "next"} → {graph.nodes.find((node) => node.id === edge.to)?.label || edge.to}</button><button type="button" aria-label={`Delete ${edge.transient || "next"} link`} title="Delete link" onClick={() => removeLink(edge.id)}><Trash2 size={13} /></button></div>)}</div>}
        {hasLoop && <div className="flow-loop-controls"><strong>Loop limits</strong><p className="flow-hint">Set either limit as a node attribute. A limit routes to its own outgoing transient at the next loop decision.</p>
          {(["max_loop", "max_timeout"] as const).map((name) => {
            const attribute = selectedNode.attributes?.find((item) => item.name === name);
            const key = name === "max_loop" ? "maxCountTransient" : "timeoutTransient";
            return <div key={name}>{attribute ? <label>{name === "max_loop" ? "Count limit output" : "Timeout output"}<select value={selectedNode.loopControl?.[key] || ""} onChange={(event) => patchNode(selectedNode.id, { loopControl: { ...selectedNode.loopControl, [key]: event.target.value } })}><option value="">Choose transient</option>{limitTransients.map((value) => <option key={value} value={value}>{value}</option>)}</select></label> : <button className="flow-small-action" type="button" onClick={() => patchNode(selectedNode.id, { attributes: [...(selectedNode.attributes || []), { id: uid("attr"), name, type: "number", defaultValue: name === "max_loop" ? 10 : 86400, candidates: [] }] })}>Add {name === "max_loop" ? "count" : "timeout"} limit</button>}</div>;
          })}
        </div>}
      </> : selectedEdge ? <>
        <label>Connection type<select value={selectedEdge.routing || graph.edgeRouting || "bezier"} onChange={(event) => patchEdge(selectedEdge.id, { routing: event.target.value as FlowEdge["routing"] })}><option value="bezier">Bezier</option><option value="straight">Straight</option><option value="step">Step</option><option value="smoothstep">Smooth step</option></select></label>
        <label>Source<select value={selectedEdge.from} onChange={(event) => patchEdge(selectedEdge.id, { from: event.target.value, transient: graph.nodes.find((node) => node.id === event.target.value)?.transients?.[0] || "next" })}>{graph.nodes.filter((node) => node.type !== "end").map((node) => <option key={node.id} value={node.id}>{node.label || node.id}</option>)}</select></label>
        <label>Target<select value={selectedEdge.to} onChange={(event) => patchEdge(selectedEdge.id, { to: event.target.value })}>{graph.nodes.filter((node) => node.id !== selectedEdge.from && node.type !== "start").map((node) => <option key={node.id} value={node.id}>{node.label || node.id}</option>)}</select></label>
        <label>Transient value<select value={selectedEdge.transient || ""} onChange={(event) => patchEdge(selectedEdge.id, { transient: event.target.value })}>{(sourceNode?.transients || []).map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
        <label className="flow-check"><input type="checkbox" checked={!!selectedEdge.loop} onChange={(event) => patchEdge(selectedEdge.id, { loop: event.target.checked })} /> Loop back link</label>
        <p className="flow-hint">The link annotation is its selected transient value. Ctrl-click a source and target to add another link.</p>
        <button type="button" className="flow-delete-action" onClick={() => removeLink(selectedEdge.id)}><Trash2 size={14} /> Delete link</button>
      </> : <p className="flow-hint">Select a node or link to edit its properties.</p>}
      {!selectedEdge && <AttributeEditor key={selectedNode?.id || "flow"} attributes={attributes} onChange={setAttributes} onMessage={onMessage} />}
      </>}
    </aside>
  </div>;
}

export function FlowCanvasEditor(props: Props) {
  return <ReactFlowProvider><EditorBody {...props} /></ReactFlowProvider>;
}
