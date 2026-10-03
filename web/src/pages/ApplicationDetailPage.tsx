/**
 * ApplicationDetailPage — structured read-only config view for a single application.
 * Opened by clicking an enabled app in the sidebar under "Applications".
 */
import React, { useState, useEffect } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { type Application } from "./ApplicationsManagePage";
import { Edit2, ToggleLeft, ToggleRight, ArrowLeft } from "lucide-react";

// ── style helpers ─────────────────────────────────────────────────────────────

const S = {
  page: {
    padding: 0,
    maxWidth: "820px",
    margin: "0 auto",
    color: "var(--fg)",
  } as React.CSSProperties,

  header: {
    display: "flex", alignItems: "flex-start", gap: "1rem",
    marginBottom: "1.75rem",
  } as React.CSSProperties,

  badge: (tone: "success" | "muted" | "info"): React.CSSProperties => ({
    display: "inline-block",
    padding: "2px 8px",
    borderRadius: "12px",
    fontSize: "0.72rem",
    fontWeight: 600,
    background: tone === "success" ? "var(--success-bg)" : tone === "info" ? "var(--info-bg)" : "var(--sidebar-active)",
    color: tone === "success" ? "var(--green)" : tone === "info" ? "var(--blue)" : "var(--fg-muted)",
    letterSpacing: "0.02em",
  }),

  section: {
    marginBottom: "1.5rem",
    background: "var(--card)",
    border: "1px solid var(--border)",
    borderRadius: "8px",
    overflow: "hidden",
  } as React.CSSProperties,

  sectionHead: {
    padding: "0.6rem 1rem",
    borderBottom: "1px solid var(--border)",
    fontSize: "0.75rem",
    fontWeight: 700,
    color: "var(--fg-muted)",
    textTransform: "uppercase" as const,
    letterSpacing: "0.08em",
    background: "var(--card)",
  },

  sectionBody: {
    padding: "0.85rem 1rem",
  } as React.CSSProperties,

  mono: {
    fontFamily: "Consolas, 'Courier New', monospace",
    fontSize: "0.84rem",
    color: "var(--fg)",
    background: "var(--bg)",
    border: "1px solid var(--border)",
    borderRadius: "5px",
    padding: "0.6rem 0.9rem",
    wordBreak: "break-all" as const,
  } as React.CSSProperties,

  kv: {
    display: "grid",
    gridTemplateColumns: "180px 1fr",
    rowGap: "6px",
    fontSize: "0.84rem",
  } as React.CSSProperties,

  key: {
    color: "var(--fg-muted)",
    fontFamily: "monospace",
    paddingRight: "1rem",
    paddingTop: "2px",
    fontWeight: 600,
  } as React.CSSProperties,

  tag: {
    display: "inline-block",
    padding: "2px 9px",
    borderRadius: "12px",
    fontSize: "0.75rem",
    background: "var(--sidebar-active)",
    color: "var(--fg)",
    border: "1px solid var(--border)",
    marginRight: "6px",
    marginBottom: "4px",
  } as React.CSSProperties,

  tableHead: {
    fontSize: "0.75rem", fontWeight: 600, color: "var(--fg-muted)",
    borderBottom: "1px solid var(--border)",
    padding: "4px 8px",
  } as React.CSSProperties,

  tableCell: {
    padding: "5px 8px",
    fontSize: "0.83rem",
    fontFamily: "monospace",
    borderBottom: "1px solid var(--border)",
    color: "var(--fg)",
    wordBreak: "break-all" as const,
  } as React.CSSProperties,
};

// ── page ──────────────────────────────────────────────────────────────────────

