/**
 * ServiceAccountsPage — Administration > Security > Service Accounts.
 *
 * A Service Account is an OAuth2 client-credentials client another
 * application uses to call Gyrfalcon's own REST API (`POST /api/oauth/token`
 * with its client_id/client_secret, then the returned bearer token on every
 * request). See `gyrfalcon/security.py`.
 */
import React, { useCallback, useEffect, useState } from "react";
import { Plus, RefreshCw, Trash2, KeyRound, Copy, Check } from "lucide-react";
import { api } from "../lib/api";

interface ServiceAccount {
  id: string;
  name: string;
  client_id: string;
  scopes: string[];
  enabled: boolean;
  created_at: string;
  last_used_at: string | null;
}

const S = {
  page:    { padding: 0, maxWidth: "900px" } as React.CSSProperties,
  head:    { display: "flex", alignItems: "center", gap: "10px", marginBottom: "6px", flexWrap: "wrap" as const },
  h1:      { fontSize: "16px", fontWeight: 700, margin: 0 } as React.CSSProperties,
  sub:     { fontSize: "12.5px", color: "var(--fg-muted)", marginBottom: "16px", maxWidth: "620px", lineHeight: 1.5 },
  btn: {
    fontSize: "12px", padding: "6px 12px", borderRadius: "6px", border: "1px solid var(--border)",
    background: "var(--card)", color: "var(--fg)", cursor: "pointer",
    display: "inline-flex", alignItems: "center", gap: "6px",
  } as React.CSSProperties,
  primaryBtn: {
    fontSize: "12px", padding: "6px 12px", borderRadius: "8px", border: "1px solid var(--btn-bg)",
    background: "var(--btn-bg)", color: "var(--btn-fg)", cursor: "pointer",
    display: "inline-flex", alignItems: "center", gap: "6px",
  } as React.CSSProperties,
  table:   { border: "1px solid var(--border)", borderRadius: "8px", overflow: "hidden", marginTop: "12px" } as React.CSSProperties,
  th:      { textAlign: "left" as const, padding: "9px 14px", fontSize: "11px", fontWeight: 700, textTransform: "uppercase" as const, letterSpacing: "0.07em", color: "var(--fg-muted)", borderBottom: "1px solid var(--border)", background: "var(--card)" },
  td:      { padding: "9px 14px", verticalAlign: "middle" as const, fontSize: "13px", borderBottom: "1px solid var(--border)" },
  mono:    { fontFamily: "monospace", fontSize: "12.5px" } as React.CSSProperties,
  scope:   { fontSize: "11px", padding: "2px 7px", borderRadius: "9px", background: "var(--sidebar-active)", marginRight: "4px", display: "inline-block" },
  actions: { display: "flex", gap: "6px", justifyContent: "flex-end" } as React.CSSProperties,
  actionBtn: (color: string): React.CSSProperties => ({
    fontSize: "11px", padding: "3px 9px", borderRadius: "5px", border: `1px solid ${color}`,
    color, background: "transparent", cursor: "pointer", display: "inline-flex", alignItems: "center", gap: "3px",
  }),
  empty:   { color: "var(--fg-muted)", fontSize: "13px", padding: "24px 0", textAlign: "center" as const },
  badge: (on: boolean): React.CSSProperties => ({
    fontSize: "11px", fontWeight: 600, padding: "2px 8px", borderRadius: "9px",
    color: on ? "var(--green)" : "var(--fg-muted)",
    background: on ? "var(--success-bg)" : "var(--sidebar-active)",
  }),
  modalOverlay: {
    position: "fixed" as const, inset: 0, background: "var(--overlay)",
    display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000,
  },
  modal: {
    background: "var(--card)", border: "1px solid var(--border)", borderRadius: "10px",
    padding: "20px", width: "420px", maxWidth: "90vw",
  } as React.CSSProperties,
  label:   { fontSize: "12px", fontWeight: 600, color: "var(--fg-muted)", display: "block", marginBottom: "4px", marginTop: "12px" },
  input: {
    width: "100%", fontSize: "13px", padding: "7px 10px", borderRadius: "6px",
    border: "1px solid var(--border)", background: "var(--input-bg)", color: "var(--fg)",
    boxSizing: "border-box" as const,
  },
  secretBox: {
    fontFamily: "monospace", fontSize: "12.5px", padding: "10px 12px", borderRadius: "6px",
    background: "var(--input-bg)", border: "1px solid var(--border)", wordBreak: "break-all" as const,
    display: "flex", alignItems: "center", gap: "8px", justifyContent: "space-between",
  } as React.CSSProperties,
};

function fmtDate(s: string | null): string {
  if (!s) return "Never";
  return new Date(s).toLocaleString();
}

/** The plaintext secret is only ever available right after create/rotate —
 * this dialog is the one and only place it's shown. */
function SecretRevealModal({ secret, onClose }: { secret: string; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <div style={S.modalOverlay} onClick={onClose}>
      <div style={S.modal} onClick={(e) => e.stopPropagation()}>
        <h3 style={{ margin: 0, fontSize: "14px" }}>Client secret</h3>
        <p style={{ fontSize: "12px", color: "var(--fg-muted)", marginTop: "6px" }}>
          Copy this now — it won't be shown again. Losing it means rotating for a new one.
        </p>
        <div style={{ ...S.secretBox, marginTop: "10px" }}>
          <span>{secret}</span>
          <button
            style={{ ...S.btn, padding: "4px 8px", flexShrink: 0 }}
            onClick={() => { navigator.clipboard.writeText(secret).catch(() => {}); setCopied(true); }}
          >
            {copied ? <Check size={13} /> : <Copy size={13} />}
          </button>
        </div>
        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "16px" }}>
          <button style={S.primaryBtn} onClick={onClose}>Done</button>
        </div>
      </div>
    </div>
  );
}

