/**
 * FlowInstancesPage — "what is running / stuck" (spec §14.2 judgment call #2:
 * operators outnumber authors, so Instances is the default flow surface).
 *
 * Spec: 15-flow.md §11 (filter-by-POST, action endpoints), §14.3.
 */
import React, { useCallback, useEffect, useState } from "react";
import {
  Play, ChevronRight, ChevronDown, RefreshCw, XCircle,
  CheckCircle, AlertCircle, Clock3, Ban, Loader2,
} from "lucide-react";
import { api } from "../lib/api";

interface FlowRun {
  id: string;
  name: string;
  kind: "flow" | "task";
  state_type: string;
  state_name: string;
  parent_run_id: string | null;
  flow_run_id: string | null;
  tags: string[];
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  is_final: boolean;
  result: unknown;
  error: unknown;
}

interface HistoryEntry {
  seq: number;
  state_type: string;
  state_name: string;
  message: string | null;
  orchestration: string;
  at: number;
}

const STATE_COLOR: Record<string, string> = {
  RUNNING: "#456DE6", SCHEDULED: "#a855f7", PENDING: "#71717a",
  PAUSED: "#f59e0b", CANCELLING: "#f59e0b",
  COMPLETED: "#22c55e", FAILED: "#ef4444", CRASHED: "#ef4444", CANCELLED: "#71717a",
};

const STATE_ICON: Record<string, React.ElementType> = {
  RUNNING: Loader2, SCHEDULED: Clock3, PENDING: Clock3,
  PAUSED: Clock3, CANCELLING: Ban,
  COMPLETED: CheckCircle, FAILED: AlertCircle, CRASHED: AlertCircle, CANCELLED: Ban,
};

const PAGE_SIZE = 30;

function fmtDate(ts: number | null): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
}

