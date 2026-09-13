/**
 * FlowDeploymentsPage — binds a flow to a schedule and parameters (§9.1).
 *
 * A deployment is a flow's equivalent of an Agent: a saved, named, invokable
 * config. So its card carries the same action set as AgentCard
 * (AgentsPage.tsx) — Run, Edit, Duplicate, Delete — spec §14.10.
 *
 * Honesty rule carried over from the Scheduler page: if the runner isn't
 * ticking, say so rather than let a deployment look active while nothing will
 * ever fire it.
 */
import React, { useEffect, useState } from "react";
import {
  CalendarClock, Plus, Pause, Play, PlayCircle, Trash2, Edit2, Copy,
  AlertTriangle, RefreshCw, X,
} from "lucide-react";
import { api } from "../lib/api";

interface FlowDeployment {
  id: string;
  name: string;
  flow_name: string;
  schedule_raw: string | null;
  schedule: { kind: string; display?: string } | null;
  parameters: Record<string, unknown>;
  tags: string[];
  concurrency_limit: number | null;
  paused: boolean;
  next_run_at: string | null;
}

interface FlowDefinition {
  name: string;
  description: string | null;
}

const S = {
  page:    { padding: "20px", maxWidth: "900px" } as React.CSSProperties,
  head:    { display: "flex", alignItems: "center", gap: "10px", marginBottom: "10px" } as React.CSSProperties,
  h1:      { fontSize: "16px", fontWeight: 700, margin: 0 } as React.CSSProperties,
  addBtn:  { marginLeft: "auto", background: "var(--btn-bg)", color: "var(--btn-fg)", border: "none", borderRadius: "6px", padding: "6px 12px", cursor: "pointer", fontSize: "12px", fontWeight: 600, display: "flex", alignItems: "center", gap: "5px" } as React.CSSProperties,
  refreshBtn: { background: "transparent", border: "1px solid var(--border)", borderRadius: "6px", padding: "5px 8px", cursor: "pointer", color: "var(--fg-muted)", display: "flex", alignItems: "center", gap: "4px", fontSize: "12px" } as React.CSSProperties,
  warnBanner: { display: "flex", alignItems: "center", gap: "8px", background: "rgba(245,158,11,0.1)", border: "1px solid rgba(245,158,11,0.4)", color: "#b45309", borderRadius: "7px", padding: "9px 12px", fontSize: "12.5px", marginBottom: "14px" } as React.CSSProperties,
  runBanner: { display: "flex", alignItems: "center", gap: "8px", background: "rgba(34,197,94,0.1)", border: "1px solid rgba(34,197,94,0.35)", color: "#15803d", borderRadius: "7px", padding: "9px 12px", fontSize: "12.5px", marginBottom: "14px" } as React.CSSProperties,
  card:    { border: "1px solid var(--border)", borderRadius: "8px", padding: "13px 16px", marginBottom: "10px", background: "var(--card)" } as React.CSSProperties,
  row:     { display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" as const },
  name:    { fontSize: "13.5px", fontWeight: 700 } as React.CSSProperties,
  flowTag: { fontFamily: "monospace", fontSize: "11px", color: "var(--fg-muted)", background: "var(--input-bg)", padding: "1px 7px", borderRadius: "5px" } as React.CSSProperties,
  meta:    { fontSize: "11.5px", color: "var(--fg-muted)", marginTop: "5px", display: "flex", gap: "14px", flexWrap: "wrap" as const },
  pausedTag: { fontSize: "10.5px", fontWeight: 700, color: "#71717a", background: "var(--sidebar-active)", padding: "1px 7px", borderRadius: "10px" } as React.CSSProperties,
  actions: { display: "flex", alignItems: "center", gap: "4px", marginLeft: "auto" },
  iconBtn: (danger?: boolean): React.CSSProperties => ({
    background: "transparent", border: "1px solid var(--border)", borderRadius: "5px",
    padding: "4px 7px", cursor: "pointer", color: danger ? "#ef4444" : "var(--fg-muted)",
    display: "flex", alignItems: "center",
  }),
  empty:   { color: "var(--fg-muted)", fontSize: "13px", padding: "24px 0", textAlign: "center" as const },
  modalOverlay: { position: "fixed" as const, inset: 0, background: "rgba(0,0,0,0.4)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 50 },
  modal:   { background: "var(--card)", border: "1px solid var(--border)", borderRadius: "10px", padding: "20px", width: "440px", maxWidth: "90vw" } as React.CSSProperties,
  field:   { display: "block", fontSize: "11.5px", fontWeight: 600, color: "var(--fg-muted)", marginBottom: "4px", marginTop: "12px" },
  input:   { width: "100%", background: "var(--input-bg)", border: "1px solid var(--border)", borderRadius: "6px", padding: "7px 9px", color: "var(--fg)", fontSize: "13px", boxSizing: "border-box" as const },
  hint:    { fontSize: "10.5px", color: "var(--fg-muted)", marginTop: "3px" },
  modalActions: { display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "18px" },
  cancelBtn: { fontSize: "12.5px", padding: "6px 14px", borderRadius: "6px", border: "1px solid var(--border)", background: "transparent", color: "var(--fg)", cursor: "pointer" } as React.CSSProperties,
  saveBtn: { fontSize: "12.5px", padding: "6px 14px", borderRadius: "6px", border: "none", background: "var(--btn-bg)", color: "var(--btn-fg)", cursor: "pointer", fontWeight: 600 } as React.CSSProperties,
  err:     { color: "#ef4444", fontSize: "12px", marginTop: "10px" },
};

export function FlowDeploymentsPage() {
  const [deployments, setDeployments] = useState<FlowDeployment[]>([]);
  const [runnerRunning, setRunnerRunning] = useState(true);
  const [definitions, setDefinitions] = useState<FlowDefinition[]>([]);
  const [loading, setLoading] = useState(true);
  const [formTarget, setFormTarget] = useState<"create" | FlowDeployment | null>(null);
  const [runningId, setRunningId] = useState<string | null>(null);
  const [lastRun, setLastRun] = useState<{ dep: string; runId: string } | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const [d, defs] = await Promise.all([api.getFlowDeployments(), api.getFlowDefinitions()]);
      setDeployments(d.deployments || []);
      setRunnerRunning(d.runner_running !== false);
      setDefinitions(defs.definitions || []);
    } catch {
      setDeployments([]);
    }
    setLoading(false);
  };

  useEffect(() => { load(); }, []);

  async function togglePause(dep: FlowDeployment) {
    if (dep.paused) await api.resumeFlowDeployment(dep.id);
    else await api.pauseFlowDeployment(dep.id);
    load();
  }

  async function remove(dep: FlowDeployment) {
    if (!confirm(`Delete deployment "${dep.name}"?`)) return;
    await api.deleteFlowDeployment(dep.id);
    load();
  }

  async function runNow(dep: FlowDeployment) {
    setRunningId(dep.id);
    setLastRun(null);
    try {
      const res = await api.runFlowDeploymentNow(dep.id);
      setLastRun({ dep: dep.name, runId: res.run_id });
    } catch (e: any) {
      alert(e.message || "Failed to start run");
    }
    setRunningId(null);
  }

  // Duplicate needs no new backend, same as AgentsPage's onCopy — it's the
  // existing create call with this deployment's fields, minus its schedule
  // (a copy that immediately started firing on the original's cadence would
  // be a surprise, not a convenience) and paused so the duplicate is inert
  // until reviewed.
  async function duplicate(dep: FlowDeployment) {
    await api.createFlowDeployment({
      name: `${dep.name}-copy`, flow_name: dep.flow_name,
      schedule: null, parameters: dep.parameters, tags: dep.tags,
      concurrency_limit: dep.concurrency_limit,
    });
    await load();
  }

  return (
    <div style={S.page}>
      <div style={S.head}>
        <h1 style={S.h1}>Deployments</h1>
        <span style={{ fontSize: "12px", color: "var(--fg-muted)" }}>{deployments.length}</span>
        <button style={S.refreshBtn} onClick={load}><RefreshCw size={12} /> Refresh</button>
        <button style={S.addBtn} onClick={() => setFormTarget("create")}><Plus size={13} /> New</button>
      </div>

      {!runnerRunning && (
        <div style={S.warnBanner}>
          <AlertTriangle size={14} />
          The runner is not ticking — no deployment will fire on its schedule until the
          dashboard (or a process calling <code>start_runner()</code>) is running. "Run now"
          below still works: it starts the flow directly, not through the runner's tick.
        </div>
      )}

      {lastRun && (
        <div style={S.runBanner}>
          <Play size={13} />
          Started <strong>{lastRun.dep}</strong> — run <code>{lastRun.runId.slice(0, 8)}</code>.
          {" "}See it on the <a href="/flows/instances" style={{ color: "inherit", textDecoration: "underline" }}>Instances</a> page.
          <button onClick={() => setLastRun(null)} style={{ marginLeft: "auto", background: "transparent", border: "none", cursor: "pointer", color: "inherit", display: "flex" }}>
            <X size={13} />
          </button>
        </div>
      )}

      {loading ? (
        <div style={S.empty}>Loading…</div>
      ) : deployments.length === 0 ? (
        <div style={S.empty}>No deployments yet. Create one to run a flow on a schedule.</div>
      ) : (
        deployments.map((d) => (
          <div key={d.id} style={S.card}>
            <div style={S.row}>
              <CalendarClock size={15} style={{ color: "var(--fg-muted)" }} />
              <span style={S.name}>{d.name}</span>
              <span style={S.flowTag}>{d.flow_name}</span>
              {d.paused && <span style={S.pausedTag}>PAUSED</span>}

              <div style={S.actions}>
                <button
                  style={{ ...S.iconBtn(), opacity: runningId === d.id ? 0.5 : 1 }}
                  onClick={() => runNow(d)}
                  disabled={runningId === d.id}
                  title="Run now — fires immediately, does not disturb the schedule"
                >
                  <Play size={13} />
                </button>
                <button style={S.iconBtn()} onClick={() => togglePause(d)} title={d.paused ? "Resume schedule" : "Pause schedule"}>
                  {d.paused ? <PlayCircle size={13} /> : <Pause size={13} />}
                </button>
                <button style={S.iconBtn()} onClick={() => setFormTarget(d)} title="Edit deployment">
                  <Edit2 size={13} />
                </button>
                <button style={S.iconBtn()} onClick={() => duplicate(d)} title="Duplicate deployment">
                  <Copy size={13} />
                </button>
                <button style={S.iconBtn(true)} onClick={() => remove(d)} title="Delete deployment">
                  <Trash2 size={13} />
                </button>
              </div>
            </div>
            <div style={S.meta}>
              <span>Schedule: {d.schedule?.display || d.schedule_raw || "—"}</span>
              <span>Next run: {d.next_run_at ? new Date(d.next_run_at).toLocaleString() : "—"}</span>
              {d.concurrency_limit != null && <span>Concurrency limit: {d.concurrency_limit}</span>}
            </div>
          </div>
        ))
      )}

      {formTarget && (
        <DeploymentFormModal
          definitions={definitions}
          deployment={formTarget === "create" ? null : formTarget}
          onClose={() => setFormTarget(null)}
          onSaved={() => { setFormTarget(null); load(); }}
        />
      )}
    </div>
  );
}

