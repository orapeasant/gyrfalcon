/**
 * FlowDefinitionsPage — registered @flow templates.
 *
 * Spec: 15-flow.md §14.3, §14.9 (pencil editor), §14.12 (Run/Edit/Duplicate/
 * Delete — Agent-card parity, applied here the way §14.10 applies it to
 * Deployments). Flows here are code (§14, the "This document" column), so a
 * definition IS the decorated function's source — Edit opens that source
 * directly rather than a form.
 *
 * Only a flow whose source lives under the user's flows directory
 * (`editable: true` from the API) carries Edit/Delete/Duplicate — a flow
 * shipped inside gyrfalcon itself has no honest "save" to offer here. Run
 * works for every registered flow regardless.
 */
import React, { useEffect, useState } from "react";
import { FileCode, RefreshCw, Tag, Play, Edit2, Copy, Trash2, X } from "lucide-react";
import { api } from "../lib/api";

interface FlowDefinition {
  name: string;
  version: string | null;
  description: string | null;
  retries: number;
  timeout_seconds: number | null;
  tags: string[];
  editable: boolean;
}

const S = {
  page:   { padding: "20px", maxWidth: "900px" } as React.CSSProperties,
  head:   { display: "flex", alignItems: "center", gap: "10px", marginBottom: "16px" } as React.CSSProperties,
  h1:     { fontSize: "16px", fontWeight: 700, margin: 0 } as React.CSSProperties,
  refreshBtn: { marginLeft: "auto", background: "transparent", border: "1px solid var(--border)", borderRadius: "6px", padding: "5px 8px", cursor: "pointer", color: "var(--fg-muted)", display: "flex", alignItems: "center", gap: "4px", fontSize: "12px" } as React.CSSProperties,
  runBanner: { display: "flex", alignItems: "center", gap: "8px", background: "rgba(34,197,94,0.1)", border: "1px solid rgba(34,197,94,0.35)", color: "#15803d", borderRadius: "7px", padding: "9px 12px", fontSize: "12.5px", marginBottom: "14px" } as React.CSSProperties,
  errBanner: { background: "rgba(239,68,68,0.1)", border: "1px solid rgba(239,68,68,0.35)", color: "#b91c1c", borderRadius: "7px", padding: "9px 12px", fontSize: "12.5px", marginBottom: "14px", wordBreak: "break-word" } as React.CSSProperties,
  card:   { border: "1px solid var(--border)", borderRadius: "8px", padding: "14px 16px", marginBottom: "10px", background: "var(--card)" } as React.CSSProperties,
  row:    { display: "flex", alignItems: "center", gap: "8px" },
  name:   { fontSize: "14px", fontWeight: 700, fontFamily: "monospace" } as React.CSSProperties,
  desc:   { fontSize: "12.5px", color: "var(--fg-muted)", marginTop: "4px" } as React.CSSProperties,
  meta:   { display: "flex", gap: "14px", marginTop: "8px", fontSize: "11.5px", color: "var(--fg-muted)" } as React.CSSProperties,
  tags:   { display: "flex", gap: "6px", marginTop: "8px", flexWrap: "wrap" as const },
  tag:    { fontSize: "10.5px", padding: "2px 7px", borderRadius: "10px", background: "var(--sidebar-active)", color: "var(--fg-muted)", display: "flex", alignItems: "center", gap: "3px" } as React.CSSProperties,
  empty:  { color: "var(--fg-muted)", fontSize: "13px", padding: "24px 0", textAlign: "center" as const },
  actions: { display: "flex", alignItems: "center", gap: "4px", marginLeft: "auto" },
  iconBtn: (danger?: boolean): React.CSSProperties => ({
    background: "transparent", border: "1px solid var(--border)", borderRadius: "5px",
    padding: "4px 7px", cursor: "pointer", color: danger ? "#ef4444" : "var(--fg-muted)",
    display: "flex", alignItems: "center",
  }),
  readOnlyTag: { fontSize: "10.5px", color: "var(--fg-muted)", fontStyle: "italic" as const },
  modalOverlay: { position: "fixed" as const, inset: 0, background: "rgba(0,0,0,0.4)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 50 },
  modal:  { background: "var(--card)", border: "1px solid var(--border)", borderRadius: "10px", padding: "20px", width: "440px", maxWidth: "90vw" } as React.CSSProperties,
  editorModal: { background: "var(--card)", border: "1px solid var(--border)", borderRadius: "10px", padding: "16px", width: "760px", maxWidth: "94vw", height: "80vh", display: "flex", flexDirection: "column" as const },
  field:  { display: "block", fontSize: "11.5px", fontWeight: 600, color: "var(--fg-muted)", marginBottom: "4px", marginTop: "12px" },
  input:  { width: "100%", background: "var(--input-bg)", border: "1px solid var(--border)", borderRadius: "6px", padding: "7px 9px", color: "var(--fg)", fontSize: "13px", boxSizing: "border-box" as const },
  hint:   { fontSize: "10.5px", color: "var(--fg-muted)", marginTop: "3px" },
  modalActions: { display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "18px" },
  cancelBtn: { fontSize: "12.5px", padding: "6px 14px", borderRadius: "6px", border: "1px solid var(--border)", background: "transparent", color: "var(--fg)", cursor: "pointer" } as React.CSSProperties,
  saveBtn: { fontSize: "12.5px", padding: "6px 14px", borderRadius: "6px", border: "none", background: "var(--btn-bg)", color: "var(--btn-fg)", cursor: "pointer", fontWeight: 600 } as React.CSSProperties,
  err:    { color: "#ef4444", fontSize: "12px", marginTop: "10px" },
  code:   { flex: 1, width: "100%", fontFamily: "Consolas, 'Courier New', monospace", fontSize: "12.5px", background: "var(--input-bg)", border: "1px solid var(--border)", borderRadius: "6px", padding: "10px", color: "var(--fg)", resize: "none" as const, boxSizing: "border-box" as const },
};

export function FlowDefinitionsPage() {
  const [definitions, setDefinitions] = useState<FlowDefinition[]>([]);
  const [loading, setLoading] = useState(true);
  const [editTarget, setEditTarget] = useState<string | null>(null);
  const [runTarget, setRunTarget] = useState<string | null>(null);
  const [dupTarget, setDupTarget] = useState<string | null>(null);
  const [runningId, setRunningId] = useState<string | null>(null);
  const [lastRun, setLastRun] = useState<{ name: string; runId: string } | null>(null);
  const [importErrors, setImportErrors] = useState<Record<string, string>>({});

  const load = async () => {
    setLoading(true);
    try {
      const d = await api.getFlowDefinitions();
      setDefinitions(d.definitions || []);
    } catch {
      setDefinitions([]);
    }
    setLoading(false);
  };

  // Refresh re-scans the flows directory rather than only re-reading the
  // in-memory registry: a file added since startup is otherwise invisible,
  // and a Refresh button that can't see it is worse than none at all.
  const rescan = async () => {
    setLoading(true);
    try {
      const d = await api.reloadFlowDefinitions();
      setDefinitions(d.definitions || []);
      setImportErrors(d.errors || {});
    } catch {
      await load();
      return;
    }
    setLoading(false);
  };

  useEffect(() => { rescan(); }, []);

  async function remove(d: FlowDefinition) {
    if (!confirm(`Delete "${d.name}"? This removes its source file and unregisters it.`)) return;
    try {
      const res = await api.deleteFlowDefinition(d.name);
      if (res.note) alert(res.note);
      await load();
    } catch (e: any) {
      alert(e.message || "Failed to delete");
    }
  }

  return (
    <div style={S.page}>
      <div style={S.head}>
        <h1 style={S.h1}>Flow Definitions</h1>
        <span style={{ fontSize: "12px", color: "var(--fg-muted)" }}>{definitions.length}</span>
        <button style={S.refreshBtn} onClick={rescan}><RefreshCw size={12} /> Refresh</button>
      </div>

      {Object.keys(importErrors).length > 0 && (
        <div style={S.errBanner}>
          <strong>{Object.keys(importErrors).length} flow file(s) failed to import</strong>
          {Object.entries(importErrors).map(([file, err]) => (
            <div key={file} style={{ marginTop: "4px" }}>
              <code>{file}</code> — {err}
            </div>
          ))}
        </div>
      )}

      {lastRun && (
        <div style={S.runBanner}>
          <Play size={13} />
          Started <strong>{lastRun.name}</strong> — run <code>{lastRun.runId.slice(0, 8)}</code>.
          {" "}See it on the <a href="/flows/instances" style={{ color: "inherit", textDecoration: "underline" }}>Instances</a> page.
          <button onClick={() => setLastRun(null)} style={{ marginLeft: "auto", background: "transparent", border: "none", cursor: "pointer", color: "inherit", display: "flex" }}>
            <X size={13} />
          </button>
        </div>
      )}

      {loading ? (
        <div style={S.empty}>Loading…</div>
      ) : definitions.length === 0 ? (
        <div style={S.empty}>
          No flow definitions registered yet.<br />
          A <code>@flow</code>-decorated function registers itself the first time its module is imported.
        </div>
      ) : (
        definitions.map((d) => (
          <div key={d.name} style={S.card}>
            <div style={S.row}>
              <FileCode size={15} style={{ color: "var(--fg-muted)" }} />
              <span style={S.name}>{d.name}</span>
              {d.version && <span style={{ fontSize: "11px", color: "var(--fg-muted)" }}>v{d.version}</span>}

              <div style={S.actions}>
                <button
                  style={{ ...S.iconBtn(), opacity: runningId === d.name ? 0.5 : 1 }}
                  onClick={() => setRunTarget(d.name)}
                  disabled={runningId === d.name}
                  title="Run now with ad-hoc parameters"
                >
                  <Play size={13} />
                </button>
                {d.editable ? (
                  <>
                    <button style={S.iconBtn()} onClick={() => setEditTarget(d.name)} title="Edit source">
                      <Edit2 size={13} />
                    </button>
                    <button style={S.iconBtn()} onClick={() => setDupTarget(d.name)} title="Duplicate">
                      <Copy size={13} />
                    </button>
                    <button style={S.iconBtn(true)} onClick={() => remove(d)} title="Delete">
                      <Trash2 size={13} />
                    </button>
                  </>
                ) : (
                  <span style={S.readOnlyTag} title="Defined outside the flows directory — not editable from here">
                    built-in
                  </span>
                )}
              </div>
            </div>
            {d.description && <div style={S.desc}>{d.description}</div>}
            <div style={S.meta}>
              <span>Retries: {d.retries}</span>
              {d.timeout_seconds != null && <span>Timeout: {d.timeout_seconds}s</span>}
            </div>
            {d.tags.length > 0 && (
              <div style={S.tags}>
                {d.tags.map((t) => (
                  <span key={t} style={S.tag}><Tag size={9} />{t}</span>
                ))}
              </div>
            )}
          </div>
        ))
      )}

      {runTarget && (
        <RunDefinitionModal
          name={runTarget}
          onClose={() => setRunTarget(null)}
          onStarted={(runId) => { setRunTarget(null); setLastRun({ name: runTarget, runId }); }}
        />
      )}
      {editTarget && (
        <SourceEditorModal
          name={editTarget}
          onClose={() => setEditTarget(null)}
          onSaved={() => { setEditTarget(null); load(); }}
        />
      )}
      {dupTarget && (
        <DuplicateModal
          name={dupTarget}
          onClose={() => setDupTarget(null)}
          onDuplicated={() => { setDupTarget(null); load(); }}
        />
      )}
    </div>
  );
}

function RunDefinitionModal({ name, onClose, onStarted }: {
  name: string; onClose: () => void; onStarted: (runId: string) => void;
}) {
  const [parametersRaw, setParametersRaw] = useState("{}");
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  async function run() {
    setError(null);
    let parameters: object;
    try {
      parameters = JSON.parse(parametersRaw || "{}");
    } catch {
      setError("Parameters must be valid JSON.");
      return;
    }
    setRunning(true);
    try {
      const res = await api.runFlowDefinitionNow(name, parameters);
      onStarted(res.run_id);
    } catch (e: any) {
      setError(String(e.message || e));
    }
    setRunning(false);
  }

  return (
    <div style={S.modalOverlay} onClick={onClose}>
      <div style={S.modal} onClick={(e) => e.stopPropagation()}>
        <div style={{ display: "flex", alignItems: "center" }}>
          <h2 style={{ fontSize: "14px", fontWeight: 700, margin: 0, fontFamily: "monospace" }}>Run {name}</h2>
          <button onClick={onClose} style={{ marginLeft: "auto", background: "transparent", border: "none", cursor: "pointer", color: "var(--fg-muted)" }}>
            <X size={16} />
          </button>
        </div>
        <label style={S.field}>Parameters (JSON)</label>
        <input style={{ ...S.input, fontFamily: "monospace" }} value={parametersRaw}
               onChange={(e) => setParametersRaw(e.target.value)} autoFocus />
        <div style={S.hint}>Runs immediately, outside any deployment or schedule.</div>
        {error && <div style={S.err}>{error}</div>}
        <div style={S.modalActions}>
          <button style={S.cancelBtn} onClick={onClose}>Cancel</button>
          <button style={S.saveBtn} onClick={run} disabled={running}>{running ? "Starting…" : "Run"}</button>
        </div>
      </div>
    </div>
  );
}

function DuplicateModal({ name, onClose, onDuplicated }: {
  name: string; onClose: () => void; onDuplicated: () => void;
}) {
  const [newName, setNewName] = useState(`${name}_copy`);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function submit() {
    setError(null);
    if (!newName.trim()) { setError("Name is required."); return; }
    setSaving(true);
    try {
      await api.duplicateFlowDefinition(name, newName.trim());
      onDuplicated();
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
            Duplicate <span style={{ fontFamily: "monospace" }}>{name}</span>
          </h2>
          <button onClick={onClose} style={{ marginLeft: "auto", background: "transparent", border: "none", cursor: "pointer", color: "var(--fg-muted)" }}>
            <X size={16} />
          </button>
        </div>
        <label style={S.field}>New name</label>
        <input style={{ ...S.input, fontFamily: "monospace" }} value={newName}
               onChange={(e) => setNewName(e.target.value)} autoFocus />
        <div style={S.hint}>Copies the source file under this name — the two flows are independent afterward.</div>
        {error && <div style={S.err}>{error}</div>}
        <div style={S.modalActions}>
          <button style={S.cancelBtn} onClick={onClose}>Cancel</button>
          <button style={S.saveBtn} onClick={submit} disabled={saving}>{saving ? "Duplicating…" : "Duplicate"}</button>
        </div>
      </div>
    </div>
  );
}

/** The pencil button (§14.9): opens the flow's source file, saves back
 * through the same import path that validates it at startup. */
function SourceEditorModal({ name, onClose, onSaved }: {
  name: string; onClose: () => void; onSaved: () => void;
}) {
  const [content, setContent] = useState("");
  const [path, setPath] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.getFlowDefinitionSource(name)
      .then((r) => { setContent(r.content); setPath(r.path); })
      .catch((e) => setError(String(e.message || e)))
      .finally(() => setLoading(false));
  }, [name]);

  async function save() {
    setError(null);
    setSaving(true);
    try {
      await api.saveFlowDefinitionSource(name, content);
      onSaved();
    } catch (e: any) {
      setError(String(e.message || e));
    }
    setSaving(false);
  }

  return (
    <div style={S.modalOverlay} onClick={onClose}>
      <div style={S.editorModal} onClick={(e) => e.stopPropagation()}>
        <div style={{ display: "flex", alignItems: "center", marginBottom: "8px" }}>
          <div>
            <h2 style={{ fontSize: "14px", fontWeight: 700, margin: 0, fontFamily: "monospace" }}>{name}</h2>
            {path && <div style={{ fontSize: "10.5px", color: "var(--fg-muted)" }}>{path}</div>}
          </div>
          <button onClick={onClose} style={{ marginLeft: "auto", background: "transparent", border: "none", cursor: "pointer", color: "var(--fg-muted)" }}>
            <X size={16} />
          </button>
        </div>
        {loading ? (
          <div style={S.empty}>Loading…</div>
        ) : (
          <textarea
            style={S.code}
            value={content}
            onChange={(e) => setContent(e.target.value)}
            spellCheck={false}
          />
        )}
        <div style={S.hint}>
          Saving re-imports this file. A file that fails to import is rolled back —
          the definition keeps working under its previous source, unchanged.
        </div>
        {error && <div style={S.err}>{error}</div>}
        <div style={S.modalActions}>
          <button style={S.cancelBtn} onClick={onClose}>Cancel</button>
          <button style={S.saveBtn} onClick={save} disabled={saving || loading}>{saving ? "Saving…" : "Save"}</button>
        </div>
      </div>
    </div>
  );
}
