/**
 * ApplicationsManagePage — CRUD dashboard for crystallized CLI applications.
 * Opened via the gear icon next to "Applications" in the sidebar.
 */
import React, { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { Plus, X, ExternalLink, Trash2 } from "lucide-react";

export interface Application {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  command: string;
  args: string[];
  env: Record<string, string>;
  tags: string[];
  version: string;
  source_sessions: string[];
  config: Record<string, any>;
  created_at?: string;
  updated_at?: string;
}

type EnvRow = { key: string; value: string };

const EMPTY_FORM = {
  name: "",
  description: "",
  enabled: true,
  command: "",
  argsText: "",       // newline-separated, joined to string[]
  tagsText: "",       // comma-separated
  version: "1.0.0",
  source_sessions_text: "",
  configText: "{}",
};

type FormState = typeof EMPTY_FORM;

function appToForm(app: Application): FormState {
  return {
    name: app.name,
    description: app.description,
    enabled: app.enabled,
    command: app.command,
    argsText: (app.args || []).join("\n"),
    tagsText: (app.tags || []).join(", "),
    version: app.version || "1.0.0",
    source_sessions_text: (app.source_sessions || []).join("\n"),
    configText: JSON.stringify(app.config || {}, null, 2),
  };
}

function formToPayload(form: FormState, envRows: EnvRow[]) {
  let config: Record<string, any> = {};
  try { config = JSON.parse(form.configText || "{}"); } catch { /* keep {} */ }
  return {
    name: form.name.trim(),
    description: form.description.trim(),
    enabled: form.enabled,
    command: form.command.trim(),
    args: form.argsText.split("\n").map(s => s.trim()).filter(Boolean),
    env: Object.fromEntries(envRows.filter(r => r.key.trim()).map(r => [r.key.trim(), r.value])),
    tags: form.tagsText.split(",").map(s => s.trim()).filter(Boolean),
    version: form.version.trim() || "1.0.0",
    source_sessions: form.source_sessions_text.split("\n").map(s => s.trim()).filter(Boolean),
    config,
  };
}

// ── sub-component: key-value env editor ──────────────────────────────────────

function EnvEditor({ rows, onChange }: { rows: EnvRow[]; onChange: (rows: EnvRow[]) => void }) {
  function setRow(i: number, key: string, value: string) {
    const next = rows.map((r, idx) => idx === i ? { key, value } : r);
    onChange(next);
  }
  function addRow() { onChange([...rows, { key: "", value: "" }]); }
  function removeRow(i: number) { onChange(rows.filter((_, idx) => idx !== i)); }

  return (
    <div>
      {rows.map((row, i) => (
        <div key={i} style={{ display: "flex", gap: "6px", marginBottom: "4px", alignItems: "center" }}>
          <input
            value={row.key}
            onChange={e => setRow(i, e.target.value, row.value)}
            placeholder="KEY"
            style={inputStyle({ width: "40%", fontFamily: "monospace", fontSize: "0.82rem" })}
          />
          <input
            value={row.value}
            onChange={e => setRow(i, row.key, e.target.value)}
            placeholder="value"
            style={inputStyle({ flex: 1, fontFamily: "monospace", fontSize: "0.82rem" })}
          />
          <button
            onClick={() => removeRow(i)}
            style={{
              background: "transparent", border: "none", cursor: "pointer",
              color: "#ef4444", padding: "2px 4px", display: "flex", alignItems: "center",
            }}
            title="Remove"
          >
            <X size={13} />
          </button>
        </div>
      ))}
      <button onClick={addRow} style={ghostBtn}>
        <Plus size={12} /> Add variable
      </button>
    </div>
  );
}

// ── styles helpers ────────────────────────────────────────────────────────────

function inputStyle(extra?: React.CSSProperties): React.CSSProperties {
  return {
    border: "1px solid var(--border)",
    borderRadius: "5px",
    padding: "5px 8px",
    fontSize: "0.85rem",
    background: "var(--bg)",
    color: "var(--fg)",
    outline: "none",
    boxSizing: "border-box",
    ...extra,
  };
}

const ghostBtn: React.CSSProperties = {
  display: "inline-flex", alignItems: "center", gap: "4px",
  background: "transparent",
  border: "1px dashed var(--border)",
  borderRadius: "5px",
  padding: "3px 10px",
  cursor: "pointer",
  fontSize: "0.8rem",
  color: "var(--fg-muted)",
  marginTop: "4px",
};

const label: React.CSSProperties = {
  display: "block",
  fontSize: "0.78rem", fontWeight: 600,
  color: "var(--fg-muted)",
  marginBottom: "4px",
  textTransform: "uppercase",
  letterSpacing: "0.05em",
};

function Field({ name, children }: { name: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: "1.1rem" }}>
      <label style={label}>{name}</label>
      {children}
    </div>
  );
}

