/**
 * SecretStorePage — Administration > Security > Secret Store.
 *
 * Secrets Gyrfalcon itself uses to call *out* to other systems (an API key
 * for a third-party service a tool or skill needs, say). Values are
 * write-only from this UI's point of view — never returned by the API once
 * saved, only replaced. See `gyrfalcon/security.py`.
 */
import React, { useCallback, useEffect, useState } from "react";
import { Plus, RefreshCw, Trash2, Lock, Pencil } from "lucide-react";
import { api } from "../lib/api";

interface SecretEntry {
  id: string;
  name: string;
  description: string;
  created_at: string;
  updated_at: string;
}

const S = {
  page:    { padding: "20px", maxWidth: "900px" } as React.CSSProperties,
  head:    { display: "flex", alignItems: "center", gap: "10px", marginBottom: "6px", flexWrap: "wrap" as const },
  h1:      { fontSize: "16px", fontWeight: 700, margin: 0 } as React.CSSProperties,
  sub:     { fontSize: "12.5px", color: "var(--fg-muted)", marginBottom: "16px", maxWidth: "620px", lineHeight: 1.5 },
  btn: {
    fontSize: "12px", padding: "6px 12px", borderRadius: "6px", border: "1px solid var(--border)",
    background: "var(--card)", color: "var(--fg)", cursor: "pointer",
    display: "inline-flex", alignItems: "center", gap: "6px",
  } as React.CSSProperties,
  primaryBtn: {
    fontSize: "12px", padding: "6px 12px", borderRadius: "6px", border: "1px solid #456DE6",
    background: "#456DE6", color: "#fff", cursor: "pointer",
    display: "inline-flex", alignItems: "center", gap: "6px",
  } as React.CSSProperties,
  table:   { border: "1px solid var(--border)", borderRadius: "8px", overflow: "hidden", marginTop: "12px" } as React.CSSProperties,
  th:      { textAlign: "left" as const, padding: "9px 14px", fontSize: "11px", fontWeight: 700, textTransform: "uppercase" as const, letterSpacing: "0.07em", color: "var(--fg-muted)", borderBottom: "1px solid var(--border)", background: "var(--card)" },
  td:      { padding: "9px 14px", verticalAlign: "middle" as const, fontSize: "13px", borderBottom: "1px solid var(--border)" },
  mono:    { fontFamily: "monospace", fontSize: "12.5px" } as React.CSSProperties,
  actions: { display: "flex", gap: "6px", justifyContent: "flex-end" } as React.CSSProperties,
  actionBtn: (color: string): React.CSSProperties => ({
    fontSize: "11px", padding: "3px 9px", borderRadius: "5px", border: `1px solid ${color}`,
    color, background: "transparent", cursor: "pointer", display: "inline-flex", alignItems: "center", gap: "3px",
  }),
  empty:   { color: "var(--fg-muted)", fontSize: "13px", padding: "24px 0", textAlign: "center" as const },
  modalOverlay: {
    position: "fixed" as const, inset: 0, background: "rgba(0,0,0,0.4)",
    display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000,
  },
  modal: {
    background: "var(--bg)", border: "1px solid var(--border)", borderRadius: "10px",
    padding: "20px", width: "420px", maxWidth: "90vw",
  } as React.CSSProperties,
  label:   { fontSize: "12px", fontWeight: 600, color: "var(--fg-muted)", display: "block", marginBottom: "4px", marginTop: "12px" },
  input: {
    width: "100%", fontSize: "13px", padding: "7px 10px", borderRadius: "6px",
    border: "1px solid var(--border)", background: "var(--input-bg)", color: "var(--fg)",
    boxSizing: "border-box" as const,
  },
  hint: { fontSize: "11px", color: "var(--fg-muted)", marginTop: "4px" },
};

function fmtDate(s: string): string {
  return new Date(s).toLocaleString();
}

