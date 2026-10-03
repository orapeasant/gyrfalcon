import React, { useCallback, useEffect, useState } from "react";
import {
  ChevronDown, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight,
  History, RefreshCw, RotateCcw, Search, Trash2, FlaskConical,
} from "lucide-react";
import { api } from "../lib/api";
import { FlowRunGraph, type FlowVisit } from "./FlowRunGraph";
import type { FlowGraph } from "./flowDesignerTypes";

type Run = {
  id: string; definition_id: string; flow_name?: string; version: number; state: string;
  trigger_kind: string; trigger_ref?: string; created_at: number; started_at?: number;
  finished_at?: number; current_node_path?: string; inventory_org?: string;
  business_unit?: string; organization_name?: string; user_name?: string; user_email?: string;
  tenant_id: string; user_id: string; error?: unknown; result?: unknown;
};
type Visit = FlowVisit & {
  id: string; node_id: string; node_path: string; node_kind: string; state: string;
  transient?: string; error?: unknown; started_at?: number; finished_at?: number; attempt: number;
};
type RunDetail = Run & { nodes: Visit[]; graph?: FlowGraph };

const PAGE_SIZES = [20, 50, 100];
const fmtDate = (ts?: number) => ts ? new Date(ts * 1000).toLocaleString() : "—";
const label = (value?: string) => value ? value.replaceAll("_", " ") : "—";
const terminal = (state: string) => ["completed", "failed", "cancelled", "crashed"].includes(state);
const failed = (state: string) => ["failed", "crashed"].includes(state);

const S = {
  page: { padding: 0, maxWidth: "1100px" } as React.CSSProperties,
  head: { display: "flex", alignItems: "center", gap: "10px", marginBottom: "14px", flexWrap: "wrap" as const },
  h1: { fontSize: "16px", fontWeight: 700, margin: 0 } as React.CSSProperties,
  count: { fontSize: "12px", color: "var(--fg-muted)" } as React.CSSProperties,
  tools: { display: "flex", gap: "7px", alignItems: "center", marginLeft: "auto" } as React.CSSProperties,
  searchWrap: { position: "relative" as const, marginBottom: "11px" },
  search: { width: "100%", boxSizing: "border-box" as const, background: "var(--input-bg)", border: "1px solid var(--border)", borderRadius: "6px", padding: "7px 10px 7px 31px", color: "var(--fg)", fontSize: "13px" },
  select: { background: "var(--input-bg)", border: "1px solid var(--border)", borderRadius: "6px", padding: "6px 9px", color: "var(--fg)", fontSize: "12px" },
  table: { border: "1px solid var(--border)", borderRadius: "8px", overflow: "hidden" } as React.CSSProperties,
  th: { textAlign: "left" as const, padding: "9px 12px", fontSize: "10.5px", fontWeight: 700, textTransform: "uppercase" as const, letterSpacing: "0.06em", color: "var(--fg-muted)", borderBottom: "1px solid var(--border)", background: "var(--card)", whiteSpace: "nowrap" as const },
  td: { padding: "9px 12px", verticalAlign: "middle" as const, fontSize: "12.5px", color: "var(--fg)" },
  iconBtn: (danger = false, disabled = false): React.CSSProperties => ({ display: "inline-flex", alignItems: "center", justifyContent: "center", gap: "5px", background: "transparent", border: "1px solid var(--border)", borderRadius: "5px", padding: "5px 8px", cursor: disabled ? "default" : "pointer", color: danger ? "var(--red)" : "var(--fg-muted)", opacity: disabled ? 0.45 : 1, fontSize: "11.5px" }),
  actionBar: { display: "flex", alignItems: "center", gap: "8px", padding: "8px 10px", background: "var(--sidebar-active)", borderBottom: "1px solid var(--border)", fontSize: "12px" } as React.CSSProperties,
  empty: { padding: "40px", textAlign: "center" as const, color: "var(--fg-muted)", border: "1px dashed var(--border)", borderRadius: "8px" } as React.CSSProperties,
  detail: { background: "var(--card)", borderTop: "1px solid var(--border)", padding: "14px 20px" } as React.CSSProperties,
  btn: (active: boolean, disabled: boolean): React.CSSProperties => ({ ...S.iconBtn(false, disabled), background: active ? "var(--btn-bg)" : "transparent", color: active ? "var(--btn-fg)" : "var(--fg-muted)" }),
};

const STATE_COLORS: Record<string, string> = {
  queued: "var(--purple)", running: "var(--blue)", waiting: "var(--warning)",
  completed: "var(--green)", failed: "var(--red)", crashed: "var(--red)", cancelled: "var(--fg-muted)",
};