function fmtDuration(start: number | null, end: number | null): string {
  if (!start) return "—";
  const seconds = (end || Date.now() / 1000) - start;
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

const S = {
  page:    { padding: "20px", maxWidth: "1100px" } as React.CSSProperties,
  head:    { display: "flex", alignItems: "center", gap: "10px", marginBottom: "14px", flexWrap: "wrap" as const },
  h1:      { fontSize: "16px", fontWeight: 700, margin: 0 } as React.CSSProperties,
  filters: { display: "flex", gap: "6px", marginLeft: "auto" },
  filterBtn: (active: boolean): React.CSSProperties => ({
    fontSize: "11.5px", padding: "4px 10px", borderRadius: "12px", cursor: "pointer",
    border: `1px solid ${active ? "var(--fg)" : "var(--border)"}`,
    background: active ? "var(--sidebar-active)" : "transparent",
    color: active ? "var(--fg)" : "var(--fg-muted)",
  }),
  table:   { border: "1px solid var(--border)", borderRadius: "8px", overflow: "hidden" } as React.CSSProperties,
  th:      { textAlign: "left" as const, padding: "9px 14px", fontSize: "11px", fontWeight: 700, textTransform: "uppercase" as const, letterSpacing: "0.07em", color: "var(--fg-muted)", borderBottom: "1px solid var(--border)", background: "var(--card)" },
  td:      { padding: "9px 14px", verticalAlign: "middle" as const, fontSize: "13px" },
  row:     { borderBottom: "1px solid var(--border)", cursor: "pointer" } as React.CSSProperties,
  chevron: { color: "var(--fg-muted)", flexShrink: 0 } as React.CSSProperties,
  badge:   (color: string): React.CSSProperties => ({
    display: "inline-flex", alignItems: "center", gap: "5px",
    fontSize: "11.5px", fontWeight: 600, color,
  }),
  cancelBtn: { fontSize: "11px", padding: "3px 9px", borderRadius: "5px", border: "1px solid #ef4444", color: "#ef4444", background: "transparent", cursor: "pointer" } as React.CSSProperties,
  detail:  { background: "var(--card)", borderTop: "1px solid var(--border)", padding: "14px 20px" } as React.CSSProperties,
  histRow: { display: "flex", gap: "10px", fontSize: "12px", padding: "3px 0", color: "var(--fg-muted)" } as React.CSSProperties,
  pre:     { margin: "6px 0 0", padding: "8px 10px", fontSize: "11.5px", fontFamily: "monospace", background: "var(--input-bg)", border: "1px solid var(--border)", borderRadius: "6px", whiteSpace: "pre-wrap" as const, wordBreak: "break-word" as const, maxHeight: "220px", overflow: "auto" },
  empty:   { color: "var(--fg-muted)", fontSize: "13px", padding: "24px 0", textAlign: "center" as const },
};

const FILTERS: { label: string; states: string[] | null }[] = [
  { label: "All", states: null },
  { label: "Running", states: ["RUNNING", "SCHEDULED"] },
  { label: "Failed", states: ["FAILED", "CRASHED"] },
  { label: "Completed", states: ["COMPLETED"] },
];

export function FlowInstancesPage() {
  const [runs, setRuns] = useState<FlowRun[]>([]);
  const [total, setTotal] = useState(0);
  const [filterIdx, setFilterIdx] = useState(0);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [history, setHistory] = useState<Record<string, HistoryEntry[] | "loading" | "error">>({});

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api.filterFlowRuns({
        limit: PAGE_SIZE,
        offset: 0,
        state_types: FILTERS[filterIdx].states,
        kind: "flow", // top-level view — task detail lives in the flow's graph
      });
      setRuns(d.runs || []);
      setTotal(d.total ?? 0);
    } catch {
      setRuns([]);
    }
    setLoading(false);
  }, [filterIdx]);

  useEffect(() => { load(); }, [load]);

  async function toggle(runId: string) {
    if (expanded === runId) { setExpanded(null); return; }
    setExpanded(runId);
    if (!history[runId]) {
      setHistory((p) => ({ ...p, [runId]: "loading" }));
      try {
        const d = await api.getFlowRunHistory(runId);
        setHistory((p) => ({ ...p, [runId]: d.history || [] }));
      } catch {
        setHistory((p) => ({ ...p, [runId]: "error" }));
      }
    }
  }

  async function cancel(e: React.MouseEvent, runId: string) {
    e.stopPropagation();
    await api.cancelFlowRun(runId);
    load();
  }

  return (
    <div style={S.page}>
      <div style={S.head}>
        <h1 style={S.h1}>Flow Instances</h1>
        <span style={{ fontSize: "12px", color: "var(--fg-muted)" }}>{total}</span>
        <div style={S.filters}>
          {FILTERS.map((f, i) => (
            <button key={f.label} style={S.filterBtn(i === filterIdx)} onClick={() => setFilterIdx(i)}>
              {f.label}
            </button>
          ))}
        </div>
        <button
          onClick={load}
          style={{ background: "transparent", border: "1px solid var(--border)", borderRadius: "6px", padding: "5px 8px", cursor: "pointer", color: "var(--fg-muted)", display: "flex", alignItems: "center", gap: "4px", fontSize: "12px" }}
        >
          <RefreshCw size={12} /> Refresh
        </button>
      </div>

      {loading ? (
        <div style={S.empty}>Loading…</div>
      ) : runs.length === 0 ? (
        <div style={S.empty}>No flow runs match this filter.</div>
      ) : (
        <div style={S.table}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th style={S.th}></th>
                <th style={S.th}>Name</th>
                <th style={S.th}>State</th>
                <th style={S.th}>Started</th>
                <th style={S.th}>Duration</th>
                <th style={S.th}></th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => {
                const color = STATE_COLOR[r.state_type] || "var(--fg-muted)";
                const Icon = STATE_ICON[r.state_type] || Play;
                const isOpen = expanded === r.id;
                const hist = history[r.id];
                return (
                  <React.Fragment key={r.id}>
                    <tr style={S.row} onClick={() => toggle(r.id)}>
                      <td style={{ ...S.td, width: "28px" }}>
                        {isOpen ? <ChevronDown size={14} style={S.chevron} /> : <ChevronRight size={14} style={S.chevron} />}
                      </td>
                      <td style={{ ...S.td, fontFamily: "monospace", fontWeight: 600 }}>{r.name}</td>
                      <td style={S.td}>
                        <span style={S.badge(color)}>
                          <Icon size={13} className={r.state_type === "RUNNING" ? "spin" : undefined} />
                          {r.state_name}
                        </span>
                      </td>
                      <td style={S.td}>{fmtDate(r.started_at)}</td>
                      <td style={S.td}>{fmtDuration(r.started_at, r.finished_at)}</td>
                      <td style={S.td}>
                        {!r.is_final && (
                          <button style={S.cancelBtn} onClick={(e) => cancel(e, r.id)}>
                            <XCircle size={11} style={{ marginRight: "3px", verticalAlign: "-2px" }} />
                            Cancel
                          </button>
                        )}
                      </td>
                    </tr>
                    {isOpen && (
                      <tr>
                        <td colSpan={6} style={{ padding: 0 }}>
                          <div style={S.detail}>
                            {r.error != null && (
                              <>
                                <div style={{ fontSize: "12px", fontWeight: 700, color: "#ef4444" }}>Error</div>
                                <pre style={S.pre}>{String(r.error)}</pre>
                              </>
                            )}
                            {r.result != null && r.state_type === "COMPLETED" && (
                              <>
                                <div style={{ fontSize: "12px", fontWeight: 700, marginTop: r.error != null ? "10px" : 0 }}>Result</div>
                                <pre style={S.pre}>{JSON.stringify(r.result, null, 2)}</pre>
                              </>
                            )}
                            <div style={{ fontSize: "12px", fontWeight: 700, marginTop: "10px" }}>History</div>
                            {hist === "loading" && <div style={{ fontSize: "12px", color: "var(--fg-muted)" }}>Loading…</div>}
                            {hist === "error" && <div style={{ fontSize: "12px", color: "#ef4444" }}>Failed to load history.</div>}
                            {Array.isArray(hist) && hist.map((h) => (
                              <div key={h.seq} style={S.histRow}>
                                <span style={{ width: "60px", flexShrink: 0 }}>{fmtDate(h.at).split(",")[1]}</span>
                                <span style={{ color: STATE_COLOR[h.state_type] || "var(--fg-muted)", fontWeight: 600, width: "140px", flexShrink: 0 }}>
                                  {h.state_name}
                                </span>
                                {h.orchestration !== "ACCEPT" && (
                                  <span style={{ fontSize: "10.5px", padding: "0 6px", borderRadius: "8px", background: "var(--sidebar-active)" }}>
                                    {h.orchestration}
                                  </span>
                                )}
                                {h.message && <span>{h.message}</span>}
                              </div>
                            ))}
                          </div>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <style>{`
        .spin { animation: flow-spin 1s linear infinite; }
        @keyframes flow-spin { to { transform: rotate(360deg); } }
      `}</style>
    </div>
  );
}