function CreateModal({ onClose, onCreated }: { onClose: () => void; onCreated: (secret: string) => void }) {
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState("");
  const [saving, setSaving] = useState(false);

  async function submit() {
    if (!name.trim()) return;
    setSaving(true);
    try {
      const res = await api.createServiceAccount({
        name: name.trim(),
        scopes: scopes.split(",").map((s) => s.trim()).filter(Boolean),
      });
      onCreated(res.client_secret);
    } catch (e: any) {
      alert(e.message || "Failed to create service account");
      setSaving(false);
    }
  }

  return (
    <div style={S.modalOverlay} onClick={onClose}>
      <div style={S.modal} onClick={(e) => e.stopPropagation()}>
        <h3 style={{ margin: 0, fontSize: "14px" }}>New service account</h3>
        <label style={S.label}>Name</label>
        <input style={S.input} value={name} onChange={(e) => setName(e.target.value)} placeholder="partner-app" autoFocus />
        <label style={S.label}>Scopes (comma-separated, optional)</label>
        <input style={S.input} value={scopes} onChange={(e) => setScopes(e.target.value)} placeholder="flow:read, flow:write" />
        <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "18px" }}>
          <button style={S.btn} onClick={onClose}>Cancel</button>
          <button style={S.primaryBtn} onClick={submit} disabled={saving || !name.trim()}>
            {saving ? "Creating…" : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}

export function ServiceAccountsPage() {
  const [accounts, setAccounts] = useState<ServiceAccount[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [revealSecret, setRevealSecret] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api.getServiceAccounts();
      setAccounts(d.service_accounts || []);
    } catch {
      setAccounts([]);
    }
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  async function toggle(a: ServiceAccount) {
    try {
      await api.toggleServiceAccount(a.id, !a.enabled);
      load();
    } catch (e: any) {
      alert(e.message || "Failed to update service account");
    }
  }

  async function rotate(a: ServiceAccount) {
    if (!confirm(`Rotate the secret for "${a.name}"? Its current secret stops working immediately.`)) return;
    try {
      const res = await api.rotateServiceAccountSecret(a.id);
      setRevealSecret(res.client_secret);
    } catch (e: any) {
      alert(e.message || "Failed to rotate secret");
    }
  }

  async function remove(a: ServiceAccount) {
    if (!confirm(`Delete service account "${a.name}"? Any application using it will stop being able to authenticate.`)) return;
    try {
      await api.deleteServiceAccount(a.id);
      load();
    } catch (e: any) {
      alert(e.message || "Failed to delete service account");
    }
  }

  return (
    <div style={S.page}>
      <div style={S.head}>
        <h1 style={S.h1}>Service Accounts</h1>
        <div style={{ marginLeft: "auto", display: "flex", gap: "8px" }}>
          <button style={S.btn} onClick={load}><RefreshCw size={12} /> Refresh</button>
          <button style={S.primaryBtn} onClick={() => setShowCreate(true)}><Plus size={13} /> New</button>
        </div>
      </div>
      <p style={S.sub}>
        Credentials other applications use to call Gyrfalcon's own API over OAuth2 —
        exchange a client_id/client_secret for a bearer token at <code>POST /api/oauth/token</code>,
        then use that token like any other API caller.
      </p>

      {loading ? (
        <div style={S.empty}>Loading…</div>
      ) : accounts.length === 0 ? (
        <div style={S.empty}>No service accounts yet — create one for an application that needs to call Gyrfalcon.</div>
      ) : (
        <div style={S.table}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th style={S.th}>Name</th>
                <th style={S.th}>Client ID</th>
                <th style={S.th}>Scopes</th>
                <th style={S.th}>Status</th>
                <th style={S.th}>Last used</th>
                <th style={S.th}></th>
              </tr>
            </thead>
            <tbody>
              {accounts.map((a) => (
                <tr key={a.id}>
                  <td style={{ ...S.td, fontWeight: 600 }}>
                    <span style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
                      <KeyRound size={13} style={{ color: "var(--fg-muted)" }} /> {a.name}
                    </span>
                  </td>
                  <td style={{ ...S.td, ...S.mono }}>{a.client_id}</td>
                  <td style={S.td}>
                    {a.scopes.length === 0
                      ? <span style={{ color: "var(--fg-muted)" }}>—</span>
                      : a.scopes.map((s) => <span key={s} style={S.scope}>{s}</span>)}
                  </td>
                  <td style={S.td}><span style={S.badge(a.enabled)}>{a.enabled ? "Enabled" : "Disabled"}</span></td>
                  <td style={S.td}>{fmtDate(a.last_used_at)}</td>
                  <td style={S.td}>
                    <div style={S.actions}>
                      <button style={S.actionBtn("var(--fg-muted)")} onClick={() => toggle(a)}>
                        {a.enabled ? "Disable" : "Enable"}
                      </button>
                      <button style={S.actionBtn("var(--primary)")} onClick={() => rotate(a)}>Rotate</button>
                      <button style={S.actionBtn("var(--red)")} onClick={() => remove(a)}><Trash2 size={11} /> Delete</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showCreate && (
        <CreateModal
          onClose={() => setShowCreate(false)}
          onCreated={(secret) => { setShowCreate(false); setRevealSecret(secret); load(); }}
        />
      )}
      {revealSecret && <SecretRevealModal secret={revealSecret} onClose={() => setRevealSecret(null)} />}
    </div>
  );
}