export function FlowInstancesPage() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [total, setTotal] = useState(0);
  const [query, setQuery] = useState("");
  const [state, setState] = useState("");
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [details, setDetails] = useState<Record<string, RunDetail | "loading" | string>>({});
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const [message, setMessage] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.listVisualRuns({ limit: pageSize, offset: page * pageSize, state: state || undefined, q: query.trim() || undefined });
      setRuns(data.runs || []);
      setTotal(data.total || 0);
      setSelected(new Set());
    } catch (error: any) {
      setRuns([]);
      setMessage(error.message || "Could not load flow instances");
    } finally { setLoading(false); }
  }, [page, pageSize, query, state]);

  useEffect(() => { const id = setTimeout(load, 250); return () => clearTimeout(id); }, [load]);
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const allSelected = runs.length > 0 && runs.every((run) => selected.has(run.id));
  const expandedDetail = expanded ? details[expanded] : undefined;
  const expandedState = expandedDetail && typeof expandedDetail === "object" ? expandedDetail.state : "";

  useEffect(() => {
    if (!expanded || !expandedState || terminal(expandedState)) return;
    const timer = window.setInterval(() => {
      api.getVisualRun(expanded).then((data) => {
        setDetails((old) => ({ ...old, [expanded]: data }));
        setRuns((old) => old.map((run) => run.id === expanded
          ? { ...run, state: data.state, current_node_path: data.current_node_path,
              error: data.error, result: data.result, finished_at: data.finished_at }
          : run));
      }).catch(() => undefined);
    }, 1800);
    return () => window.clearInterval(timer);
  }, [expanded, expandedState]);

  async function toggleDetails(run: Run) {
    if (expanded === run.id) { setExpanded(null); return; }
    setExpanded(run.id);
    if (details[run.id]) return;
    setDetails((old) => ({ ...old, [run.id]: "loading" }));
    try {
      const data = await api.getVisualRun(run.id);
      setDetails((old) => ({ ...old, [run.id]: data }));
    } catch (error: any) {
      setDetails((old) => ({ ...old, [run.id]: error.message || "Could not load run trajectory" }));
    }
  }

  function markBusy(id: string, active: boolean) {
    setBusy((old) => { const next = new Set(old); active ? next.add(id) : next.delete(id); return next; });
  }

  async function retry(run: Run) {
    markBusy(run.id, true); setMessage("");
    try { await api.retryVisualRun(run.id); setMessage(`Retry queued for ${run.flow_name || "flow"} at the failed activity.`); await load(); }
    catch (error: any) { setMessage(error.message || "Could not retry flow instance"); }
    finally { markBusy(run.id, false); }
  }

  async function runDemo() {
    setMessage("");
    try {
      const result = await api.runComplexSampleFlow();
      setQuery(""); setState(""); setPage(0);
      setExpanded(result.run_id);
      const detail = await api.getVisualRun(result.run_id);
      setDetails((old) => ({ ...old, [result.run_id]: detail }));
      const pageData = await api.listVisualRuns({ limit: pageSize, offset: 0 });
      setRuns(pageData.runs || []); setTotal(pageData.total || 0); setSelected(new Set());
      setMessage(`Published v${result.version} and queued the 10-Activity sample as ${result.run_id}. The flow daemon will execute it.`);
    } catch (error: any) { setMessage(error.message || "Could not queue the sample flow"); }
  }

  async function rewind(run: Run) {
    markBusy(run.id, true); setMessage("");
    try { const result = await api.rewindVisualRun(run.id); setMessage(`New run started from the beginning: ${result.run_id}`); setPage(0); await load(); }
    catch (error: any) { setMessage(error.message || "Could not rewind flow instance"); }
    finally { markBusy(run.id, false); }
  }

  async function deleteRuns(ids: string[]) {
    if (!ids.length || !window.confirm(`Delete ${ids.length} selected flow instance${ids.length === 1 ? "" : "s"}? Agent conversation history will be retained.`)) return;
    setMessage("");
    try {
      const result = await api.deleteVisualRuns(ids);
      const skipped = (result.active || []).length;
      setMessage(skipped ? `Deleted ${result.deleted.length}; ${skipped} active instance(s) were kept.` : `Deleted ${result.deleted.length} flow instance(s).`);
      if (expanded && ids.includes(expanded)) setExpanded(null);
      await load();
    } catch (error: any) { setMessage(error.message || "Could not delete selected flow instances"); }
  }

  return <div style={S.page}>
    <header style={S.head}>
      <h1 style={S.h1}>Flow Instances</h1><span style={S.count}>{total.toLocaleString()}</span>
      <button type="button" style={S.iconBtn()} onClick={runDemo} title="Publish and execute the branching sample flow"><FlaskConical size={13} /> Run sample flow</button>
      <div style={S.tools}>
        <select aria-label="Filter by state" style={S.select} value={state} onChange={(e) => { setState(e.target.value); setPage(0); }}>
          <option value="">All states</option><option value="queued">Queued</option><option value="running">Running</option><option value="waiting">Waiting</option><option value="completed">Completed</option><option value="failed">Failed</option><option value="crashed">Crashed</option><option value="cancelled">Cancelled</option>
        </select>
        <select aria-label="Rows per page" style={S.select} value={pageSize} onChange={(e) => { setPageSize(Number(e.target.value)); setPage(0); }}>
          {PAGE_SIZES.map((size) => <option key={size} value={size}>{size} per page</option>)}
        </select>
        <button type="button" style={S.iconBtn()} onClick={load} title="Refresh"><RefreshCw size={13} /> Refresh</button>
      </div>
    </header>

    <div style={S.searchWrap}><Search size={14} style={{ position: "absolute", left: 10, top: 8, color: "var(--fg-muted)" }} /><input style={S.search} placeholder="Search flow, run ID, user, organization, inventory org, or business unit…" value={query} onChange={(e) => { setQuery(e.target.value); setPage(0); }} /></div>
    {message && <div role="status" style={{ padding: "8px 10px", marginBottom: 10, border: "1px solid var(--border)", borderRadius: 6, color: "var(--fg-muted)", fontSize: 12 }}>{message}<button type="button" onClick={() => setMessage("")} style={{ float: "right", border: 0, background: "transparent", color: "inherit", cursor: "pointer" }}>×</button></div>}

    {loading ? <div style={S.empty}>Loading flow instances…</div> : runs.length === 0 ? <div style={S.empty}>No flow instances match these criteria.</div> : <div style={S.table}>
      {selected.size > 0 && <div style={S.actionBar}><strong>{selected.size} selected</strong><button type="button" style={S.iconBtn(true)} onClick={() => deleteRuns([...selected])}><Trash2 size={13} /> Delete selected</button><button type="button" style={S.iconBtn()} onClick={() => setSelected(new Set())}>Clear selection</button></div>}
      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <thead><tr>
          <th style={{ ...S.th, width: 32 }}><input aria-label="Select all on this page" type="checkbox" checked={allSelected} onChange={(e) => {
            if (e.target.checked) setSelected(new Set([...selected, ...runs.map((run) => run.id)]));
            else setSelected(new Set([...selected].filter((id) => !runs.some((run) => run.id === id))));
          }} /></th>
          <th style={{ ...S.th, width: 24 }} /> <th style={S.th}>Flow / instance</th><th style={S.th}>Run context</th><th style={S.th}>State</th><th style={S.th}>Started</th><th style={{ ...S.th, textAlign: "right" }}>Actions</th>
        </tr></thead>
        <tbody>{runs.map((run) => {
          const opened = expanded === run.id;
          const detail = details[run.id];
          return <React.Fragment key={run.id}>
            <tr style={{ borderBottom: "1px solid var(--border)" }}>
              <td style={S.td}><input aria-label={`Select ${run.flow_name || "flow"} ${run.id}`} type="checkbox" checked={selected.has(run.id)} onChange={(e) => setSelected((old) => { const next = new Set(old); e.target.checked ? next.add(run.id) : next.delete(run.id); return next; })} /></td>
              <td style={S.td}><button type="button" aria-label={opened ? "Collapse trajectory" : "Expand trajectory"} onClick={() => toggleDetails(run)} style={{ border: 0, background: "transparent", color: "var(--fg-muted)", padding: 0, cursor: "pointer" }}>{opened ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</button></td>
              <td style={S.td} onClick={() => toggleDetails(run)}>
                <div style={{ fontWeight: 650, cursor: "pointer" }}>{run.flow_name || "Unnamed flow"}{run.inventory_org && <span style={{ color: "var(--fg-muted)", fontWeight: 400 }}> · Inventory: {run.inventory_org}</span>}</div>
                <code style={{ fontSize: 10.5, color: "var(--fg-muted)" }}>{run.id.slice(0, 12)} · v{run.version} · {label(run.trigger_kind)}</code>
              </td>
              <td style={S.td}><div>{run.user_name || run.user_email || run.user_id}</div><small style={{ color: "var(--fg-muted)" }}>{run.organization_name || run.tenant_id}{run.business_unit ? ` · BU: ${run.business_unit}` : ""}</small></td>
              <td style={S.td}><span style={{ color: STATE_COLORS[run.state] || "var(--fg-muted)", fontWeight: 650 }}>{label(run.state)}</span></td>
              <td style={S.td}>{fmtDate(run.started_at || run.created_at)}</td>
              <td style={{ ...S.td, textAlign: "right", whiteSpace: "nowrap" }}>
                {failed(run.state) && <button type="button" style={S.iconBtn(false, busy.has(run.id))} disabled={busy.has(run.id)} title="Continue from the failed activity" onClick={() => retry(run)}><RotateCcw size={13} /> Retry</button>}
                {terminal(run.state) && <button type="button" style={S.iconBtn(false, busy.has(run.id))} disabled={busy.has(run.id)} title="Start a new run from the beginning" onClick={() => rewind(run)}><History size={13} /> Rewind</button>}
                {terminal(run.state) && <button type="button" style={S.iconBtn(true, busy.has(run.id))} disabled={busy.has(run.id)} title="Delete instance" onClick={() => deleteRuns([run.id])}><Trash2 size={13} /></button>}
              </td>
            </tr>
            {opened && <tr><td colSpan={7} style={{ padding: 0 }}><div style={S.detail}>
              <div style={{ fontWeight: 700, fontSize: 12, marginBottom: 8 }}>Execution graph · select a node to inspect its context</div>
              {detail === "loading" || !detail ? <div style={S.count}>Loading trajectory…</div> : typeof detail === "string" ? <div style={{ color: "var(--red)" }}>{detail}</div> : <>
                {detail.graph ? <FlowRunGraph graph={detail.graph} visits={detail.nodes || []} /> : <div style={{ color: "var(--fg-muted)" }}>This run has no published visual graph snapshot.</div>}
                <details style={{ marginTop: 12 }}><summary style={{ cursor: "pointer", fontSize: 11, color: "var(--fg-muted)" }}>Visit history ({(detail.nodes || []).length})</summary>
                  {(detail.nodes || []).map((node) => <div key={node.id} style={{ display: "grid", gridTemplateColumns: "minmax(130px, 1fr) 90px 100px 140px", gap: 10, padding: "6px 0", borderBottom: "1px solid var(--border)", fontSize: 12 }}>
                    <span><strong>{node.node_id}</strong><small style={{ display: "block", color: "var(--fg-muted)" }}>{node.node_kind} · attempt {node.attempt}</small></span><span>{label(node.state)}</span><span>{node.transient ? `→ ${node.transient}` : "—"}</span><span style={{ color: "var(--fg-muted)" }}>{fmtDate(node.started_at)}</span>
                    {Boolean(node.error) && <pre style={{ gridColumn: "1 / -1", whiteSpace: "pre-wrap", color: "var(--red)" }}>{JSON.stringify(node.error, null, 2)}</pre>}
                  </div>)}
                </details>
                {run.current_node_path && <div style={{ marginTop: 9, fontSize: 11.5, color: "var(--fg-muted)" }}>Current / failed activity: <code>{run.current_node_path}</code></div>}
                {Boolean(run.error) && <pre style={{ whiteSpace: "pre-wrap", color: "var(--red)" }}>{JSON.stringify(run.error, null, 2)}</pre>}
                {run.result != null && <pre style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(run.result, null, 2)}</pre>}
              </>}
            </div></td></tr>}
          </React.Fragment>;
        })}</tbody>
      </table>
      <footer style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "8px 11px", borderTop: "1px solid var(--border)", fontSize: 11.5, color: "var(--fg-muted)" }}>
        <span>{total ? `${page * pageSize + 1}–${Math.min((page + 1) * pageSize, total)} of ${total}` : "0"}</span>
        <div style={{ display: "flex", gap: 5 }}>
          <button type="button" style={S.btn(false, page === 0)} disabled={page === 0} onClick={() => setPage(0)} title="First page"><ChevronsLeft size={14} /></button>
          <button type="button" style={S.btn(false, page === 0)} disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))} title="Previous page"><ChevronLeft size={14} /></button>
          <span style={{ padding: "5px 8px" }}>Page {page + 1} of {pageCount}</span>
          <button type="button" style={S.btn(false, page + 1 >= pageCount)} disabled={page + 1 >= pageCount} onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))} title="Next page"><ChevronRight size={14} /></button>
          <button type="button" style={S.btn(false, page + 1 >= pageCount)} disabled={page + 1 >= pageCount} onClick={() => setPage(pageCount - 1)} title="Last page"><ChevronsRight size={14} /></button>
        </div>
      </footer>
    </div>}
  </div>;
}
