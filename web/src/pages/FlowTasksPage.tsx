/**
 * FlowTasksPage — pending human-in-the-loop approval gates.
 *
 * Spec: 15-flow.md §8, §14.2 ("a silently unread ask is a stalled flow nobody
 * notices" — this page plus the nav badge is the entire discoverability story).
 */
import React, { useEffect, useState } from "react";
import { Inbox, Clock3, AlertTriangle, Check, X, RefreshCw } from "lucide-react";
import { api } from "../lib/api";

interface PendingTask {
  flow_run_id: string;
  pause_key: string | null;
  reschedule: boolean;
  timeout: number | null;
  paused_at: number;
  expired: boolean;
}

const S = {
  page:   { padding: "20px", maxWidth: "800px" } as React.CSSProperties,
  head:   { display: "flex", alignItems: "center", gap: "10px", marginBottom: "16px" } as React.CSSProperties,
  h1:     { fontSize: "16px", fontWeight: 700, margin: 0 } as React.CSSProperties,
  refreshBtn: { marginLeft: "auto", background: "transparent", border: "1px solid var(--border)", borderRadius: "6px", padding: "5px 8px", cursor: "pointer", color: "var(--fg-muted)", display: "flex", alignItems: "center", gap: "4px", fontSize: "12px" } as React.CSSProperties,
  card:   (expired: boolean): React.CSSProperties => ({
    border: `1px solid ${expired ? "#ef4444" : "var(--border)"}`,
    borderRadius: "8px", padding: "14px 16px", marginBottom: "10px",
    background: "var(--card)",
  }),
  row:    { display: "flex", alignItems: "center", gap: "10px" } as React.CSSProperties,
  runId:  { fontFamily: "monospace", fontSize: "13px", fontWeight: 600 } as React.CSSProperties,
  meta:   { fontSize: "11.5px", color: "var(--fg-muted)", marginTop: "4px", display: "flex", gap: "12px" } as React.CSSProperties,
  actions: { display: "flex", gap: "8px", marginLeft: "auto" },
  approveBtn: { fontSize: "12px", padding: "5px 12px", borderRadius: "5px", border: "none", background: "#22c55e", color: "#fff", cursor: "pointer", display: "flex", alignItems: "center", gap: "4px" } as React.CSSProperties,
  rejectBtn:  { fontSize: "12px", padding: "5px 12px", borderRadius: "5px", border: "1px solid #ef4444", background: "transparent", color: "#ef4444", cursor: "pointer", display: "flex", alignItems: "center", gap: "4px" } as React.CSSProperties,
  expiredTag: { fontSize: "10.5px", fontWeight: 700, color: "#ef4444", display: "flex", alignItems: "center", gap: "4px", marginTop: "6px" } as React.CSSProperties,
  empty:  { color: "var(--fg-muted)", fontSize: "13px", padding: "32px 0", textAlign: "center" as const, display: "flex", flexDirection: "column" as const, alignItems: "center", gap: "8px" },
};

function fmtAge(paused_at: number): string {
  const seconds = Date.now() / 1000 - paused_at;
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

export function FlowTasksPage() {
  const [tasks, setTasks] = useState<PendingTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<Set<string>>(new Set());

  const load = async () => {
    setLoading(true);
    try {
      const d = await api.getFlowTasks();
      setTasks(d.tasks || []);
    } catch {
      setTasks([]);
    }
    setLoading(false);
  };

  useEffect(() => {
    load();
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, []);

  async function respond(runId: string, approve: boolean) {
    setBusy((s) => new Set(s).add(runId));
    try {
      await api.respondToFlowTask(runId, { approve });
      setTasks((prev) => prev.filter((t) => t.flow_run_id !== runId));
    } finally {
      setBusy((s) => { const n = new Set(s); n.delete(runId); return n; });
    }
  }

  return (
    <div style={S.page}>
      <div style={S.head}>
        <h1 style={S.h1}>My Tasks</h1>
        <span style={{ fontSize: "12px", color: "var(--fg-muted)" }}>{tasks.length} pending</span>
        <button style={S.refreshBtn} onClick={load}><RefreshCw size={12} /> Refresh</button>
      </div>

      {loading ? (
        <div style={S.empty}>Loading…</div>
      ) : tasks.length === 0 ? (
        <div style={S.empty}>
          <Inbox size={28} style={{ opacity: 0.4 }} />
          Nothing waiting on you right now.
        </div>
      ) : (
        tasks.map((t) => (
          <div key={t.flow_run_id} style={S.card(t.expired)}>
            <div style={S.row}>
              <span style={S.runId}>{t.flow_run_id}</span>
              {t.pause_key && <span style={{ fontSize: "11px", color: "var(--fg-muted)" }}>({t.pause_key})</span>}
              <div style={S.actions}>
                <button
                  style={S.approveBtn}
                  disabled={busy.has(t.flow_run_id)}
                  onClick={() => respond(t.flow_run_id, true)}
                >
                  <Check size={13} /> Approve
                </button>
                <button
                  style={S.rejectBtn}
                  disabled={busy.has(t.flow_run_id)}
                  onClick={() => respond(t.flow_run_id, false)}
                >
                  <X size={13} /> Reject
                </button>
              </div>
            </div>
            <div style={S.meta}>
              <span><Clock3 size={11} style={{ verticalAlign: "-2px", marginRight: "3px" }} />{fmtAge(t.paused_at)}</span>
              {t.timeout && <span>Timeout: {t.timeout}s</span>}
              <span>{t.reschedule ? "Suspended (process exited)" : "Paused (process alive)"}</span>
            </div>
            {t.expired && (
              <div style={S.expiredTag}>
                <AlertTriangle size={11} /> Timeout exceeded — this flow's escalation path should have fired
              </div>
            )}
          </div>
        ))
      )}
    </div>
  );
}