export function ApplicationDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [app, setApp] = useState<Application | null>(null);
  const [loading, setLoading] = useState(true);
  const [toggling, setToggling] = useState(false);

  useEffect(() => {
    if (!id) return;
    setLoading(true);
    api.getApplication(id)
      .then(data => { setApp(data); setLoading(false); })
      .catch(() => setLoading(false));
  }, [id]);

  async function toggleEnabled() {
    if (!app) return;
    setToggling(true);
    try {
      const updated = await api.toggleApplication(app.id, !app.enabled);
      setApp(updated);
    } catch {}
    setToggling(false);
  }

  if (loading) {
    return (
      <div style={{ color: "var(--fg-muted)" }}>Loading…</div>
    );
  }

  if (!app) {
    return (
      <div style={{ color: "var(--fg-muted)" }}>
        Application not found.{" "}
        <span
          style={{ color: "var(--primary)", cursor: "pointer" }}
          onClick={() => navigate("/applications/manage")}
        >
          ← Back to Applications
        </span>
      </div>
    );
  }

  const envEntries = Object.entries(app.env || {});
  const hasConfig = app.config && Object.keys(app.config).length > 0;

  return (
    <div style={{ overflowY: "auto", height: "100%" }}>
      <div style={S.page}>

        {/* ── Header ── */}
        <div style={S.header}>
          <div style={{ flex: 1 }}>
            <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "6px", flexWrap: "wrap" }}>
              <h2 style={{ margin: 0, fontSize: "1.3rem", fontWeight: 700, color: "var(--fg)" }}>
                ⚡ {app.name}
              </h2>
              <span style={S.badge(app.enabled ? "success" : "muted")}>
                {app.enabled ? "Enabled" : "Disabled"}
              </span>
              {app.version && (
                <span style={S.badge("info")}>v{app.version}</span>
              )}
              {(app.tags || []).map(tag => (
                <span key={tag} style={S.tag}>{tag}</span>
              ))}
            </div>
            {app.description && (
              <p style={{ margin: 0, color: "var(--fg-muted)", fontSize: "0.9rem", lineHeight: 1.5 }}>
                {app.description}
              </p>
            )}
          </div>

          {/* Action buttons */}
          <div style={{ display: "flex", gap: "8px", flexShrink: 0, alignItems: "center" }}>
            <button
              onClick={() => navigate("/applications/manage")}
              title="Back to manage"
              style={{
                display: "flex", alignItems: "center", gap: "4px",
                background: "transparent", border: "1px solid var(--border)",
                borderRadius: "6px", padding: "5px 11px",
                cursor: "pointer", fontSize: "0.8rem", color: "var(--fg-muted)",
              }}
            >
              <ArrowLeft size={13} /> Manage
            </button>
            <button
              onClick={toggleEnabled}
              disabled={toggling}
              title={app.enabled ? "Disable application" : "Enable application"}
              style={{
                display: "flex", alignItems: "center", gap: "5px",
                background: app.enabled ? "transparent" : "var(--btn-bg)",
                border: `1px solid ${app.enabled ? "var(--border)" : "var(--btn-bg)"}`,
                borderRadius: "6px", padding: "5px 11px",
                cursor: toggling ? "wait" : "pointer",
                fontSize: "0.8rem",
                color: app.enabled ? "var(--fg-muted)" : "var(--btn-fg)",
              }}
            >
              {app.enabled
                ? <><ToggleRight size={14} /> Disable</>
                : <><ToggleLeft size={14} /> Enable</>}
            </button>
            <button
              onClick={() => navigate("/applications/manage")}
              title="Edit"
              style={{
                display: "flex", alignItems: "center", gap: "5px",
                background: "var(--btn-bg)", border: "none",
                borderRadius: "6px", padding: "5px 13px",
                cursor: "pointer", fontSize: "0.8rem",
                color: "var(--btn-fg)", fontWeight: 500,
              }}
            >
              <Edit2 size={13} /> Edit
            </button>
          </div>
        </div>

        {/* ── Command ── */}
        {app.command && (
          <div style={S.section}>
            <div style={S.sectionHead}>Command</div>
            <div style={S.sectionBody}>
              <div style={S.mono}>{app.command}</div>
              {(app.args || []).length > 0 && (
                <div style={{ marginTop: "0.6rem" }}>
                  <div style={{ fontSize: "0.75rem", color: "var(--fg-muted)", marginBottom: "4px", fontWeight: 600 }}>
                    DEFAULT ARGUMENTS
                  </div>
                  {(app.args || []).map((arg, i) => (
                    <div key={i} style={{ ...S.mono, marginBottom: "3px" }}>{arg}</div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}

        {/* ── Environment Variables ── */}
        {envEntries.length > 0 && (
          <div style={S.section}>
            <div style={S.sectionHead}>Environment Variables ({envEntries.length})</div>
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr>
                    <th style={{ ...S.tableHead, textAlign: "left", width: "35%" }}>Variable</th>
                    <th style={{ ...S.tableHead, textAlign: "left" }}>Value</th>
                  </tr>
                </thead>
                <tbody>
                  {envEntries.map(([k, v]) => (
                    <tr key={k}>
                      <td style={{ ...S.tableCell, fontWeight: 600, color: "var(--primary)" }}>{k}</td>
                      <td style={S.tableCell}>{v}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* ── Extra Config ── */}
        {hasConfig && (
          <div style={S.section}>
            <div style={S.sectionHead}>Configuration</div>
            <div style={S.sectionBody}>
              <pre style={{
                margin: 0, padding: "0.75rem 1rem",
                background: "var(--bg)",
                border: "1px solid var(--border)",
                borderRadius: "5px",
                fontFamily: "Consolas, monospace",
                fontSize: "0.82rem",
                lineHeight: 1.6,
                overflowX: "auto",
                color: "var(--fg)",
              }}>
                {JSON.stringify(app.config, null, 2)}
              </pre>
            </div>
          </div>
        )}

        {/* ── Source Sessions ── */}
        {(app.source_sessions || []).length > 0 && (
          <div style={S.section}>
            <div style={S.sectionHead}>
              Source Sessions — distilled from {app.source_sessions.length} session{app.source_sessions.length !== 1 ? "s" : ""}
            </div>
            <div style={S.sectionBody}>
              {app.source_sessions.map(sid => (
                <div
                  key={sid}
                  onClick={() => navigate(`/sessions`)}
                  title="View in Sessions"
                  style={{
                    ...S.mono,
                    marginBottom: "4px",
                    cursor: "pointer",
                    color: "var(--primary)",
                    display: "inline-block",
                    marginRight: "6px",
                  }}
                >
                  {sid}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ── Metadata ── */}
        <div style={S.section}>
          <div style={S.sectionHead}>Metadata</div>
          <div style={{ ...S.sectionBody, ...S.kv }}>
            <span style={S.key}>ID</span>
            <span style={{ fontFamily: "monospace", fontSize: "0.82rem", color: "var(--fg-muted)" }}>{app.id}</span>

            {app.created_at && (
              <>
                <span style={S.key}>Created</span>
                <span style={{ fontSize: "0.84rem" }}>{new Date(app.created_at).toLocaleString()}</span>
              </>
            )}
            {app.updated_at && (
              <>
                <span style={S.key}>Last updated</span>
                <span style={{ fontSize: "0.84rem" }}>{new Date(app.updated_at).toLocaleString()}</span>
              </>
            )}
            <span style={S.key}>Status</span>
            <span style={{ fontSize: "0.84rem", color: app.enabled ? "var(--green)" : "var(--fg-muted)" }}>
              {app.enabled ? "● Enabled (visible in sidebar)" : "○ Disabled (hidden from sidebar)"}
            </span>
          </div>
        </div>

      </div>
    </div>
  );
}
