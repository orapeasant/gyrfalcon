/**
 * FlowEventsPage — the reactive complement to the imperative flow (§10).
 *
 * Read-only observability: what happened, causally chained via `follows`.
 * Automations themselves have no REST surface (§10 note: actions are Python
 * callables, registered from code — a declarative action DSL is its own
 * feature, not built here) so this page is the visibility half only.
 */
import React, { useEffect, useState } from "react";
import { Radio, ChevronRight, ChevronDown, RefreshCw, GitBranch } from "lucide-react";
import { api } from "../lib/api";

interface FlowEvent {
  id: string;
  occurred: number;
  event: string;
  resource_id: string;
  related: { id: string; role: string }[];
  payload: Record<string, unknown>;
  follows: string | null;
}

const PAGE_SIZE = 50;

function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString();
}

function kindColor(event: string): string {
  if (event.includes(".flow-run.")) return "#456DE6";
  if (event.includes(".task-run.")) return "#a855f7";
  if (event.includes(".agent.")) return "#f59e0b";
  return "var(--fg-muted)";
}

const S = {
  page:    { padding: "20px", maxWidth: "1000px" } as React.CSSProperties,
  head:    { display: "flex", alignItems: "center", gap: "10px", marginBottom: "10px", flexWrap: "wrap" as const },
  h1:      { fontSize: "16px", fontWeight: 700, margin: 0 } as React.CSSProperties,
  filterInput: { fontSize: "12px", padding: "5px 9px", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--input-bg)", color: "var(--fg)", width: "220px" } as React.CSSProperties,
  refreshBtn: { background: "transparent", border: "1px solid var(--border)", borderRadius: "6px", padding: "5px 8px", cursor: "pointer", color: "var(--fg-muted)", display: "flex", alignItems: "center", gap: "4px", fontSize: "12px" } as React.CSSProperties,
  table:   { border: "1px solid var(--border)", borderRadius: "8px", overflow: "hidden" } as React.CSSProperties,
  th:      { textAlign: "left" as const, padding: "8px 12px", fontSize: "10.5px", fontWeight: 700, textTransform: "uppercase" as const, letterSpacing: "0.06em", color: "var(--fg-muted)", borderBottom: "1px solid var(--border)", background: "var(--card)" },
  td:      { padding: "8px 12px", fontSize: "12.5px", verticalAlign: "middle" as const },
  row:     { borderBottom: "1px solid var(--border)", cursor: "pointer" } as React.CSSProperties,
  chevron: { color: "var(--fg-muted)", flexShrink: 0 } as React.CSSProperties,
  eventName: (color: string): React.CSSProperties => ({ fontFamily: "monospace", fontSize: "12px", fontWeight: 600, color }),
  detail:  { background: "var(--card)", borderTop: "1px solid var(--border)", padding: "12px 20px" } as React.CSSProperties,
  pre:     { margin: "6px 0 0", padding: "8px 10px", fontSize: "11.5px", fontFamily: "monospace", background: "var(--input-bg)", border: "1px solid var(--border)", borderRadius: "6px", whiteSpace: "pre-wrap" as const, maxHeight: "200px", overflow: "auto" },
  chainStep: { display: "flex", alignItems: "center", gap: "6px", fontSize: "12px", padding: "3px 0" } as React.CSSProperties,
  empty:   { color: "var(--fg-muted)", fontSize: "13px", padding: "24px 0", textAlign: "center" as const },
};

export function FlowEventsPage() {
  const [events, setEvents] = useState<FlowEvent[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [eventTypeFilter, setEventTypeFilter] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [chains, setChains] = useState<Record<string, FlowEvent[] | "loading">>({});

  const load = async () => {
    setLoading(true);
    try {
      const d = await api.getFlowEvents({
        limit: PAGE_SIZE,
        event_type: eventTypeFilter.trim() || undefined,
      });
      setEvents(d.events || []);
      setTotal(d.total ?? 0);
    } catch {
      setEvents([]);
    }
    setLoading(false);
  };

  useEffect(() => { load(); }, [eventTypeFilter]);

  async function toggle(id: string) {
    if (expanded === id) { setExpanded(null); return; }
    setExpanded(id);
    if (!chains[id]) {
      setChains((p) => ({ ...p, [id]: "loading" }));
      try {
        const d = await api.getFlowEventChain(id);
        setChains((p) => ({ ...p, [id]: d.chain || [] }));
      } catch {
        setChains((p) => ({ ...p, [id]: [] }));
      }
    }
  }

  return (
    <div style={S.page}>
      <div style={S.head}>
        <h1 style={S.h1}>Events</h1>
        <span style={{ fontSize: "12px", color: "var(--fg-muted)" }}>{total}</span>
        <input
          style={S.filterInput}
          placeholder="filter by event type…"
          value={eventTypeFilter}
          onChange={(e) => setEventTypeFilter(e.target.value)}
        />
        <button style={S.refreshBtn} onClick={load}><RefreshCw size={12} /> Refresh</button>
      </div>

      {loading ? (
        <div style={S.empty}>Loading…</div>
      ) : events.length === 0 ? (
        <div style={S.empty}>
          No events recorded yet — every state transition on a persisted run
          emits one automatically.
        </div>
      ) : (
        <div style={S.table}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th style={S.th}></th>
                <th style={S.th}>Event</th>
                <th style={S.th}>Resource</th>
                <th style={S.th}>Occurred</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => {
                const isOpen = expanded === e.id;
                const chain = chains[e.id];
                const color = kindColor(e.event);
                return (
                  <React.Fragment key={e.id}>
                    <tr style={S.row} onClick={() => toggle(e.id)}>
                      <td style={{ ...S.td, width: "24px" }}>
                        {isOpen ? <ChevronDown size={13} style={S.chevron} /> : <ChevronRight size={13} style={S.chevron} />}
                      </td>
                      <td style={S.td}><span style={S.eventName(color)}>{e.event}</span></td>
                      <td style={{ ...S.td, fontFamily: "monospace", fontSize: "11px", color: "var(--fg-muted)" }}>
                        {e.resource_id.slice(0, 16)}
                      </td>
                      <td style={S.td}>{fmtTime(e.occurred)}</td>
                    </tr>
                    {isOpen && (
                      <tr>
                        <td colSpan={4} style={{ padding: 0 }}>
                          <div style={S.detail}>
                            <div style={{ fontSize: "11.5px", fontWeight: 700, display: "flex", alignItems: "center", gap: "5px" }}>
                              <Radio size={12} /> Payload
                            </div>
                            <pre style={S.pre}>{JSON.stringify(e.payload, null, 2)}</pre>

                            <div style={{ fontSize: "11.5px", fontWeight: 700, marginTop: "12px", display: "flex", alignItems: "center", gap: "5px" }}>
                              <GitBranch size={12} /> Causal chain
                            </div>
                            {chain === "loading" && <div style={{ fontSize: "12px", color: "var(--fg-muted)" }}>Loading…</div>}
                            {Array.isArray(chain) && chain.map((c, i) => (
                              <div key={c.id} style={S.chainStep}>
                                <span style={{ color: "var(--fg-muted)" }}>{i + 1}.</span>
                                <span style={{ fontFamily: "monospace", color: kindColor(c.event) }}>{c.event}</span>
                                <span style={{ color: "var(--fg-muted)", fontSize: "11px" }}>{fmtTime(c.occurred)}</span>
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
    </div>
  );
}