/** Create or edit a deployment — one form, one save handler each way, same
 * as AgentsPage's edit view doubling as its create view. */
function DeploymentFormModal({
  definitions, deployment, onClose, onSaved,
}: {
  definitions: FlowDefinition[];
  deployment: FlowDeployment | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const editing = deployment !== null;
  const [name, setName] = useState(deployment?.name || "");
  const [flowName, setFlowName] = useState(deployment?.flow_name || definitions[0]?.name || "");
  const [schedule, setSchedule] = useState(deployment?.schedule_raw || "");
  const [parametersRaw, setParametersRaw] = useState(
    JSON.stringify(deployment?.parameters ?? {}, null, 0),
  );
  const [concurrencyLimit, setConcurrencyLimit] = useState(
    deployment?.concurrency_limit != null ? String(deployment.concurrency_limit) : "",
  );
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function submit() {
    setError(null);
    let parameters: object;
    try {
      parameters = JSON.parse(parametersRaw || "{}");
    } catch {
      setError("Parameters must be valid JSON.");
      return;
    }
    if (!name.trim() || !flowName.trim()) {
      setError("Name and flow are required.");
      return;
    }
    setSaving(true);
    try {
      const payload = {
        name: name.trim(),
        flow_name: flowName.trim(),
        schedule: schedule.trim() || null,
        parameters,
        tags: deployment?.tags ?? [],
        concurrency_limit: concurrencyLimit ? Number(concurrencyLimit) : null,
      };
      if (editing) {
        await api.updateFlowDeployment(deployment.id, payload);
      } else {
        await api.createFlowDeployment(payload);
      }
      onSaved();
    } catch (e: any) {
      setError(String(e.message || e));
    }
    setSaving(false);
  }

  return (
    <div style={S.modalOverlay} onClick={onClose}>
      <div style={S.modal} onClick={(e) => e.stopPropagation()}>
        <div style={{ display: "flex", alignItems: "center" }}>
          <h2 style={{ fontSize: "14px", fontWeight: 700, margin: 0 }}>
            {editing ? `Edit ${deployment!.name}` : "New Deployment"}
          </h2>
          <button onClick={onClose} style={{ marginLeft: "auto", background: "transparent", border: "none", cursor: "pointer", color: "var(--fg-muted)" }}>
            <X size={16} />
          </button>
        </div>

        <label style={S.field}>Name</label>
        <input style={S.input} value={name} onChange={(e) => setName(e.target.value)} placeholder="daily-report" />

        <label style={S.field}>Flow</label>
        {editing ? (
          <>
            <input style={{ ...S.input, fontFamily: "monospace", opacity: 0.7 }} value={flowName} disabled />
            <div style={S.hint}>
              Which flow a deployment runs isn't editable — duplicate it instead if you need
              the same schedule and parameters against a different flow.
            </div>
          </>
        ) : definitions.length === 0 ? (
          <div style={S.hint}>
            No registered flows. A @flow function must be imported in this process before it can be deployed.
          </div>
        ) : (
          <select style={S.input} value={flowName} onChange={(e) => setFlowName(e.target.value)}>
            {definitions.map((d) => <option key={d.name} value={d.name}>{d.name}</option>)}
          </select>
        )}

        <label style={S.field}>Schedule</label>
        <input style={S.input} value={schedule} onChange={(e) => setSchedule(e.target.value)}
               placeholder="e.g. 30m, 0 9 * * 1-5, every morning at 9am" />
        <div style={S.hint}>Leave blank for no schedule (manual "Run now" only).</div>

        <label style={S.field}>Parameters (JSON)</label>
        <input style={{ ...S.input, fontFamily: "monospace" }} value={parametersRaw}
               onChange={(e) => setParametersRaw(e.target.value)} />

        <label style={S.field}>Concurrency limit</label>
        <input style={S.input} type="number" min={1} value={concurrencyLimit}
               onChange={(e) => setConcurrencyLimit(e.target.value)} placeholder="unlimited" />

        {error && <div style={S.err}>{error}</div>}

        <div style={S.modalActions}>
          <button style={S.cancelBtn} onClick={onClose}>Cancel</button>
          <button style={S.saveBtn} onClick={submit} disabled={saving || (!editing && definitions.length === 0)}>
            {saving ? "Saving…" : editing ? "Save" : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}
