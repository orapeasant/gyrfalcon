import React, { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import {
  Clock, Plus, Copy, Trash2, Play, Pause, Save,
  ChevronRight, FileText, RefreshCw, CheckCircle,
  XCircle, AlertCircle, Circle, Timer, Zap,
} from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────────────

interface SchedulerJob {
  id: string;
  name: string;
  prompt: string;
  skill?: string;
  model?: string;
  provider?: string;
  schedule_raw: string;
  schedule_display: string;
  schedule: { kind: string; display: string };
  repeat: { times?: number; completed: number };
  enabled: boolean;
  state: string;
  last_run_at?: string;
  last_status?: string;
  last_error?: string;
  next_run_at?: string;
  created_at: string;
}

interface HistoryRun {
  run_id: string;
  filename: string;
  timestamp: string;
  size_bytes: number;
}

type Tab = "edit" | "history";

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtTs(ts?: string): string {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleString();
  } catch { return ts; }
}

function fmtRunTs(stem: string): string {
  // "20260701_112957" -> "2026-07-01 11:29:57"
  const m = stem.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/);
  if (!m) return stem;
  return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}`;
}

function emptyJob(): Partial<SchedulerJob> & { _isNew: boolean } {
  return {
    _isNew: true, name: "", prompt: "", skill: "",
    model: "", provider: "", schedule_raw: "30m",
    enabled: true,
  };
}

// ── Status badge ──────────────────────────────────────────────────────────────

function StatusBadge({ state, lastStatus }: { state?: string; lastStatus?: string }) {
  const s = state || "scheduled";
  const cfg: Record<string, { color: string; bg: string; Icon: any; label: string }> = {
    scheduled:  { color: "#60a5fa", bg: "rgba(96,165,250,0.12)",  Icon: Timer,        label: "Scheduled" },
    running:    { color: "var(--fg-muted)", bg: "var(--primary-dim)", Icon: RefreshCw, label: "Running" },
    completed:  { color: "#22c55e", bg: "rgba(34,197,94,0.12)",   Icon: CheckCircle,  label: "Completed" },
    failed:     { color: "#ef4444", bg: "rgba(239,68,68,0.12)",   Icon: XCircle,      label: "Failed" },
    paused:     { color: "#71717a", bg: "rgba(113,113,122,0.12)", Icon: Pause,        label: "Paused" },
    error:      { color: "#ef4444", bg: "rgba(239,68,68,0.12)",   Icon: AlertCircle,  label: "Error" },
  };
  const c = cfg[s] || cfg.scheduled;
  return (
    <span style={{ display:"inline-flex", alignItems:"center", gap:"4px", padding:"2px 8px", borderRadius:"10px", background:c.bg, color:c.color, fontSize:"11px", fontWeight:600 }}>
      <c.Icon size={10} strokeWidth={2.5} />
      {c.label}
    </span>
  );
}

// ── Styles ────────────────────────────────────────────────────────────────────

const C = {
  layout:   { display:"flex", height:"100%", overflow:"hidden" },
  sidebar:  { width:"260px", flexShrink:0, borderRight:"1px solid var(--border)", display:"flex", flexDirection:"column" as const, background:"var(--sidebar-bg)", overflow:"hidden" },
  sbHead:   { padding:"12px 16px", borderBottom:"1px solid var(--border)", display:"flex", alignItems:"center", justifyContent:"space-between" },
  sbTitle:  { fontSize:"12px", fontWeight:700, textTransform:"uppercase" as const, letterSpacing:"0.08em", color:"var(--fg-muted)", margin:0 },
  newBtn:   { background:"var(--btn-bg)", color:"var(--btn-fg)", border:"none", borderRadius:"5px", padding:"4px 10px", fontSize:"12px", fontWeight:700, cursor:"pointer", display:"flex", alignItems:"center", gap:"4px" },
  list:     { flex:1, overflowY:"auto" as const },
  jobRow:   (active:boolean):React.CSSProperties => ({ padding:"10px 16px", cursor:"pointer", borderLeft: active ? "2px solid var(--fg-muted)" : "2px solid transparent", background: active ? "var(--sidebar-active)" : "transparent", borderBottom:"1px solid var(--border)", transition:"background 0.1s" }),
  jobName:  (active:boolean):React.CSSProperties => ({ fontSize:"13px", fontWeight: active ? 600 : 400, color: active ? "var(--fg)" : "var(--fg-muted)", overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }),
  jobMeta:  { fontSize:"11px", color:"var(--fg-muted)", marginTop:"2px", display:"flex", alignItems:"center", gap:"6px" },
  main:     { flex:1, display:"flex", flexDirection:"column" as const, overflow:"hidden" },
  tabBar:   { display:"flex", alignItems:"center", gap:"4px", padding:"0 20px", borderBottom:"1px solid var(--border)", height:"44px", flexShrink:0, background:"var(--sidebar-bg)" },
  tab:      (active:boolean):React.CSSProperties => ({ padding:"6px 14px", borderRadius:"5px", fontSize:"13px", fontWeight: active ? 600 : 400, color: active ? "var(--fg)" : "var(--fg-muted)", background: active ? "rgba(255,255,255,0.06)" : "transparent", border:"none", cursor:"pointer", transition:"all 0.1s" }),
  content:  { flex:1, overflow:"auto", padding:"20px" },
  card:     { background:"var(--card)", border:"1px solid var(--border)", borderRadius:"8px", padding:"20px", maxWidth:"680px" },
  cardHead: { display:"flex", alignItems:"center", justifyContent:"space-between", marginBottom:"20px", flexWrap:"wrap" as const, gap:"10px" },
  h2:       { fontSize:"15px", fontWeight:700, margin:0, display:"flex", alignItems:"center", gap:"8px" },
  field:    { marginBottom:"14px" },
  label:    { display:"block", fontSize:"11px", fontWeight:700, textTransform:"uppercase", letterSpacing:"0.07em", color:"var(--fg-muted)", marginBottom:"5px" },
  input:    { width:"100%", background:"var(--input-bg)", border:"1px solid var(--border)", borderRadius:"6px", padding:"7px 10px", color:"var(--fg)", fontSize:"13px", fontFamily:"inherit" } as React.CSSProperties,
  textarea: { width:"100%", background:"var(--input-bg)", border:"1px solid var(--border)", borderRadius:"6px", padding:"8px 10px", color:"var(--fg)", fontSize:"13px", fontFamily:"inherit", resize:"vertical", minHeight:"90px" } as React.CSSProperties,
  hint:     { fontSize:"11px", color:"var(--fg-muted)", marginTop:"3px" },
  row2:     { display:"grid", gridTemplateColumns:"1fr 1fr", gap:"12px" },
  actions:  { display:"flex", gap:"8px", marginTop:"18px", flexWrap:"wrap" as const, alignItems:"center" },
  saveBtn:  (dis:boolean):React.CSSProperties => ({ background: dis ? "var(--btn-bg-disabled)" : "var(--btn-bg)", color: dis ? "var(--btn-fg-disabled)" : "var(--btn-fg)", border:"none", borderRadius:"6px", padding:"7px 16px", fontWeight:700, fontSize:"13px", cursor: dis ? "not-allowed" : "pointer", display:"flex", alignItems:"center", gap:"6px" }),
  secBtn:   { background:"transparent", color:"var(--fg-muted)", border:"1px solid var(--border)", borderRadius:"6px", padding:"7px 14px", fontSize:"13px", cursor:"pointer", display:"flex", alignItems:"center", gap:"6px" } as React.CSSProperties,
  delBtn:   { background:"transparent", color:"#ef4444", border:"1px solid #ef4444", borderRadius:"6px", padding:"7px 14px", fontSize:"13px", cursor:"pointer", display:"flex", alignItems:"center", gap:"6px", marginLeft:"auto" } as React.CSSProperties,
  empty:    { display:"flex", flexDirection:"column" as const, alignItems:"center", justifyContent:"center", height:"60%", color:"var(--fg-muted)", gap:"8px" },
  toast:    (t:"success"|"error"):React.CSSProperties => ({ position:"fixed", bottom:"20px", right:"20px", background: t==="success" ? "#064e3b" : "#450a0a", border:`1px solid ${t==="success" ? "#22c55e" : "#ef4444"}`, borderRadius:"8px", padding:"10px 16px", color: t==="success" ? "#22c55e" : "#ef4444", fontWeight:600, fontSize:"13px", zIndex:9999, boxShadow:"0 4px 20px rgba(0,0,0,0.5)" }),
  runRow:   (active:boolean):React.CSSProperties => ({ display:"flex", alignItems:"center", padding:"8px 12px", borderRadius:"6px", cursor:"pointer", background: active ? "var(--sidebar-active)" : "transparent", border: active ? "1px solid var(--border)" : "1px solid transparent", marginBottom:"4px", transition:"all 0.1s" }),
  logPre:   { background:"var(--input-bg)", border:"1px solid var(--border)", borderRadius:"6px", padding:"14px", fontSize:"12px", lineHeight:1.6, overflowX:"auto", whiteSpace:"pre-wrap", wordBreak:"break-word", marginTop:"14px", maxHeight:"500px", overflowY:"auto", color:"var(--fg-muted)" } as React.CSSProperties,
};

// ── SchedulerEditor ────────────────────────────────────────────────────────────────

function SchedulerEditor({
  job, onSaved, onDeleted, onCopy,
}: {
  job: (Partial<SchedulerJob> & { _isNew: boolean }) | SchedulerJob;
  onSaved: (j: SchedulerJob) => void;
  onDeleted: (id: string) => void;
  onCopy: (j: Partial<SchedulerJob> & { _isNew: boolean }) => void;
}) {
  const [tab, setTab] = useState<Tab>("edit");
  const [form, setForm] = useState<any>({ ...job });
  const [saving, setSaving] = useState(false);
  const [history, setHistory] = useState<HistoryRun[]>([]);
  const [histLoading, setHistLoading] = useState(false);
  const [selectedRun, setSelectedRun] = useState<string | null>(null);
  const [runContent, setRunContent] = useState<string>("");
  const [runLoading, setRunLoading] = useState(false);
  const [toast, setToast] = useState<{ type:"success"|"error"; msg:string }|null>(null);

  const isNew = (job as any)._isNew;
  const jobId = (job as SchedulerJob).id;

  useEffect(() => {
    setForm({ ...job });
    setTab("edit");
    setHistory([]);
    setSelectedRun(null);
    setRunContent("");
  }, [jobId, isNew]);

  useEffect(() => {
    if (tab === "history" && !isNew) {
      setHistLoading(true);
      api.getSchedulerHistory(jobId).then((d: any) => {
        setHistory(d.runs || []);
        setHistLoading(false);
      }).catch(() => setHistLoading(false));
    }
  }, [tab, jobId, isNew]);

  function showToast(type: "success"|"error", msg: string) {
    setToast({ type, msg });
    setTimeout(() => setToast(null), 3500);
  }

  async function handleSave() {
    setSaving(true);
    try {
      if (isNew) {
        const res = await api.createSchedulerJob({ ...form, _isNew: undefined });
        const jobs = await api.getSchedulerJobs();
        const created = (jobs.jobs || []).find((j: SchedulerJob) => j.id === res.job_id);
        if (created) { onSaved(created); showToast("success", `Created: ${created.name || created.id}`); }
      } else {
        const res = await api.updateSchedulerJob(jobId, { ...form, _isNew: undefined });
        onSaved(res.job);
        showToast("success", `Saved: ${form.name || jobId}`);
      }
    } catch (e: any) {
      showToast("error", e.message);
    }
    setSaving(false);
  }

  async function handleDelete() {
    if (!confirm(`Delete scheduled job "${form.name || jobId}"?`)) return;
    try {
      await api.deleteSchedulerJob(jobId);
      onDeleted(jobId);
    } catch (e: any) {
      showToast("error", e.message);
    }
  }

  async function loadRun(runId: string) {
    if (selectedRun === runId) { setSelectedRun(null); setRunContent(""); return; }
    setSelectedRun(runId);
    setRunLoading(true);
    try {
      const d = await api.getSchedulerRunDetail(jobId, runId);
      setRunContent(d.content || "(empty)");
    } catch (e: any) {
      setRunContent(`Error loading log: ${e.message}`);
    }
    setRunLoading(false);
  }

  const f = (field: string, val: any) => setForm((p: any) => ({ ...p, [field]: val }));

  return (
    <div style={{ height:"100%", display:"flex", flexDirection:"column" }}>
      {/* Tab bar */}
      <div style={C.tabBar}>
        <button style={C.tab(tab==="edit")}    onClick={() => setTab("edit")}>Edit</button>
        {!isNew && (
          <button style={C.tab(tab==="history")} onClick={() => setTab("history")}>
            History {history.length > 0 ? `(${history.length})` : ""}
          </button>
        )}
        {!isNew && (
          <div style={{ marginLeft:"auto", display:"flex", gap:"6px", alignItems:"center" }}>
            <StatusBadge state={(job as SchedulerJob).state} lastStatus={(job as SchedulerJob).last_status} />
          </div>
        )}
      </div>

      <div style={C.content}>
        {/* ── Edit tab ── */}
        {tab === "edit" && (
          <div style={C.card}>
            <div style={C.cardHead}>
              <h2 style={C.h2}>
                <Clock size={16} />
                {isNew ? "New Scheduled Job" : (form.name || jobId?.slice(0,8))}
              </h2>
              <label style={{ display:"flex", alignItems:"center", gap:"6px", fontSize:"13px", color:"var(--fg-muted)", cursor:"pointer" }}>
                <input type="checkbox" checked={!!form.enabled} onChange={e => f("enabled", e.target.checked)} />
                Enabled
              </label>
            </div>

            <div style={C.row2}>
              <div style={C.field}>
                <label style={C.label}>Job Name</label>
                <input style={C.input} value={form.name || ""} placeholder="Daily report" onChange={e => f("name", e.target.value)} />
              </div>
              <div style={C.field}>
                <label style={C.label}>Schedule</label>
                <input style={C.input} value={form.schedule_raw || ""} placeholder="30m  2h  0 9 * * 1-5" onChange={e => f("schedule_raw", e.target.value)} />
                <div style={C.hint}>Interval (30m), cron (0 9 * * 1-5), or ISO date</div>
              </div>
            </div>

            <div style={C.field}>
              <label style={C.label}>Prompt</label>
              <textarea style={C.textarea} value={form.prompt || ""} placeholder="Write your agent prompt here..." onChange={e => f("prompt", e.target.value)} />
            </div>

            <div style={C.row2}>
              <div style={C.field}>
                <label style={C.label}>Skill (optional)</label>
                <input style={C.input} value={form.skill || ""} placeholder="skill name" onChange={e => f("skill", e.target.value)} />
                <div style={C.hint}>Run a skill instead of a prompt</div>
              </div>
              <div style={C.field}>
                <label style={C.label}>Model (optional)</label>
                <input style={C.input} value={form.model || ""} placeholder="gpt-4o" onChange={e => f("model", e.target.value)} />
              </div>
            </div>

            <div style={C.row2}>
              <div style={C.field}>
                <label style={C.label}>Provider (optional)</label>
                <input style={C.input} value={form.provider || ""} placeholder="copilot" onChange={e => f("provider", e.target.value)} />
              </div>
              <div style={C.field}>
                <label style={C.label}>Repeat</label>
                <input style={C.input} type="number" min="1" value={form.repeat?.times ?? ""} placeholder="unlimited" onChange={e => f("repeat", { ...form.repeat, times: e.target.value ? parseInt(e.target.value) : null })} />
                <div style={C.hint}>Leave blank for unlimited runs</div>
              </div>
            </div>

            {!isNew && (
              <div style={{ padding:"10px 14px", background:"rgba(255,255,255,0.03)", borderRadius:"6px", fontSize:"12px", color:"var(--fg-muted)", display:"grid", gridTemplateColumns:"1fr 1fr", gap:"6px", marginBottom:"4px" }}>
                <span>Next run: <strong style={{ color:"var(--fg)" }}>{fmtTs((job as SchedulerJob).next_run_at)}</strong></span>
                <span>Last run: <strong style={{ color:"var(--fg)" }}>{fmtTs((job as SchedulerJob).last_run_at)}</strong></span>
                <span>Runs completed: <strong style={{ color:"var(--fg)" }}>{(job as SchedulerJob).repeat?.completed ?? 0}</strong></span>
                <span>Last status: <strong style={{ color: (job as SchedulerJob).last_status === "success" ? "#22c55e" : "#ef4444" }}>{(job as SchedulerJob).last_status || "—"}</strong></span>
              </div>
            )}

            <div style={C.actions}>
              <button style={C.saveBtn(saving)} disabled={saving} onClick={handleSave}>
                <Save size={13} />
                {saving ? "Saving…" : "Save"}
              </button>
              {!isNew && (
                <button style={C.secBtn} onClick={() => onCopy({ ...form, _isNew: true, name: `${form.name}_copy`, id: undefined, state: undefined, created_at: undefined })}>
                  <Copy size={13} /> Copy
                </button>
              )}
              {!isNew && (
                <button style={C.delBtn} onClick={handleDelete}>
                  <Trash2 size={13} /> Delete
                </button>
              )}
            </div>
          </div>
        )}

        {/* ── History tab ── */}
        {tab === "history" && (
          <div style={{ maxWidth:"720px" }}>
            <div style={{ marginBottom:"12px", display:"flex", alignItems:"center", justifyContent:"space-between" }}>
              <span style={{ fontSize:"13px", fontWeight:600 }}>Run History</span>
              <button style={C.secBtn} onClick={() => {
                setHistLoading(true);
                api.getSchedulerHistory(jobId).then((d:any) => { setHistory(d.runs||[]); setHistLoading(false); }).catch(() => setHistLoading(false));
              }}>
                <RefreshCw size={12} /> Refresh
              </button>
            </div>

            {histLoading && <div style={{ color:"var(--fg-muted)", padding:"20px" }}>Loading history…</div>}
            {!histLoading && history.length === 0 && (
              <div style={{ color:"var(--fg-muted)", padding:"20px", textAlign:"center", border:"1px dashed var(--border)", borderRadius:"6px" }}>
                No runs yet for this job.
              </div>
            )}
            {history.map(run => (
              <div key={run.run_id}>
                <div style={C.runRow(selectedRun === run.run_id)} onClick={() => loadRun(run.run_id)}>
                  <FileText size={13} style={{ flexShrink:0, color:"var(--fg-muted)", marginRight:"8px" }} />
                  <div style={{ flex:1 }}>
                    <div style={{ fontSize:"13px", fontWeight:500 }}>{fmtRunTs(run.timestamp)}</div>
                    <div style={{ fontSize:"11px", color:"var(--fg-muted)" }}>{(run.size_bytes / 1024).toFixed(1)} KB</div>
                  </div>
                  <ChevronRight size={14} style={{ color:"var(--fg-muted)", transform: selectedRun === run.run_id ? "rotate(90deg)" : "none", transition:"transform 0.15s" }} />
                </div>
                {selectedRun === run.run_id && (
                  <div style={{ marginBottom:"8px" }}>
                    {runLoading
                      ? <div style={{ padding:"12px", color:"var(--fg-muted)", fontSize:"12px" }}>Loading…</div>
                      : <pre style={C.logPre}>{runContent}</pre>
                    }
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {toast && <div style={C.toast(toast.type)}>{toast.msg}</div>}
    </div>
  );
}

// ── SchedulerPage ──────────────────────────────────────────────────────────────────

export function SchedulerPage() {
  const navigate = useNavigate();
  const [jobs, setJobs] = useState<SchedulerJob[]>([]);
  const [selected, setSelected] = useState<(Partial<SchedulerJob> & { _isNew: boolean }) | SchedulerJob | null>(null);
  const [loading, setLoading] = useState(true);
  const [runningJobs, setRunningJobs] = useState<Set<string>>(new Set());
  const [runToast, setRunToast] = useState<{ msg: string; ok: boolean } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api.getSchedulerJobs();
      setJobs(d.jobs || []);
    } catch { setJobs([]); }
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  function handleNew() { setSelected(emptyJob()); }

  function handleSaved(j: SchedulerJob) {
    setJobs(prev => {
      const exists = prev.find(p => p.id === j.id);
      return exists ? prev.map(p => p.id === j.id ? j : p) : [...prev, j];
    });
    setSelected(j);
  }

  function handleDeleted(id: string) {
    setJobs(prev => prev.filter(j => j.id !== id));
    setSelected(null);
  }

  const stateColor: Record<string, string> = {
    scheduled: "#60a5fa", running: "#f5a623", completed: "#22c55e",
    failed: "#ef4444", error: "#ef4444", paused: "#71717a",
  };

  async function handleRunNow(e: React.MouseEvent, job: SchedulerJob) {
    e.stopPropagation();
    if (runningJobs.has(job.id)) return;
    setRunningJobs(prev => new Set(prev).add(job.id));
    setRunToast(null);
    try {
      const res = await api.runSchedulerJobNow(job.id);
      setRunToast({ msg: `"${job.name}" started — opening session…`, ok: true });
      setTimeout(() => {
        navigate(`/chat?session=${res.session_id}`);
        setRunToast(null);
      }, 1200);
    } catch (err: any) {
      setRunToast({ msg: err.message || "Failed to start job", ok: false });
      setTimeout(() => setRunToast(null), 4000);
    } finally {
      setRunningJobs(prev => { const n = new Set(prev); n.delete(job.id); return n; });
    }
  }

  if (loading) return <div style={{ padding:"24px", color:"var(--fg-muted)" }}>Loading scheduled jobs…</div>;

  return (
    <div style={{ ...C.layout, height:"calc(100vh - 52px)" }}>
      {/* Run toast */}
      {runToast && (
        <div style={{
          position:"fixed", bottom:"20px", right:"20px", zIndex:9999,
          background: runToast.ok ? "#064e3b" : "#450a0a",
          border: `1px solid ${runToast.ok ? "#22c55e" : "#ef4444"}`,
          borderRadius:"8px", padding:"10px 16px",
          color: runToast.ok ? "#22c55e" : "#ef4444",
          fontWeight:600, fontSize:"13px",
          boxShadow:"0 4px 20px rgba(0,0,0,0.5)",
        }}>
          {runToast.ok ? "⚡ " : "✗ "}{runToast.msg}
        </div>
      )}
      {/* Sidebar */}
      <div style={C.sidebar}>
        <div style={C.sbHead}>
          <p style={C.sbTitle}>Scheduler</p>
          <button style={C.newBtn} onClick={handleNew}>
            <Plus size={12} /> New
          </button>
        </div>
        <div style={C.list}>
          {jobs.length === 0 && (
            <div style={{ padding:"16px", color:"var(--fg-muted)", fontSize:"13px" }}>
              No jobs configured.
            </div>
          )}
          {jobs.map(j => {
            const isActive = (selected as SchedulerJob)?.id === j.id;
            const isRunning = runningJobs.has(j.id);
            return (
              <div key={j.id} style={C.jobRow(isActive)} onClick={() => setSelected(j)}>
                <div style={{ display:"flex", alignItems:"center", justifyContent:"space-between", gap:"6px" }}>
                  <div style={{ ...C.jobName(isActive), flex:1, minWidth:0 }}>{j.name || j.id.slice(0, 10)}</div>
                  {/* Run now button */}
                  <button
                    title="Run now — opens as chat session"
                    onClick={e => handleRunNow(e, j)}
                    disabled={isRunning}
                    style={{
                      flexShrink: 0,
                      display:"flex", alignItems:"center", justifyContent:"center",
                      background: isRunning ? "var(--sidebar-active)" : "var(--btn-bg)",
                      color: isRunning ? "var(--fg-muted)" : "var(--btn-fg)",
                      border:"none", borderRadius:"4px",
                      width:"22px", height:"22px",
                      cursor: isRunning ? "wait" : "pointer",
                      transition:"opacity 0.15s",
                    }}
                  >
                    {isRunning
                      ? <RefreshCw size={11} style={{ animation:"spin 1s linear infinite" }} />
                      : <Zap size={11} />}
                  </button>
                </div>
                <div style={C.jobMeta}>
                  <span style={{ width:"6px", height:"6px", borderRadius:"50%", background: stateColor[j.state] || "#71717a", display:"inline-block", flexShrink:0 }} />
                  <span style={{ overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }}>{j.schedule_display || j.schedule_raw}</span>
                  {!j.enabled && <span style={{ color:"#71717a" }}>off</span>}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Main */}
      <div style={C.main}>
        {!selected ? (
          <div style={C.empty}>
            <Clock size={36} style={{ opacity:0.3 }} />
            <span>Select a job or click <strong>New</strong></span>
          </div>
        ) : (
          <SchedulerEditor
            key={(selected as SchedulerJob).id ?? "new"}
            job={selected as any}
            onSaved={handleSaved}
            onDeleted={handleDeleted}
            onCopy={copy => setSelected(copy)}
          />
        )}
      </div>
    </div>
  );
}