// ── main page ─────────────────────────────────────────────────────────────────

export function ApplicationsManagePage() {
  const [apps, setApps] = useState<Application[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<Application | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState<FormState>({ ...EMPTY_FORM });
  const [envRows, setEnvRows] = useState<EnvRow[]>([]);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);
  const navigate = useNavigate();

  useEffect(() => { load(); }, []);

  async function load(keepId?: string) {
    setLoading(true);
    try {
      const data = await api.getApplications();
      const list: Application[] = data.applications || [];
      setApps(list);
      if (keepId) {
        const refreshed = list.find(a => a.id === keepId);
        if (refreshed) selectApp(refreshed);
      }
    } catch { setApps([]); }
    setLoading(false);
  }

  function selectApp(app: Application) {
    setSelected(app);
    setCreating(false);
    setForm(appToForm(app));
    setEnvRows(Object.entries(app.env || {}).map(([key, value]) => ({ key, value })));
    setMsg(null);
    setDeleteConfirm(null);
  }

  function startCreate() {
    setSelected(null);
    setCreating(true);
    setForm({ ...EMPTY_FORM });
    setEnvRows([]);
    setMsg(null);
    setDeleteConfirm(null);
  }

  function cancelPanel() {
    setSelected(null);
    setCreating(false);
    setMsg(null);
    setDeleteConfirm(null);
  }

  function patch(key: keyof FormState, value: any) {
    setForm(f => ({ ...f, [key]: value }));
  }

  async function save() {
    if (!form.name.trim()) return;
    setSaving(true);
    setMsg(null);
    const payload = formToPayload(form, envRows);
    try {
      if (creating) {
        const created = await api.createApplication(payload);
        setMsg({ text: `"${created.name}" created`, ok: true });
        setCreating(false);
        await load(created.id);
      } else if (selected) {
        await api.updateApplication(selected.id, payload);
        setMsg({ text: "Saved", ok: true });
        await load(selected.id);
      }
    } catch (e: any) {
      setMsg({ text: e.message || "Save failed", ok: false });
    }
    setSaving(false);
  }

  async function deleteApp(id: string) {
    try {
      await api.deleteApplication(id);
      cancelPanel();
      await load();
    } catch (e: any) {
      setMsg({ text: e.message || "Delete failed", ok: false });
    }
  }

  async function toggleApp(app: Application, e: React.MouseEvent) {
    e.stopPropagation();
    try {
      await api.toggleApplication(app.id, !app.enabled);
      setApps(prev => prev.map(a => a.id === app.id ? { ...a, enabled: !a.enabled } : a));
      if (selected?.id === app.id) {
        setSelected(prev => prev ? { ...prev, enabled: !prev.enabled } : null);
        setForm(f => ({ ...f, enabled: !f.enabled }));
      }
    } catch {}
  }

  const hasPanel = selected || creating;

  return (
    <div style={{ display: "flex", height: "calc(100vh - 3.25rem)", overflow: "hidden" }}>

      {/* ── LEFT: app list ── */}
      <div style={{
        width: "270px", minWidth: "200px", flexShrink: 0,
        borderRight: "1px solid var(--border)",
        display: "flex", flexDirection: "column",
        background: "var(--sidebar-bg)",
      }}>
        <div style={{
          padding: "0.65rem 1rem",
          borderBottom: "1px solid var(--border)",
          display: "flex", alignItems: "center", justifyContent: "space-between",
        }}>
          <span style={{ fontWeight: 600, fontSize: "0.88rem", color: "var(--fg)" }}>
            All Applications
          </span>
          <button
            onClick={startCreate}
            style={{
              display: "flex", alignItems: "center", gap: "4px",
              background: "var(--btn-bg)", color: "var(--btn-fg)",
              border: "none", borderRadius: "5px",
              padding: "4px 10px", cursor: "pointer", fontSize: "0.8rem",
            }}
          >
            <Plus size={13} /> New
          </button>
        </div>

        <div style={{ flex: 1, overflowY: "auto" }}>
          {loading && (
            <div style={{ padding: "1rem", color: "var(--fg-muted)", fontSize: "0.85rem" }}>Loading…</div>
          )}
          {!loading && apps.length === 0 && (
            <div style={{
              padding: "2rem 1rem", color: "var(--fg-muted)", fontSize: "0.85rem",
              textAlign: "center", lineHeight: 1.6,
            }}>
              <div style={{ fontSize: "2rem", opacity: 0.3, marginBottom: "0.5rem" }}>⚡</div>
              No applications yet.<br />Click <strong>New</strong> to create one.
            </div>
          )}
          {apps.map(app => {
            const isActive = !creating && selected?.id === app.id;
            return (
              <div
                key={app.id}
                onClick={() => selectApp(app)}
                style={{
                  padding: "0.6rem 1rem",
                  cursor: "pointer",
                  borderLeft: isActive ? "3px solid var(--fg-muted)" : "3px solid transparent",
                  background: isActive ? "var(--sidebar-active)" : "transparent",
                  borderBottom: "1px solid var(--border)",
                  display: "flex", alignItems: "flex-start", gap: "8px",
                }}
              >
                {/* enabled dot — click toggles */}
                <span
                  title={app.enabled ? "Enabled — click to disable" : "Disabled — click to enable"}
                  onClick={e => toggleApp(app, e)}
                  style={{
                    marginTop: "5px", flexShrink: 0,
                    width: "8px", height: "8px", borderRadius: "50%",
                    background: app.enabled ? "#22c55e" : "var(--fg-subtle)",
                    display: "inline-block", cursor: "pointer",
                    transition: "background 0.15s",
                  }}
                />
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ fontWeight: isActive ? 600 : 400, fontSize: "0.87rem", color: "var(--fg)", marginBottom: "2px" }}>
                    {app.name}
                    {app.version && (
                      <span style={{ marginLeft: "6px", fontSize: "0.72rem", color: "var(--fg-muted)" }}>v{app.version}</span>
                    )}
                  </div>
                  {app.command ? (
                    <div style={{
                      fontSize: "0.73rem", color: "var(--fg-muted)", fontFamily: "monospace",
                      overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                    }}>
                      {app.command}
                    </div>
                  ) : app.description ? (
                    <div style={{
                      fontSize: "0.73rem", color: "var(--fg-muted)",
                      overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                    }}>
                      {app.description}
                    </div>
                  ) : null}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* ── RIGHT: form panel ── */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden", minWidth: 0 }}>
        {!hasPanel ? (
          <div style={{
            flex: 1, display: "flex", flexDirection: "column",
            alignItems: "center", justifyContent: "center",
            color: "var(--fg-muted)", gap: "0.5rem",
          }}>
            <span style={{ fontSize: "2.5rem", opacity: 0.25 }}>⚡</span>
            <span style={{ fontSize: "0.9rem" }}>
              Select an application to edit, or click <strong>New</strong> to create one.
            </span>
          </div>
        ) : (
          <>
            {/* Toolbar */}
            <div style={{
              padding: "0.55rem 1.25rem",
              borderBottom: "1px solid var(--border)",
              display: "flex", alignItems: "center", gap: "0.5rem",
              background: "var(--sidebar-bg)", flexWrap: "wrap",
            }}>
              <span style={{ fontWeight: 600, fontSize: "0.88rem", flex: 1, color: "var(--fg)" }}>
                {creating ? "✨ New Application" : `⚡ ${selected?.name}`}
              </span>

              {selected && !creating && (
                <button
                  onClick={() => navigate(`/applications/${selected.id}`)}
                  title="View detail page"
                  style={{
                    display: "flex", alignItems: "center", gap: "4px",
                    background: "transparent", border: "1px solid var(--border)",
                    borderRadius: "5px", padding: "3px 9px",
                    cursor: "pointer", fontSize: "0.78rem", color: "var(--fg-muted)",
                  }}
                >
                  <ExternalLink size={12} /> Detail
                </button>
              )}

              {selected && !creating && (
                deleteConfirm === selected.id ? (
                  <>
                    <span style={{ fontSize: "0.8rem", color: "var(--fg-muted)" }}>Confirm delete?</span>
                    <button
                      onClick={() => deleteApp(selected.id)}
                      style={{
                        background: "#ef4444", color: "#fff", border: "none",
                        borderRadius: "5px", padding: "3px 10px",
                        cursor: "pointer", fontSize: "0.78rem",
                      }}
                    >Yes</button>
                    <button
                      onClick={() => setDeleteConfirm(null)}
                      style={{
                        background: "transparent", border: "1px solid var(--border)",
                        borderRadius: "5px", padding: "3px 8px",
                        cursor: "pointer", fontSize: "0.78rem", color: "var(--fg)",
                      }}
                    >No</button>
                  </>
                ) : (
                  <button
                    onClick={() => setDeleteConfirm(selected.id)}
                    title="Delete application"
                    style={{
                      display: "flex", alignItems: "center",
                      background: "transparent", border: "1px solid var(--border)",
                      borderRadius: "5px", padding: "3px 8px",
                      cursor: "pointer", color: "#ef4444",
                    }}
                  >
                    <Trash2 size={13} />
                  </button>
                )
              )}

              <button
                onClick={cancelPanel}
                style={{
                  display: "flex", alignItems: "center",
                  background: "transparent", border: "1px solid var(--border)",
                  borderRadius: "5px", padding: "3px 8px",
                  cursor: "pointer", color: "var(--fg-muted)",
                }}
                title="Cancel"
              >
                <X size={13} />
              </button>

              <button
                onClick={save}
                disabled={saving || !form.name.trim()}
                style={{
                  background: form.name.trim() ? "var(--btn-bg)" : "var(--sidebar-active)",
                  color: form.name.trim() ? "var(--btn-fg)" : "var(--fg-muted)",
                  border: "none", borderRadius: "5px",
                  padding: "4px 16px", cursor: saving || !form.name.trim() ? "default" : "pointer",
                  fontSize: "0.85rem", fontWeight: 500,
                }}
              >
                {saving ? "Saving…" : "Save"}
              </button>

              {msg && (
                <span style={{ fontSize: "0.78rem", color: msg.ok ? "#22c55e" : "#ef4444" }}>
                  {msg.ok ? "✓" : "✗"} {msg.text}
                </span>
              )}
            </div>

            {/* Form */}
            <div style={{ flex: 1, overflowY: "auto", padding: "1.25rem 1.5rem" }}>
              <div style={{ maxWidth: "680px" }}>

                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0 1rem" }}>
                  <Field name="Name *">
                    <input
                      value={form.name}
                      onChange={e => patch("name", e.target.value)}
                      placeholder="my-cli-app"
                      style={inputStyle({ width: "100%" })}
                    />
                  </Field>
                  <Field name="Version">
                    <input
                      value={form.version}
                      onChange={e => patch("version", e.target.value)}
                      placeholder="1.0.0"
                      style={inputStyle({ width: "100%" })}
                    />
                  </Field>
                </div>

                <Field name="Description">
                  <textarea
                    value={form.description}
                    onChange={e => patch("description", e.target.value)}
                    rows={2}
                    placeholder="What this application does…"
                    style={inputStyle({ width: "100%", resize: "vertical", lineHeight: "1.5" })}
                  />
                </Field>

                <Field name="Command">
                  <input
                    value={form.command}
                    onChange={e => patch("command", e.target.value)}
                    placeholder="python my_app.py"
                    style={inputStyle({ width: "100%", fontFamily: "monospace", fontSize: "0.85rem" })}
                  />
                </Field>

                <Field name="Arguments (one per line)">
                  <textarea
                    value={form.argsText}
                    onChange={e => patch("argsText", e.target.value)}
                    rows={3}
                    placeholder={"--config config.yaml\n--verbose"}
                    style={inputStyle({ width: "100%", fontFamily: "monospace", fontSize: "0.82rem", resize: "vertical" })}
                  />
                </Field>

                <Field name="Environment Variables">
                  <EnvEditor rows={envRows} onChange={setEnvRows} />
                </Field>

                <Field name="Tags (comma-separated)">
                  <input
                    value={form.tagsText}
                    onChange={e => patch("tagsText", e.target.value)}
                    placeholder="data, automation, reporting"
                    style={inputStyle({ width: "100%" })}
                  />
                </Field>

                <Field name="Source Sessions (one session ID per line)">
                  <textarea
                    value={form.source_sessions_text}
                    onChange={e => patch("source_sessions_text", e.target.value)}
                    rows={3}
                    placeholder="Session IDs this application was distilled from…"
                    style={inputStyle({ width: "100%", fontFamily: "monospace", fontSize: "0.78rem", resize: "vertical" })}
                  />
                </Field>

                <Field name="Extra Config (JSON)">
                  <textarea
                    value={form.configText}
                    onChange={e => patch("configText", e.target.value)}
                    rows={4}
                    style={inputStyle({ width: "100%", fontFamily: "monospace", fontSize: "0.8rem", resize: "vertical" })}
                  />
                </Field>

                <Field name="Enabled">
                  <label style={{ display: "flex", alignItems: "center", gap: "8px", cursor: "pointer" }}>
                    <input
                      type="checkbox"
                      checked={form.enabled}
                      onChange={e => patch("enabled", e.target.checked)}
                      style={{ width: "15px", height: "15px", accentColor: "var(--primary)", cursor: "pointer" }}
                    />
                    <span style={{ fontSize: "0.85rem", color: "var(--fg)" }}>
                      Show in sidebar navigation
                    </span>
                  </label>
                </Field>

              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
