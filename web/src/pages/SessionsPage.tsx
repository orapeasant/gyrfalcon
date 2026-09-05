import React, { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { MessageSquare, Clock, ChevronRight, ChevronDown, Search, Coins, Hash, ArrowRightLeft, Trash2, ChevronLeft, ChevronsLeft, ChevronsRight } from "lucide-react";

function fmtDate(ts: string | number | undefined): string {
  if (!ts) return "—";
  try {
    const d = typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
    return d.toLocaleString();
  } catch { return String(ts); }
}

function fmtNum(n: number): string {
  if (!n) return "0";
  return n.toLocaleString();
}

interface SessionStats {
  input_tokens: number;
  output_tokens: number;
  reasoning_tokens: number;
  total_tokens: number;
  cost_usd: number;
  cost_aic: number;
  model: string;
  provider: string;
  message_count: number;
  last_active?: number;
  cost_display: { usd: number; aic: number; display: string };
}

interface TrajectoryToolCall {
  id: string;
  name: string;
  arguments: any;
  result?: string | null;
  duration?: number;
}

interface TrajectoryStep {
  index: number;
  role: string;
  content: string | null;
  at?: number;
  reasoning?: string;
  tool_calls?: TrajectoryToolCall[];
  tool_name?: string;
  orphaned?: boolean;
}

interface Trajectory {
  steps: TrajectoryStep[];
  step_count: number;
  tool_call_count: number;
  message_count: number;
}

const ROLE_COLOR: Record<string, string> = {
  user: "#456DE6",
  assistant: "#22c55e",
  system: "#a855f7",
  tool: "#f59e0b",
};

const PAGE_SIZES = [20, 50, 100];

const S = {
  page:    { padding:"20px", maxWidth:"1060px" } as React.CSSProperties,
  head:    { display:"flex", alignItems:"center", gap:"10px", marginBottom:"14px", flexWrap:"wrap" as const } as React.CSSProperties,
  h1:      { fontSize:"16px", fontWeight:700, margin:0 } as React.CSSProperties,
  cnt:     { fontSize:"12px", color:"var(--fg-muted)" } as React.CSSProperties,
  searchW: { position:"relative" as const, marginBottom:"12px" } as React.CSSProperties,
  searchI: { width:"100%", background:"var(--input-bg)", border:"1px solid var(--border)", borderRadius:"6px", padding:"7px 10px 7px 30px", color:"var(--fg)", fontSize:"13px", boxSizing:"border-box" as const } as React.CSSProperties,
  table:   { border:"1px solid var(--border)", borderRadius:"8px", overflow:"hidden" } as React.CSSProperties,
  th:      { textAlign:"left" as const, padding:"9px 14px", fontSize:"11px", fontWeight:700, textTransform:"uppercase" as const, letterSpacing:"0.07em", color:"var(--fg-muted)", borderBottom:"1px solid var(--border)", background:"var(--card)", whiteSpace:"nowrap" as const },
  td:      { padding:"10px 14px", verticalAlign:"middle" as const },
  chevron: { color:"var(--fg-muted)", cursor:"pointer", flexShrink:0 } as React.CSSProperties,
  detail:  { background:"var(--card)", borderTop:"1px solid var(--border)", padding:"16px 20px" } as React.CSSProperties,
  grid:    { display:"grid", gridTemplateColumns:"repeat(auto-fill, minmax(180px, 1fr))", gap:"12px" } as React.CSSProperties,
  statCard:{ background:"var(--input-bg)", border:"1px solid var(--border)", borderRadius:"6px", padding:"12px 14px" } as React.CSSProperties,
  statLabel:{ fontSize:"11px", fontWeight:700, textTransform:"uppercase" as const, letterSpacing:"0.06em", color:"var(--fg-muted)", marginBottom:"4px" },
  statVal: { fontSize:"18px", fontWeight:700, color:"var(--fg)" } as React.CSSProperties,
  statSub: { fontSize:"11px", color:"var(--fg-muted)", marginTop:"2px" } as React.CSSProperties,
  aicVal:  { fontSize:"18px", fontWeight:700, color:"var(--primary)" } as React.CSSProperties,
  resumeBtn:{ background:"var(--btn-bg)", color:"var(--btn-fg)", border:"none", borderRadius:"5px", padding:"5px 12px", fontSize:"12px", fontWeight:700, cursor:"pointer", whiteSpace:"nowrap" as const },

  // Trajectory
  trajHead: { display:"flex", alignItems:"center", gap:"8px", marginTop:"18px", marginBottom:"10px", cursor:"pointer", userSelect:"none" as const } as React.CSSProperties,
  trajTitle:{ fontSize:"12px", fontWeight:700, color:"var(--fg)" } as React.CSSProperties,
  trajMeta: { fontSize:"11px", color:"var(--fg-muted)" } as React.CSSProperties,
  step:     { display:"flex", gap:"10px", paddingBottom:"14px" } as React.CSSProperties,
  rail:     { display:"flex", flexDirection:"column" as const, alignItems:"center", flexShrink:0, width:"18px" } as React.CSSProperties,
  dot:      (c: string): React.CSSProperties => ({ width:"9px", height:"9px", borderRadius:"50%", background:c, marginTop:"4px", flexShrink:0 }),
  line:     { flex:1, width:"1px", background:"var(--border)", marginTop:"4px", minHeight:"8px" } as React.CSSProperties,
  stepBody: { flex:1, minWidth:0 } as React.CSSProperties,
  roleTag:  (c: string): React.CSSProperties => ({ fontSize:"10px", fontWeight:700, textTransform:"uppercase", letterSpacing:"0.06em", color:c }),
  stepText: { fontSize:"12.5px", color:"var(--fg)", whiteSpace:"pre-wrap" as const, wordBreak:"break-word" as const, marginTop:"3px", lineHeight:1.5 } as React.CSSProperties,
  reason:   { fontSize:"11.5px", color:"var(--fg-muted)", fontStyle:"italic" as const, whiteSpace:"pre-wrap" as const, marginTop:"4px", paddingLeft:"8px", borderLeft:"2px solid var(--border)" } as React.CSSProperties,
  call:     { border:"1px solid var(--border)", borderRadius:"6px", marginTop:"6px", overflow:"hidden", background:"var(--input-bg)" } as React.CSSProperties,
  callHead: { display:"flex", alignItems:"center", gap:"6px", padding:"6px 9px", cursor:"pointer", fontSize:"11.5px" } as React.CSSProperties,
  callName: { fontFamily:"monospace", fontWeight:700, color:"var(--fg)" } as React.CSSProperties,
  pre:      { margin:0, padding:"8px 9px", fontSize:"11px", fontFamily:"Consolas, 'Courier New', monospace", whiteSpace:"pre-wrap" as const, wordBreak:"break-word" as const, color:"var(--fg-muted)", borderTop:"1px solid var(--border)", maxHeight:"260px", overflow:"auto" } as React.CSSProperties,
  empty:   { padding:"40px", textAlign:"center" as const, color:"var(--fg-muted)", border:"1px dashed var(--border)", borderRadius:"8px" } as React.CSSProperties,
  iconBtn: (danger?: boolean): React.CSSProperties => ({
    display:"flex", alignItems:"center", justifyContent:"center",
    background:"transparent", border:"1px solid var(--border)", borderRadius:"5px",
    padding:"4px 7px", cursor:"pointer",
    color: danger ? "#ef4444" : "var(--fg-muted)",
    transition:"background 0.1s, color 0.1s",
  }),
  pageBtn: (active?: boolean, disabled?: boolean): React.CSSProperties => ({
    background: active ? "var(--btn-bg)" : "transparent",
    color: active ? "var(--btn-fg)" : disabled ? "var(--fg-subtle)" : "var(--fg-muted)",
    border: "1px solid var(--border)",
    borderRadius:"5px", padding:"4px 9px", fontSize:"12px",
    cursor: disabled ? "default" : "pointer", fontWeight: active ? 600 : 400,
    opacity: disabled ? 0.4 : 1,
  }),
};

export function SessionsPage() {
  const [sessions, setSessions]   = useState<any[]>([]);
  const [total, setTotal]         = useState(0);
  const [page, setPage]           = useState(0);
  const [pageSize, setPageSize]   = useState(50);
  const [loading, setLoading]     = useState(true);
  const [search, setSearch]       = useState("");
  const [expanded, setExpanded]   = useState<string | null>(null);
  const [stats, setStats]         = useState<Record<string, SessionStats | "loading" | "error">>({});
  const [traj, setTraj]           = useState<Record<string, Trajectory | "loading" | "error">>({});
  const [trajOpen, setTrajOpen]   = useState<Set<string>>(new Set());
  const [openCalls, setOpenCalls] = useState<Set<string>>(new Set());
  const [deleting, setDeleting]   = useState<string | null>(null);   // id being confirmed
  const [deletingPage, setDeletingPage] = useState(false);
  const [busyIds, setBusyIds]     = useState<Set<string>>(new Set());
  const navigate = useNavigate();

  const load = useCallback(async (pg = page, ps = pageSize) => {
    setLoading(true);
    try {
      const d = await api.getSessions(ps, pg * ps);
      setSessions(d.sessions || []);
      setTotal(d.total ?? 0);
    } catch { setSessions([]); }
    setLoading(false);
  }, [page, pageSize]);

  useEffect(() => { load(page, pageSize); }, [page, pageSize]);

  function changePage(newPage: number) {
    setPage(newPage);
    setExpanded(null);
    setDeleting(null);
  }

  function changePageSize(ps: number) {
    setPageSize(ps);
    setPage(0);
    setExpanded(null);
    setDeleting(null);
  }

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  // Client-side search filters the current page
  const filtered = search
    ? sessions.filter(s =>
        (s.title || "").toLowerCase().includes(search.toLowerCase()) ||
        (s.id || "").startsWith(search.toLowerCase()))
    : sessions;

  function handleResume(sessionId: string) {
    navigate(`/chat?session=${sessionId}`);
  }

  async function handleExpand(sessionId: string) {
    if (expanded === sessionId) { setExpanded(null); return; }
    setExpanded(sessionId);
    if (!stats[sessionId]) {
      setStats(p => ({ ...p, [sessionId]: "loading" }));
      try {
        const d = await api.getSessionStats(sessionId);
        setStats(p => ({ ...p, [sessionId]: d }));
      } catch {
        setStats(p => ({ ...p, [sessionId]: "error" }));
      }
    }
  }

  async function toggleTrajectory(sessionId: string) {
    const open = new Set(trajOpen);
    if (open.has(sessionId)) {
      open.delete(sessionId);
      setTrajOpen(open);
      return;
    }
    open.add(sessionId);
    setTrajOpen(open);
    if (!traj[sessionId]) {
      setTraj(p => ({ ...p, [sessionId]: "loading" }));
      try {
        const d = await api.getSessionTrajectory(sessionId);
        setTraj(p => ({ ...p, [sessionId]: d }));
      } catch {
        setTraj(p => ({ ...p, [sessionId]: "error" }));
      }
    }
  }

  function toggleCall(key: string) {
    setOpenCalls(prev => {
      const n = new Set(prev);
      n.has(key) ? n.delete(key) : n.add(key);
      return n;
    });
  }

  async function deleteOne(id: string) {
    setBusyIds(s => new Set(s).add(id));
    try {
      await api.deleteSession(id);
      setDeleting(null);
      setExpanded(null);
      // Re-load; if page is now empty and not first page, go back one
      const newTotal = total - 1;
      const maxPage  = Math.max(0, Math.ceil(newTotal / pageSize) - 1);
      const targetPage = Math.min(page, maxPage);
      if (targetPage !== page) setPage(targetPage);
      else await load(page, pageSize);
      setTotal(newTotal);
    } catch {}
    setBusyIds(s => { const n = new Set(s); n.delete(id); return n; });
  }

  async function deleteCurrentPage() {
    const ids = filtered.map(s => s.id);
    if (!ids.length) return;
    setDeletingPage(false);
    try {
      await api.deleteSessionsBatch(ids);
      setExpanded(null);
      const newTotal = Math.max(0, total - ids.length);
      const maxPage  = Math.max(0, Math.ceil(newTotal / pageSize) - 1);
      setPage(Math.min(page, maxPage));
      setTotal(newTotal);
      await load(Math.min(page, maxPage), pageSize);
    } catch {}
  }

  // ── Pagination bar ──────────────────────────────────────────────────────────
  function PaginationBar() {
    const start = page * pageSize + 1;
    const end   = Math.min((page + 1) * pageSize, total);

    // Page numbers: show at most 5 around current page
    const pages: (number | "...")[] = [];
    if (totalPages <= 7) {
      for (let i = 0; i < totalPages; i++) pages.push(i);
    } else {
      pages.push(0);
      if (page > 2) pages.push("...");
      for (let i = Math.max(1, page - 1); i <= Math.min(totalPages - 2, page + 1); i++) pages.push(i);
      if (page < totalPages - 3) pages.push("...");
      pages.push(totalPages - 1);
    }

    return (
      <div style={{ display:"flex", alignItems:"center", gap:"6px", marginTop:"14px", flexWrap:"wrap" }}>
        {/* Page size */}
        <select
          value={pageSize}
          onChange={e => changePageSize(Number(e.target.value))}
          style={{
            background:"var(--input-bg)", border:"1px solid var(--border)",
            borderRadius:"5px", padding:"4px 8px", fontSize:"12px",
            color:"var(--fg)", cursor:"pointer",
          }}
        >
          {PAGE_SIZES.map(ps => (
            <option key={ps} value={ps}>{ps} / page</option>
          ))}
        </select>

        <span style={{ fontSize:"12px", color:"var(--fg-muted)", marginRight:"4px" }}>
          {total > 0 ? `${start}–${end} of ${total.toLocaleString()}` : "0 sessions"}
        </span>

        {/* First */}
        <button style={S.pageBtn(false, page === 0)} disabled={page === 0} onClick={() => changePage(0)} title="First page">
          <ChevronsLeft size={13} />
        </button>
        {/* Prev */}
        <button style={S.pageBtn(false, page === 0)} disabled={page === 0} onClick={() => changePage(page - 1)} title="Previous page">
          <ChevronLeft size={13} />
        </button>

        {/* Page numbers */}
        {pages.map((p, i) =>
          p === "..." ? (
            <span key={`e${i}`} style={{ fontSize:"12px", color:"var(--fg-muted)", padding:"0 2px" }}>…</span>
          ) : (
            <button key={p} style={S.pageBtn(p === page)} onClick={() => changePage(p as number)}>
              {(p as number) + 1}
            </button>
          )
        )}

        {/* Next */}
        <button style={S.pageBtn(false, page >= totalPages - 1)} disabled={page >= totalPages - 1} onClick={() => changePage(page + 1)} title="Next page">
          <ChevronRight size={13} />
        </button>
        {/* Last */}
        <button style={S.pageBtn(false, page >= totalPages - 1)} disabled={page >= totalPages - 1} onClick={() => changePage(totalPages - 1)} title="Last page">
          <ChevronsRight size={13} />
        </button>
      </div>
    );
  }

  if (loading && sessions.length === 0)
    return <div style={{ padding:"24px", color:"var(--fg-muted)" }}>⠋ Loading sessions…</div>;

  return (
    <div style={S.page}>
      {/* ── Header ── */}
      <div style={S.head}>
        <h1 style={S.h1}>Sessions</h1>
        <span style={S.cnt}>{total.toLocaleString()} total</span>

        <div style={{ flex:1 }} />

        {/* Delete page button */}
        {filtered.length > 0 && (
          deletingPage ? (
            <>
              <span style={{ fontSize:"12px", color:"var(--fg-muted)" }}>
                Delete {filtered.length} session{filtered.length !== 1 ? "s" : ""} on this page?
              </span>
              <button
                onClick={deleteCurrentPage}
                style={{ ...S.iconBtn(true), padding:"4px 12px", fontSize:"12px", gap:"4px" }}
              >
                <Trash2 size={12} /> Yes, delete
              </button>
              <button
                onClick={() => setDeletingPage(false)}
                style={{ ...S.iconBtn(), padding:"4px 10px", fontSize:"12px" }}
              >
                Cancel
              </button>
            </>
          ) : (
            <button
              onClick={() => setDeletingPage(true)}
              style={{ ...S.iconBtn(true), padding:"4px 12px", fontSize:"12px", gap:"5px" }}
              title={`Delete all ${filtered.length} sessions on this page`}
            >
              <Trash2 size={13} /> Delete page
            </button>
          )
        )}
      </div>

      {/* ── Search ── */}
      <div style={S.searchW}>
        <Search size={13} style={{ position:"absolute", left:"10px", top:"50%", transform:"translateY(-50%)", color:"var(--fg-muted)" }} />
        <input value={search} onChange={e => setSearch(e.target.value)}
          placeholder="Filter by title or ID…" style={S.searchI} />
      </div>

      {/* ── Table ── */}
      {filtered.length === 0 ? (
        <div style={S.empty}>{search ? "No sessions match your filter." : "No sessions yet. Start a chat to create one."}</div>
      ) : (
        <div style={S.table}>
          <table style={{ width:"100%", borderCollapse:"collapse" }}>
            <thead>
              <tr>
                {["ID", "Title", "Model", "Source", "Last Active", "", ""].map((h, i) => (
                  <th key={i} style={S.th}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered.map((s: any) => {
                const isOpen   = expanded === s.id;
                const isBusy   = busyIds.has(s.id);
                const isDel    = deleting === s.id;
                const st       = stats[s.id];

                return (
                  <React.Fragment key={s.id}>
                    <tr
                      style={{ borderBottom: isOpen ? "none" : "1px solid var(--border)" }}
                      onMouseEnter={e => (e.currentTarget.style.background="rgba(255,255,255,0.02)")}
                      onMouseLeave={e => (e.currentTarget.style.background="transparent")}
                    >
                      {/* ID */}
                      <td style={{ ...S.td, fontFamily:"monospace", fontSize:"12px", color:"var(--primary)", cursor:"pointer", whiteSpace:"nowrap" }}
                        onClick={() => handleResume(s.id)} title="Resume session">
                        {s.id?.slice(0, 8)}
                      </td>

                      {/* Title */}
                      <td style={{ ...S.td, maxWidth:"260px", cursor:"pointer" }} onClick={() => handleResume(s.id)}>
                        <div style={{ display:"flex", alignItems:"center", gap:"7px" }}>
                          <MessageSquare size={13} style={{ flexShrink:0, color:"var(--fg-muted)" }} />
                          <span style={{ fontWeight:500, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap", fontSize:"13px", color:"var(--fg)" }}>
                            {s.title || <span style={{ color:"var(--fg-muted)", fontStyle:"italic" }}>Untitled</span>}
                          </span>
                        </div>
                      </td>

                      <td style={{ ...S.td, fontSize:"12px", color:"var(--fg-muted)", fontFamily:"monospace", whiteSpace:"nowrap" }}>{s.model || "—"}</td>
                      <td style={{ ...S.td, fontSize:"12px", color:"var(--fg-muted)", whiteSpace:"nowrap" }}>{s.source || "—"}</td>
                      <td style={{ ...S.td, fontSize:"12px", color:"var(--fg-muted)", whiteSpace:"nowrap" }}>
                        <span style={{ display:"flex", alignItems:"center", gap:"4px" }}>
                          <Clock size={11} />{fmtDate(s.last_active || s.created_at)}
                        </span>
                      </td>

                      {/* Delete column */}
                      <td style={{ ...S.td, width:"1%", whiteSpace:"nowrap", paddingRight:"6px" }}>
                        {isDel ? (
                          <span style={{ display:"flex", alignItems:"center", gap:"5px" }}>
                            <button onClick={() => deleteOne(s.id)} disabled={isBusy}
                              style={{ ...S.iconBtn(true), padding:"3px 8px", fontSize:"11px", gap:"3px" }}>
                              <Trash2 size={11} /> Yes
                            </button>
                            <button onClick={() => setDeleting(null)}
                              style={{ ...S.iconBtn(), padding:"3px 6px", fontSize:"11px" }}>
                              No
                            </button>
                          </span>
                        ) : (
                          <button
                            onClick={() => setDeleting(s.id)}
                            disabled={isBusy}
                            style={S.iconBtn(true)}
                            title="Delete session"
                          >
                            <Trash2 size={13} />
                          </button>
                        )}
                      </td>

                      {/* Expand chevron */}
                      <td style={{ ...S.td, width:"36px" }} onClick={() => handleExpand(s.id)} title="View stats">
                        <div style={{ cursor:"pointer", display:"flex", justifyContent:"center" }}>
                          {isOpen
                            ? <ChevronDown size={15} style={{ color:"var(--primary)" }} />
                            : <ChevronRight size={15} style={S.chevron} />}
                        </div>
                      </td>
                    </tr>

                    {/* ── Expanded detail ── */}
                    {isOpen && (
                      <tr style={{ borderBottom:"1px solid var(--border)" }}>
                        <td colSpan={7} style={{ padding:0 }}>
                          <div style={S.detail}>
                            {st === "loading" && <span style={{ color:"var(--fg-muted)", fontSize:"13px" }}>⠋ Loading stats…</span>}
                            {st === "error"   && <span style={{ color:"#ef4444", fontSize:"13px" }}>Failed to load stats.</span>}
                            {st && st !== "loading" && st !== "error" && (() => {
                              const d = st as SessionStats;
                              return (
                                <div>
                                  <div style={{ display:"flex", alignItems:"center", justifyContent:"space-between", marginBottom:"12px" }}>
                                    <span style={{ fontSize:"12px", fontWeight:600, color:"var(--fg-muted)" }}>
                                      {d.model || "unknown model"}
                                      {d.provider && <span style={{ marginLeft:"8px", background:"var(--sidebar-active)", color:"var(--fg-muted)", fontSize:"11px", padding:"1px 6px", borderRadius:"4px" }}>{d.provider}</span>}
                                    </span>
                                    <button style={S.resumeBtn} onClick={() => handleResume(s.id)}>↩ Resume in Chat</button>
                                  </div>
                                  <div style={S.grid}>
                                    <div style={S.statCard}>
                                      <div style={S.statLabel}><Coins size={10} style={{ marginRight:"4px" }} />Cost (AIC)</div>
                                      <div style={S.aicVal}>{d.cost_aic.toFixed(4)}</div>
                                      <div style={S.statSub}>${d.cost_usd.toFixed(6)} USD</div>
                                    </div>
                                    <div style={S.statCard}>
                                      <div style={S.statLabel}><Hash size={10} style={{ marginRight:"4px" }} />Total Tokens</div>
                                      <div style={S.statVal}>{fmtNum(d.total_tokens)}</div>
                                      <div style={S.statSub}>{fmtNum(d.message_count)} messages</div>
                                    </div>
                                    <div style={S.statCard}>
                                      <div style={S.statLabel}><ArrowRightLeft size={10} style={{ marginRight:"4px" }} />Input Tokens</div>
                                      <div style={S.statVal}>{fmtNum(d.input_tokens)}</div>
                                      <div style={S.statSub}>{d.total_tokens ? ((d.input_tokens / d.total_tokens) * 100).toFixed(1) + "%" : "—"} of total</div>
                                    </div>
                                    <div style={S.statCard}>
                                      <div style={S.statLabel}><ArrowRightLeft size={10} style={{ marginRight:"4px" }} />Output Tokens</div>
                                      <div style={S.statVal}>{fmtNum(d.output_tokens)}</div>
                                      <div style={S.statSub}>{d.total_tokens ? ((d.output_tokens / d.total_tokens) * 100).toFixed(1) + "%" : "—"} of total</div>
                                    </div>
                                    {d.reasoning_tokens > 0 && (
                                      <div style={S.statCard}>
                                        <div style={S.statLabel}>Reasoning Tokens</div>
                                        <div style={S.statVal}>{fmtNum(d.reasoning_tokens)}</div>
                                        <div style={S.statSub}>chain-of-thought</div>
                                      </div>
                                    )}
                                    <div style={S.statCard}>
                                      <div style={S.statLabel}>Last Active</div>
                                      <div style={{ fontSize:"13px", fontWeight:600, color:"var(--fg)" }}>{fmtDate(d.last_active)}</div>
                                    </div>
                                  </div>

                                  {/* Trajectory — agent steps with their tool calls */}
                                  <div style={S.trajHead} onClick={() => toggleTrajectory(s.id)}>
                                    {trajOpen.has(s.id)
                                      ? <ChevronDown size={13} style={S.chevron} />
                                      : <ChevronRight size={13} style={S.chevron} />}
                                    <span style={S.trajTitle}>Trajectory</span>
                                    {(() => {
                                      const t = traj[s.id];
                                      if (!t || t === "loading" || t === "error") return null;
                                      return (
                                        <span style={S.trajMeta}>
                                          {t.step_count} step{t.step_count === 1 ? "" : "s"}
                                          {t.tool_call_count > 0 && ` · ${t.tool_call_count} tool call${t.tool_call_count === 1 ? "" : "s"}`}
                                        </span>
                                      );
                                    })()}
                                  </div>

                                  {trajOpen.has(s.id) && (() => {
                                    const t = traj[s.id];
                                    if (t === "loading") return <span style={S.trajMeta}>⠋ Loading trajectory…</span>;
                                    if (t === "error")   return <span style={{ color:"#ef4444", fontSize:"12px" }}>Failed to load trajectory.</span>;
                                    if (!t) return null;
                                    if (!t.steps.length) return <span style={S.trajMeta}>No steps recorded for this session.</span>;

                                    return (
                                      <div>
                                        {t.steps.map((step, si) => {
                                          const color = ROLE_COLOR[step.role] || "var(--fg-muted)";
                                          const last  = si === t.steps.length - 1;
                                          return (
                                            <div key={step.index} style={S.step}>
                                              <div style={S.rail}>
                                                <div style={S.dot(color)} />
                                                {!last && <div style={S.line} />}
                                              </div>
                                              <div style={S.stepBody}>
                                                <div style={{ display:"flex", alignItems:"center", gap:"8px", flexWrap:"wrap" }}>
                                                  <span style={S.roleTag(color)}>
                                                    {step.orphaned ? `tool result · ${step.tool_name || "unknown"}` : step.role}
                                                  </span>
                                                  {step.at && <span style={S.trajMeta}>{fmtDate(step.at)}</span>}
                                                </div>

                                                {step.reasoning && <div style={S.reason}>{step.reasoning}</div>}
                                                {step.content && <div style={S.stepText}>{step.content}</div>}

                                                {step.tool_calls?.map((c, ci) => {
                                                  const key = `${s.id}:${step.index}:${ci}`;
                                                  const open = openCalls.has(key);
                                                  return (
                                                    <div key={key} style={S.call}>
                                                      <div style={S.callHead} onClick={() => toggleCall(key)}>
                                                        {open
                                                          ? <ChevronDown size={12} style={S.chevron} />
                                                          : <ChevronRight size={12} style={S.chevron} />}
                                                        <span style={S.callName}>{c.name}</span>
                                                        {typeof c.duration === "number" && (
                                                          <span style={S.trajMeta}>{c.duration.toFixed(2)}s</span>
                                                        )}
                                                        {c.result == null && (
                                                          <span style={{ ...S.trajMeta, color:"#f59e0b" }}>no result</span>
                                                        )}
                                                      </div>
                                                      {open && (
                                                        <>
                                                          <pre style={S.pre}>
                                                            {typeof c.arguments === "string"
                                                              ? c.arguments
                                                              : JSON.stringify(c.arguments, null, 2)}
                                                          </pre>
                                                          {c.result != null && <pre style={S.pre}>{c.result}</pre>}
                                                        </>
                                                      )}
                                                    </div>
                                                  );
                                                })}
                                              </div>
                                            </div>
                                          );
                                        })}
                                      </div>
                                    );
                                  })()}
                                </div>
                              );
                            })()}
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

      {/* ── Pagination ── */}
      {total > 0 && <PaginationBar />}
    </div>
  );
}

