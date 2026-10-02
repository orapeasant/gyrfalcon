export type FlowNodeKind = "start" | "python" | "agent" | "a2a" | "notification" | "process" | "end";

export type FlowAttribute = {
  id: string;
  name: string;
  type: string;
  defaultValue: unknown;
  candidates: unknown[];
};

export type FlowGraph = {
  version?: number;
  edgeRouting?: FlowEdge["routing"];
  nodes: FlowNode[];
  edges: FlowEdge[];
  attributes?: FlowAttribute[];
  viewport?: { x: number; y: number; zoom: number };
};

export type FlowNode = {
  id: string;
  type: FlowNodeKind;
  label?: string;
  annotation?: string;
  position?: { x: number; y: number };
  width?: number;
  height?: number;
  transients?: string[];
  transientAttribute?: { scope: "flow" | "node"; id: string };
  attributes?: FlowAttribute[];
  flowAttributeRefs?: string[];
  activity?: string;
  version?: string;
  implementation?: "python" | "agent" | "a2a";
  agent?: { id: string; version: string; kind: "agent" | "a2a"; contextMode?: "full" | "prior"; priorNodeId?: string; url?: string; cardUrl?: string; bearerSecret?: string; humanRecipientKind?: "user" | "group" | "role"; humanRecipientRef?: string };
  instructions?: string;
  inputs?: Record<string, unknown>;
  notification?: { mode: "send" | "wait"; templateId: string; timeoutSeconds?: number; timeoutTransient?: string };
  loopControl?: { maxCountTransient?: string; timeoutTransient?: string };
  process?: { mode: "inline"; graph: FlowGraph } | { mode: "reference"; definitionId: string; version?: number };
};

export type FlowEdge = {
  id: string;
  from: string;
  to: string;
  transient?: string;
  routing?: "bezier" | "straight" | "step" | "smoothstep";
  labelPosition?: { x: number; y: number };
  loop?: boolean;
  when?: { path: string; op: string; value: unknown };
};

export type GraphDefinition = {
  id: string;
  name: string;
  enabled?: boolean;
  draft: FlowGraph;
  published_version: number | null;
  created_at: number;
  updated_at: number;
};

export type Activity = { name: string; version: string; module?: string | null; description?: string; editable?: boolean };

export function canvasDraftIssue(graph: FlowGraph): string | null {
  if (graph.version !== 2) return null;
  if (graph.nodes.filter((node) => node.type === "start").length !== 1) return "A flow needs exactly one Start activity.";
  if (!graph.nodes.some((node) => node.type === "end")) return "A flow needs at least one End activity.";
  const ids = new Set(graph.nodes.map((node) => node.id));
  if (ids.size !== graph.nodes.length) return "Node IDs must be unique.";
  for (const node of graph.nodes) {
    if (node.type !== "end" && !graph.edges.some((edge) => edge.from === node.id)) return `${node.label || node.id} needs an outgoing link.`;
    if (node.type === "end" && graph.edges.some((edge) => edge.from === node.id)) return "End activities cannot have outgoing links.";
    const attrs = node.attributes || [];
    if (node.flowAttributeRefs?.some((id) => !(graph.attributes || []).some((attribute) => attribute.id === id))) return `${node.label || node.id} references a missing flow attribute.`;
    if (new Set(attrs.map((attr) => attr.name)).size !== attrs.length) return `${node.label || node.id} has duplicate attribute names.`;
    for (const attr of attrs) {
      if (!attr.name.trim()) return `${node.label || node.id} has an unnamed attribute.`;
      if (attr.candidates.length && !attr.candidates.some((value) => JSON.stringify(value) === JSON.stringify(attr.defaultValue))) return `${attr.name}'s default must be one of its allowed values.`;
    }
    if (node.type === "process" && node.process?.mode === "reference" && !node.process.definitionId) return `${node.label || node.id} needs a saved flow.`;
    if (node.type === "process" && node.process?.mode === "inline") {
      const innerIssue = canvasDraftIssue(node.process.graph);
      if (innerIssue) return `${node.label || node.id}: ${innerIssue}`;
    }
    if (graph.edges.some((edge) => edge.from === node.id && edge.loop)) {
      const names = new Set(attrs.map((attr) => attr.name));
      if (!names.has("max_loop") && !names.has("max_timeout")) return `${node.label || node.id} needs max_loop or max_timeout for its loop.`;
      if (names.has("max_loop") && (!node.loopControl?.maxCountTransient || !graph.edges.some((edge) => edge.from === node.id && !edge.loop && edge.transient === node.loopControl?.maxCountTransient))) return `${node.label || node.id} needs a count-limit output link.`;
      if (names.has("max_timeout") && (!node.loopControl?.timeoutTransient || !graph.edges.some((edge) => edge.from === node.id && !edge.loop && edge.transient === node.loopControl?.timeoutTransient))) return `${node.label || node.id} needs a timeout output link.`;
    }
  }
  for (const edge of graph.edges) {
    if (!ids.has(edge.from) || !ids.has(edge.to)) return "A link points to a missing activity.";
    const source = graph.nodes.find((node) => node.id === edge.from)!;
    const target = graph.nodes.find((node) => node.id === edge.to)!;
    if (source.type === "end" || target.type === "start") return "A link cannot leave End or enter Start.";
    if (edge.transient && !source.transients?.includes(edge.transient)) return `Link ${edge.from} → ${edge.to} uses an undeclared transient.`;
    if (graph.edges.some((other) => other.id !== edge.id && other.from === edge.from && other.transient === edge.transient)) return `${source.label || source.id} has two links for the same transient.`;
  }
  const flowAttrs = graph.attributes || [];
  if (new Set(flowAttrs.map((attr) => attr.name)).size !== flowAttrs.length) return "Flow has duplicate attribute names.";
  return null;
}
