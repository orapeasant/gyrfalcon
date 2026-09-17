/**
 * FlowInstancesPage — "what is running / stuck" (spec §14.2 judgment call #2:
 * operators outnumber authors, so Instances is the default flow surface).
 *
 * Spec: 15-flow.md §11 (filter-by-POST, action endpoints), §14.3.
 */
import React, { useCallback, useEffect, useState } from "react";
import {
  Play, ChevronRight, ChevronDown, RefreshCw, XCircle,
  CheckCircle, AlertCircle, Clock3, Ban, Loader2, RotateCcw, Trash2,
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
  actionBtn: (color: string): React.CSSProperties => ({
    fontSize: "11px", padding: "3px 9px", borderRadius: "5px", border: `1px solid ${color}`,
    color, background: "transparent", cursor: "pointer", display: "inline-flex",
    alignItems: "center", gap: "3px",
  }),
  actions: { display: "flex", gap: "6px", justifyContent: "flex-end" } as React.CSSProperties,
  searchRow: { display: "flex", gap: "8px", alignItems: "center", marginBottom: "10px", flexWrap: "wrap" as const },
  input: {
    fontSize: "12.5px", padding: "5px 10px", borderRadius: "6px",
    border: "1px solid var(--border)", background: "var(--input-bg)", color: "var(--fg)",
  } as React.CSSProperties,
  dateInput: {
    fontSize: "12.5px", padding: "5px 8px", borderRadius: "6px",
    border: "1px solid var(--border)", background: "var(--input-bg)", color: "var(--fg)",
  } as React.CSSProperties,
  bulkBar: {
    display: "flex", alignItems: "center", gap: "10px", marginBottom: "10px",
    padding: "6px 10px", borderRadius: "6px", background: "var(--sidebar-active)",
    fontSize: "12.5px",
  } as React.CSSProperties,
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
  const [nameQuery, setNameQuery] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [history, setHistory] = useState<Record<string, HistoryEntry[] | "loading" | "error">>({});
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const load = useCallback(async () => {
    setLoading(true);
    try {
      // Local midnight, so a picked date covers that whole calendar day
      // regardless of the viewer's timezone offset from UTC.
      const created_from = dateFrom ? new Date(`${dateFrom}T00:00:00`).getTime() / 1000 : null;
      const created_to = dateTo ? new Date(`${dateTo}T23:59:59.999`).getTime() / 1000 : null;
      const d = await api.filterFlowRuns({
        limit: PAGE_SIZE,
        offset: 0,
        state_types: FILTERS[filterIdx].states,
        kind: "flow", // top-level view — task detail lives in the flow's graph
        name: nameQuery.trim() || null,
        created_from,
        created_to,
      });
      setRuns(d.runs || []);
      setTotal(d.total ?? 0);
      setSelected(new Set());
    } catch {
      setRuns([]);
    }
    setLoading(false);
  }, [filterIdx, nameQuery, dateFrom, dateTo]);

  // Debounced so the name search doesn't fire a request per keystroke.
  useEffect(() => {
    const t = setTimeout(load, 300);
    return () => clearTimeout(t);
  }, [load]);

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

  async function retry(e: React.MouseEvent, runId: string) {
    e.stopPropagation();
    try {
      await api.retryFlowRun(runId);
      load();
    } catch (err: any) {
      alert(err.message || "Failed to retry run");
    }
  }

  async function deleteOne(e: React.MouseEvent, runId: string) {
    e.stopPropagation();
    if (!confirm("Delete this flow run? This cannot be undone.")) return;
    try {
      await api.deleteFlowRun(runId);
      load();
    } catch (err: any) {
      alert(err.message || "Failed to delete run");
    }
  }

  async function deleteSelected() {
    if (selected.size === 0) return;
    if (!confirm(`Delete ${selected.size} selected run(s)? This cannot be undone.`)) return;
    const res = await api.deleteFlowRuns(Array.from(selected));
    if (res.refused?.length) {
      alert(`${res.refused.length} run(s) are still active and were not deleted; cancel them first.`);
    }
    load();
  }

  function toggleRow(runId: string) {
    setSelected((p) => {
      const next = new Set(p);
      if (next.has(runId)) next.delete(runId); else next.add(runId);
      return next;
    });
  }

  function toggleAll() {
    setSelected((p) => (p.size === runs.length ? new Set() : new Set(runs.map((r) => r.id))));
  }

  return (
    <div style={S.page}>
      <div style={S.head}>
        <h1 style={S.h1}>Flow Instances</h1>
        <span style={{ fontSize: "12px", color: "var(--fg-muted)" }}>{total}</span>
      </div>

      <div style={S.searchRow}>
        <input
          style={{ ...S.input, minWidth: "220px" }}
          type="text"
          placeholder="Search by name…"
          value={nameQuery}
          onChange={(e) => setNameQuery(e.target.value)}
        />
        <span style={{ fontSize: "11.5px", color: "var(--fg-muted)" }}>from</span>
        <input style={S.dateInput} type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} />
        <span style={{ fontSize: "11.5px", color: "var(--fg-muted)" }}>to</span>
        <input style={S.dateInput} type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} />
        {(nameQuery || dateFrom || dateTo) && (
          <button
            onClick={() => { setNameQuery(""); setDateFrom(""); setDateTo(""); }}
            style={{ background: "transparent", border: "none", color: "var(--fg-muted)", cursor: "pointer", fontSize: "11.5px" }}
          >
            Clear
          </button>
        )}
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

      {selected.size > 0 && (
        <div style={S.bulkBar}>
          <span>{selected.size} selected</span>
          <button style={S.actionBtn("#ef4444")} onClick={deleteSelected}>
            <Trash2 size={11} /> Delete selected
          </button>
          <button
            onClick={() => setSelected(new Set())}
            style={{ background: "transparent", border: "none", color: "var(--fg-muted)", cursor: "pointer", fontSize: "11.5px", marginLeft: "auto" }}
          >
            Clear selection
          </button>
        </div>
      )}

      {loading ? (
        <div style={S.empty}>Loading…</div>
      ) : runs.length === 0 ? (
        <div style={S.empty}>No flow runs match this filter.</div>
      ) : (
        <div style={S.table}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th style={{ ...S.th, width: "30px" }}>
                  <input
                    type="checkbox"
                    checked={runs.length > 0 && selected.size === runs.length}
                    onChange={toggleAll}
                    aria-label="Select all"
                  />
                </th>
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
                      <td style={{ ...S.td, width: "30px" }} onClick={(e) => e.stopPropagation()}>
                        <input
                          type="checkbox"
                          checked={selected.has(r.id)}
                          onChange={() => toggleRow(r.id)}
                          aria-label={`Select ${r.name}`}
                        />
                      </td>
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
                        <div style={S.actions}>
                          {!r.is_final && (
                            <button style={S.cancelBtn} onClick={(e) => cancel(e, r.id)}>
                              <XCircle size={11} style={{ marginRight: "3px", verticalAlign: "-2px" }} />
                              Cancel
                            </button>
                          )}
                          {r.is_final && (
                            <>
                              <button style={S.actionBtn("#456DE6")} onClick={(e) => retry(e, r.id)}>
                                <RotateCcw size={11} /> Retry
                              </button>
                              <button style={S.actionBtn("#ef4444")} onClick={(e) => deleteOne(e, r.id)}>
                                <Trash2 size={11} /> Delete
                              </button>
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                    {isOpen && (
                      <tr>
                        <td colSpan={7} style={{ padding: 0 }}>
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