function SecretModal({
  initial, onClose, onSave,
}: {
  initial?: SecretEntry;
  onClose: () => void;
  onSave: (data: { name?: string; description: string; value: string }) => Promise<void>;
}) {
  const isEdit = !!initial;
  const [name, setName] = useState(initial?.name || "");
  const [description, setDescription] = useState(initial?.description || "");
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);

  async function submit() {
    if (!isEdit && !name.trim()) return;
    if (!isEdit && !value.trim()) return;
    setSaving(true);
    try {
      await onSave({ name: isEdit ? undefined : name.trim(), description, value });
    } catch (e: any) {
      alert(e.message || "Failed to save secret");
      setSaving(false);
    }
  }

  return (
    <div style={S.modalOverlay} onClick={onClose}>
      <div style={S.modal} onClick={(e) => e.stopPropagation()}>
        <h3 style={{ margin: 0, fontSize: "14px" }}>{isEdit ? `Edit "${initial!.name}"` : "New secret"}</h3>
        {!isEdit && (
          <>
            <label style={S.label}>Name</label>
            <input style={S.input} value={name} onChange={(e) => setName(e.target.value)} placeholder="stripe_api_key" autoFocus />
          </>
        )}
        <label style={S.label}>Description</label>
        <input style={S.input} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Stripe live API key, used by the billing skill" />
        <label style={S.label}>{isEdit ? "New value" : "Value"}</label>
        <input style={S.input} type="password" value={value} onChange={(e) => setValue(e.target.value)} placeholder={isEdit ? "Leave blank to keep the current value" : "sk_live_…"} />
        {isEdit && <div style={S.hint}>The current value is never shown here — leave this blank to keep it unchanged.</div>}
        <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "18px" }}>
          <button style={S.btn} onClick={onClose}>Cancel</button>
          <button style={S.primaryBtn} onClick={submit} disabled={saving || (!isEdit && (!name.trim() || !value.trim()))}>
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}

export function SecretStorePage() {
  const [secrets, setSecrets] = useState<SecretEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [editing, setEditing] = useState<SecretEntry | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api.getSecrets();
      setSecrets(d.secrets || []);
    } catch {
      setSecrets([]);
    }
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  async function remove(s: SecretEntry) {
    if (!confirm(`Delete secret "${s.name}"? Anything using it will start failing.`)) return;
    try {
      await api.deleteSecret(s.id);
      load();
    } catch (e: any) {
      alert(e.message || "Failed to delete secret");
    }
  }

  return (
    <div style={S.page}>
      <div style={S.head}>
        <h1 style={S.h1}>Secret Store</h1>
        <div style={{ marginLeft: "auto", display: "flex", gap: "8px" }}>
          <button style={S.btn} onClick={load}><RefreshCw size={12} /> Refresh</button>
          <button style={S.primaryBtn} onClick={() => setShowCreate(true)}><Plus size={13} /> New</button>
        </div>
      </div>
      <p style={S.sub}>
        Secrets Gyrfalcon uses to call other systems — an API key or token a tool or skill
        needs, looked up by name. Values are write-only: once saved, a value is never shown
        again, only replaced.
      </p>

      {loading ? (
        <div style={S.empty}>Loading…</div>
      ) : secrets.length === 0 ? (
        <div style={S.empty}>No secrets stored yet.</div>
      ) : (
        <div style={S.table}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th style={S.th}>Name</th>
                <th style={S.th}>Description</th>
                <th style={S.th}>Updated</th>
                <th style={S.th}></th>
              </tr>
            </thead>
            <tbody>
              {secrets.map((s) => (
                <tr key={s.id}>
                  <td style={{ ...S.td, fontWeight: 600 }}>
                    <span style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
                      <Lock size={13} style={{ color: "var(--fg-muted)" }} /> <span style={S.mono}>{s.name}</span>
                    </span>
                  </td>
                  <td style={{ ...S.td, color: "var(--fg-muted)" }}>{s.description || "—"}</td>
                  <td style={S.td}>{fmtDate(s.updated_at)}</td>
                  <td style={S.td}>
                    <div style={S.actions}>
                      <button style={S.actionBtn("var(--fg-muted)")} onClick={() => setEditing(s)}>
                        <Pencil size={11} /> Edit
                      </button>
                      <button style={S.actionBtn("#ef4444")} onClick={() => remove(s)}><Trash2 size={11} /> Delete</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showCreate && (
        <SecretModal
          onClose={() => setShowCreate(false)}
          onSave={async (data) => {
            await api.createSecret({ name: data.name!, description: data.description, value: data.value });
            setShowCreate(false);
            load();
          }}
        />
      )}
      {editing && (
        <SecretModal
          initial={editing}
          onClose={() => setEditing(null)}
          onSave={async (data) => {
            await api.updateSecret(editing.id, {
              description: data.description,
              value: data.value.trim() ? data.value : undefined,
            });
            setEditing(null);
            load();
          }}
        />
      )}
    </div>
  );
}
