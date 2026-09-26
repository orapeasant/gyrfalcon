/**
 * AgentsPage — create, configure, and invoke AI agents.
 * Each agent has: instructions (system prompt), attached MCPs, skills, toolsets,
 * model override, and an optional gateway endpoint.
 */
import React, { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import {
  Plus, Trash2, X, Play, RefreshCw, Copy, Eye, EyeOff,
  ExternalLink, Globe, Edit2, Users, LayoutGrid, List,
} from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────────────

export interface Agent {
  id: string;
  name: string;
  description: string;
  instructions: string;
  enabled: boolean;
  model: string;
  provider: string;
  max_iterations: number;
  mcp_servers: string[];
  skills: string[];
  enabled_toolsets: string[];
  plugins: string[];
  gateway: { enabled: boolean; api_key: string };
  tags: string[];
  created_at?: string;
  updated_at?: string;
}

interface McpServer  { name: string; enabled: boolean; connected: boolean }
interface SkillItem  { name: string; description: string }
interface ToolsetItem { name: string; description?: string }

// ── styles ────────────────────────────────────────────────────────────────────
function inputSt(extra?: React.CSSProperties): React.CSSProperties {
  return {
    border: "1px solid var(--border)", borderRadius: "6px",
    padding: "6px 10px", fontSize: "0.85rem",
    background: "var(--bg)", color: "var(--fg)",
    outline: "none", boxSizing: "border-box" as const,
    width: "100%", ...extra,
  };
}
const label: React.CSSProperties = {
  display: "block", fontSize: "0.75rem", fontWeight: 700,
  color: "var(--fg-muted)", marginBottom: "4px",
  textTransform: "uppercase" as const, letterSpacing: "0.06em",
};
function Field({ name, children }: { name: string; children: React.ReactNode }) {
  return <div style={{ marginBottom: "1rem" }}><label style={label}>{name}</label>{children}</div>;
}

// ── Multi-select checkbox list ────────────────────────────────────────────────
function MultiSelect({
  items, selected, onChange, label: lbl,
}: {
  items: string[]; selected: string[]; onChange: (v: string[]) => void; label?: string;
}) {
  function toggle(item: string) {
    onChange(selected.includes(item)
      ? selected.filter(x => x !== item)
      : [...selected, item]);
  }
  return (
    <div style={{
      border: "1px solid var(--border)", borderRadius: "6px",
      maxHeight: "140px", overflowY: "auto",
      background: "var(--bg)",
    }}>
      {items.length === 0 && (
        <div style={{ padding: "8px 12px", fontSize: "0.8rem", color: "var(--fg-muted)" }}>
          No {lbl ?? "items"} available
        </div>
      )}
      {items.map(item => (
        <label key={item} style={{
          display: "flex", alignItems: "center", gap: "8px",
          padding: "5px 10px", cursor: "pointer", fontSize: "0.83rem",
          borderBottom: "1px solid var(--border)",
          background: selected.includes(item) ? "var(--sidebar-active)" : "transparent",
        }}>
          <input
            type="checkbox"
            checked={selected.includes(item)}
            onChange={() => toggle(item)}
            style={{ accentColor: "var(--fg)", cursor: "pointer" }}
          />
          {item}
        </label>
      ))}
    </div>
  );
}

// ── Agent Card ────────────────────────────────────────────────────────────────
function AgentCard({ agent, layout, onInvoke, onEdit, onDelete, onCopy }: {
  agent: Agent;
  layout: "cards" | "list";
  onInvoke: (a: Agent) => void;
  onEdit:   (a: Agent) => void;
  onDelete: (a: Agent) => void;
  onCopy:   (a: Agent) => void;
}) {
  const [showKey, setShowKey] = useState(false);
  const [confirmDel, setConfirmDel] = useState(false);
  const gatewayUrl = `${window.location.origin}/api/gateway/agents/${agent.id}`;

  const iconBtn = (danger = false): React.CSSProperties => ({
    background: "transparent",
    border: "1px solid var(--border)",
    borderRadius: "5px",
    padding: "4px 7px",
    cursor: "pointer",
    color: danger ? "#ef4444" : "var(--fg-muted)",
    display: "flex", alignItems: "center", justifyContent: "center",
    flexShrink: 0,
  });

  return (
    <div style={{
      background: "var(--sidebar-bg)", border: "1px solid var(--border)",
      borderRadius: layout === "cards" ? "7px" : "0", padding: layout === "cards" ? "0.55rem" : "0.45rem 0.65rem",
      boxSizing: "border-box",
      display: layout === "cards" ? "flex" : "grid",
      flexDirection: layout === "cards" ? "column" : undefined,
      gridTemplateColumns: layout === "list" ? "minmax(200px, 1.1fr) minmax(150px, 1fr) minmax(180px, 1fr)" : undefined,
      gridTemplateAreas: layout === "list" ? '"identity tags gateway"' : undefined,
      alignItems: layout === "list" ? "center" : undefined,
      gap: "8px",
      aspectRatio: layout === "cards" ? "1 / 1" : undefined,
      minHeight: layout === "cards" ? "140px" : "58px",
      overflow: layout === "cards" ? "auto" : "hidden",
    }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "flex-start", gap: "8px", gridArea: layout === "list" ? "identity" : undefined, minWidth: 0 }}>
        {/* Avatar */}
        <div style={{
          width: "36px", height: "36px", borderRadius: "8px",
          background: agent.enabled ? "#456DE6" : "var(--fg-muted)",
          display: "flex", alignItems: "center", justifyContent: "center",
          color: "#fff", fontWeight: 700, fontSize: "14px", flexShrink: 0,
        }}>
          {agent.name.charAt(0).toUpperCase()}
        </div>

        {/* Name + badges */}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 700, fontSize: "0.92rem", color: "var(--fg)", display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" }}>
            {agent.name}
            <span style={{
              fontSize: "0.68rem", padding: "1px 6px", borderRadius: "10px",
              background: agent.enabled ? "rgba(34,197,94,0.12)" : "var(--sidebar-active)",
              color: agent.enabled ? "#22c55e" : "var(--fg-muted)",
              border: `1px solid ${agent.enabled ? "rgba(34,197,94,0.3)" : "var(--border)"}`,
            }}>
              {agent.enabled ? "enabled" : "disabled"}
            </span>
            {agent.gateway?.enabled && (
              <span style={{ fontSize: "0.68rem", padding: "1px 6px", borderRadius: "10px", background: "rgba(69,109,230,0.12)", color: "#456DE6", border: "1px solid rgba(69,109,230,0.3)" }}>
                <Globe size={9} style={{ display: "inline", marginRight: "2px" }} />gateway
              </span>
            )}
          </div>
          {agent.description && (
            <div style={{ fontSize: "0.78rem", color: "var(--fg-muted)", marginTop: "2px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{agent.description}</div>
          )}
        </div>

        {/* Action buttons: Run | Edit | Copy | Delete */}
        <div style={{ display: "flex", alignItems: "center", gap: "4px", flexShrink: 0 }}>
          <button
            onClick={() => onInvoke(agent)}
            disabled={!agent.enabled}
            title="Run agent"
            style={{ ...iconBtn(), color: agent.enabled ? "var(--fg)" : "var(--fg-muted)", opacity: agent.enabled ? 1 : 0.4, cursor: agent.enabled ? "pointer" : "default" }}
          >
            <Play size={13} />
          </button>
          <button onClick={() => onEdit(agent)} title="Edit agent" style={iconBtn()}>
            <Edit2 size={13} />
          </button>
          <button onClick={() => onCopy(agent)} title="Duplicate agent" style={iconBtn()}>
            <Copy size={13} />
          </button>
          {confirmDel ? (
            <>
              <button
                onClick={() => { onDelete(agent); setConfirmDel(false); }}
                title="Confirm delete"
                style={{ ...iconBtn(true), padding: "3px 8px", fontSize: "0.72rem", gap: "3px" }}
              >
                <Trash2 size={11} /> Yes
              </button>
              <button
                onClick={() => setConfirmDel(false)}
                title="Cancel"
                style={{ ...iconBtn(), padding: "3px 7px", fontSize: "0.72rem" }}
              >
                ✕
              </button>
            </>
          ) : (
            <button onClick={() => setConfirmDel(true)} title="Delete agent" style={iconBtn(true)}>
              <Trash2 size={13} />
            </button>
          )}
        </div>
      </div>

      {/* Chips */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: "4px", gridArea: layout === "list" ? "tags" : undefined, overflow: "hidden", maxHeight: layout === "cards" ? "58px" : "72px" }}>
        {(agent.skills || []).map(s => (
          <span key={s} style={{ fontSize: "0.72rem", padding: "1px 7px", borderRadius: "10px", background: "var(--sidebar-active)", color: "var(--fg-muted)", border: "1px solid var(--border)" }}>📚 {s}</span>
        ))}
        {(agent.mcp_servers || []).map(m => (
          <span key={m} style={{ fontSize: "0.72rem", padding: "1px 7px", borderRadius: "10px", background: "var(--sidebar-active)", color: "var(--fg-muted)", border: "1px solid var(--border)" }}>🖥 {m}</span>
        ))}
        {(agent.enabled_toolsets || []).map(t => (
          <span key={t} style={{ fontSize: "0.72rem", padding: "1px 7px", borderRadius: "10px", background: "var(--sidebar-active)", color: "var(--fg-muted)", border: "1px solid var(--border)" }}>🔧 {t}</span>
        ))}
        {agent.model && (
          <span style={{ fontSize: "0.72rem", padding: "1px 7px", borderRadius: "10px", background: "var(--sidebar-active)", color: "var(--fg-muted)", border: "1px solid var(--border)" }}>{agent.model}</span>
        )}
      </div>

      {/* Gateway */}
      {agent.gateway?.enabled && (
        <div style={{ background: "var(--bg)", border: "1px solid var(--border)", borderRadius: "6px", padding: "8px 10px", fontSize: "0.78rem", gridArea: layout === "list" ? "gateway" : undefined, minWidth: 0, overflow: "hidden" }}>
          <div style={{ color: "var(--fg-muted)", marginBottom: "4px", display: "flex", alignItems: "center", gap: "4px" }}>
            <Globe size={11} /> Gateway Endpoint
          </div>
          <div style={{ fontFamily: "monospace", fontSize: "0.75rem", color: "var(--blue)", wordBreak: "break-all", display: "flex", alignItems: "center", gap: "6px" }}>
            <span style={{ flex: 1 }}>{gatewayUrl}</span>
            <button onClick={() => navigator.clipboard?.writeText(gatewayUrl)} title="Copy URL"
              style={{ background: "transparent", border: "none", cursor: "pointer", color: "var(--fg-muted)", display: "flex" }}>
              <Copy size={11} />
            </button>
          </div>
          {agent.gateway.api_key && (
            <div style={{ marginTop: "4px", display: "flex", alignItems: "center", gap: "6px" }}>
              <span style={{ color: "var(--fg-muted)", fontSize: "0.72rem" }}>API Key:</span>
              <code style={{ fontFamily: "monospace", fontSize: "0.72rem", flex: 1 }}>
                {showKey ? agent.gateway.api_key : "••••••••••••••••"}
              </code>
              <button onClick={() => setShowKey(v => !v)} style={{ background: "transparent", border: "none", cursor: "pointer", color: "var(--fg-muted)", display: "flex" }}>
                {showKey ? <EyeOff size={11} /> : <Eye size={11} />}
              </button>
              <button onClick={() => navigator.clipboard?.writeText(agent.gateway.api_key)} title="Copy key"
                style={{ background: "transparent", border: "none", cursor: "pointer", color: "var(--fg-muted)", display: "flex" }}>
                <Copy size={11} />
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Invoke Dialog ─────────────────────────────────────────────────────────────
function InvokeDialog({ agent, onClose }: { agent: Agent; onClose: () => void }) {
  const navigate = useNavigate();
  const [message, setMessage] = useState("");
  const [running, setRunning]   = useState(false);
  const [result, setResult]     = useState<{ session_id: string } | null>(null);

  async function run() {
    if (!message.trim()) return;
    setRunning(true);
    try {
      const res = await api.invokeAgent(agent.id, message.trim());
      setResult(res);
      // Navigate immediately so WebSocket subscribes before agent sends events
      onClose();
      navigate(`/chat?session=${res.session_id}`);
    } catch (e: any) {
      alert(e.message || "Invocation failed");
    }
    setRunning(false);
  }

  return (
    <div style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.55)",
      display: "flex", alignItems: "center", justifyContent: "center",
      zIndex: 2000,
    }} onClick={onClose}>
      <div style={{
        background: "var(--sidebar-bg)", border: "1px solid var(--border)",
        borderRadius: "12px", padding: "1.5rem", width: "480px", maxWidth: "90vw",
        boxShadow: "0 16px 48px rgba(0,0,0,0.5)",
      }} onClick={e => e.stopPropagation()}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "1rem" }}>
          <span style={{ fontWeight: 700, fontSize: "0.95rem" }}>
            ⚡ Run: {agent.name}
          </span>
          <button onClick={onClose} style={{ background: "transparent", border: "none", cursor: "pointer", color: "var(--fg-muted)", display: "flex" }}>
            <X size={16} />
          </button>
        </div>
        {result ? (
          <div style={{ color: "#22c55e", fontSize: "0.88rem", textAlign: "center", padding: "1rem 0" }}>
            ✓ Agent started — opening session…
          </div>
        ) : (
          <>
            <textarea
              value={message}
              onChange={e => setMessage(e.target.value)}
              placeholder={`Message to ${agent.name}…`}
              rows={4}
              style={{ ...inputSt(), resize: "vertical", lineHeight: 1.5 }}
              autoFocus
              onKeyDown={e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) run(); }}
            />
            <div style={{ fontSize: "0.73rem", color: "var(--fg-muted)", marginTop: "4px", marginBottom: "12px" }}>
              Ctrl+Enter to run
            </div>
            <button onClick={run} disabled={running || !message.trim()} style={{
              width: "100%", background: message.trim() ? "var(--btn-bg)" : "var(--sidebar-active)",
              color: message.trim() ? "var(--btn-fg)" : "var(--fg-muted)",
              border: "none", borderRadius: "7px", padding: "8px",
              cursor: running || !message.trim() ? "default" : "pointer",
              fontWeight: 600, fontSize: "0.88rem",
              display: "flex", alignItems: "center", justifyContent: "center", gap: "6px",
            }}>
              {running ? <><RefreshCw size={13} style={{ animation: "spin 1s linear infinite" }} /> Running…</> : <><Play size={13} /> Run</>}
            </button>
          </>
        )}
      </div>
    </div>
  );
}

// ── Editor form ───────────────────────────────────────────────────────────────
type FormState = {
  name: string; description: string; instructions: string;
  enabled: boolean; model: string; provider: string; max_iterations: number;
  mcp_servers: string[]; skills: string[]; enabled_toolsets: string[];
  plugins: string[]; gateway_enabled: boolean; tags: string;
};

function emptyForm(): FormState {
  return {
    name: "", description: "", instructions: "", enabled: true,
    model: "", provider: "", max_iterations: 30,
    mcp_servers: [], skills: [], enabled_toolsets: [], plugins: [],
    gateway_enabled: false, tags: "",
  };
}

function agentToForm(a: Agent): FormState {
  return {
    name: a.name, description: a.description, instructions: a.instructions,
    enabled: a.enabled, model: a.model, provider: a.provider,
    max_iterations: a.max_iterations ?? 30,
    mcp_servers: a.mcp_servers || [], skills: a.skills || [],
    enabled_toolsets: a.enabled_toolsets || [], plugins: a.plugins || [],
    gateway_enabled: a.gateway?.enabled ?? false,
    tags: (a.tags || []).join(", "),
  };
}

// ── Main page ─────────────────────────────────────────────────────────────────
export function AgentsPage() {
  const [agents, setAgents]           = useState<Agent[]>([]);
  const [loading, setLoading]         = useState(true);
  const [selected, setSelected]       = useState<Agent | null>(null);
  const [creating, setCreating]       = useState(false);
  const [form, setForm]               = useState<FormState>(emptyForm());
  const [saving, setSaving]           = useState(false);
  const [msg, setMsg]                 = useState<{ text: string; ok: boolean } | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);
  const [invokeTarget, setInvokeTarget] = useState<Agent | null>(null);
  const [regenConfirm, setRegenConfirm] = useState(false);

  // Available options
  const [mcpList, setMcpList]     = useState<string[]>([]);
  const [skillList, setSkillList] = useState<string[]>([]);
  const [toolsets, setToolsets]   = useState<string[]>([]);

  // View mode: "list" (card grid) | "edit"
  const [view, setView] = useState<"list" | "edit">("list");
  const [agentLayout, setAgentLayout] = useState<"cards" | "list">(() =>
    localStorage.getItem("gyrfalcon-agents-layout") === "list" ? "list" : "cards"
  );

  useEffect(() => {
    localStorage.setItem("gyrfalcon-agents-layout", agentLayout);
  }, [agentLayout]);

  useEffect(() => { load(); loadOptions(); }, []);

  async function load(keepId?: string) {
    setLoading(true);
    try {
      const d = await api.getAgents();
      const list: Agent[] = d.agents || [];
      setAgents(list);
      if (keepId) {
        const r = list.find(a => a.id === keepId);
        if (r) { setSelected(r); setForm(agentToForm(r)); }
      }
    } catch { setAgents([]); }
    setLoading(false);
  }

  async function loadOptions() {
    try {
      const [mcpData, skillData, tsData] = await Promise.all([
        api.getMcpServers().catch(() => ({ servers: [] })),
        api.getSkills().catch(() => ({ skills: [] })),
        api.getToolsets().catch(() => ({ toolsets: {} })),
      ]);
      setMcpList((mcpData.servers || []).map((s: any) => s.name));
      setSkillList((skillData.skills || []).map((s: any) => s.name));
      setToolsets(Object.keys(tsData.toolsets || {}));
    } catch {}
  }

  function patch(key: keyof FormState, val: any) {
    setForm(f => ({ ...f, [key]: val }));
  }

  function startCreate() {
    setCreating(true); setSelected(null);
    setForm(emptyForm()); setMsg(null); setDeleteConfirm(null);
    setView("edit");
  }

  function selectAgent(a: Agent) {
    setSelected(a); setCreating(false);
    setForm(agentToForm(a)); setMsg(null); setDeleteConfirm(null);
    setView("edit");
  }

  function cancelEdit() {
    setSelected(null); setCreating(false);
    setMsg(null); setView("list");
  }

  async function save() {
    if (!form.name.trim()) return;
    setSaving(true); setMsg(null);
    const payload = {
      name: form.name.trim(), description: form.description.trim(),
      instructions: form.instructions.trim(), enabled: form.enabled,
      model: form.model.trim(), provider: form.provider.trim(),
      max_iterations: form.max_iterations,
      mcp_servers: form.mcp_servers, skills: form.skills,
      enabled_toolsets: form.enabled_toolsets, plugins: form.plugins,
      gateway_enabled: form.gateway_enabled,
      tags: form.tags.split(",").map(s => s.trim()).filter(Boolean),
    };
    try {
      if (creating) {
        const created = await api.createAgent(payload);
        setMsg({ text: `"${created.name}" created`, ok: true });
        setCreating(false);
        await load(created.id);
      } else if (selected) {
        await api.updateAgent(selected.id, payload);
        setMsg({ text: "Saved", ok: true });
        await load(selected.id);
      }
    } catch (e: any) {
      setMsg({ text: e.message || "Save failed", ok: false });
    }
    setSaving(false);
  }

  async function deleteAgent(id: string) {
    try {
      await api.deleteAgent(id);
      setSelected(null); setDeleteConfirm(null); setView("list");
      await load();
    } catch (e: any) {
      setMsg({ text: e.message || "Delete failed", ok: false });
    }
  }

  async function regenKey() {
    if (!selected) return;
    try {
      const r = await api.regenerateAgentKey(selected.id);
      setMsg({ text: "API key regenerated", ok: true });
      setRegenConfirm(false);
      await load(selected.id);
    } catch (e: any) {
      setMsg({ text: e.message || "Failed", ok: false });
    }
  }

  const hasEditor = selected || creating;

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", overflow: "hidden" }}>

      {/* ── Header bar ── */}
      <div style={{
        padding: "0.65rem 1.25rem",
        borderBottom: "1px solid var(--border)",
        display: "flex", alignItems: "center", gap: "10px",
        background: "var(--sidebar-bg)", flexShrink: 0,
      }}>
        <span style={{ fontWeight: 700, fontSize: "0.92rem", flex: 1 }}>Agents</span>
        {view === "list" && (
          <div role="group" aria-label="Agent layout" style={{ display: "flex", border: "1px solid var(--border)", borderRadius: "6px", overflow: "hidden" }}>
            <button type="button" onClick={() => setAgentLayout("cards")} aria-label="Card view" aria-pressed={agentLayout === "cards"} title="Card view" style={{ background: agentLayout === "cards" ? "var(--sidebar-active)" : "transparent", color: "var(--fg)", border: 0, padding: "5px 8px", cursor: "pointer", display: "flex" }}><LayoutGrid size={15} /></button>
            <button type="button" onClick={() => setAgentLayout("list")} aria-label="List view" aria-pressed={agentLayout === "list"} title="List view" style={{ background: agentLayout === "list" ? "var(--sidebar-active)" : "transparent", color: "var(--fg)", border: 0, borderLeft: "1px solid var(--border)", padding: "5px 8px", cursor: "pointer", display: "flex" }}><List size={15} /></button>
          </div>
        )}
        {view === "edit" && (
          <button onClick={cancelEdit} style={{
            background: "transparent", border: "1px solid var(--border)",
            borderRadius: "5px", padding: "4px 10px", cursor: "pointer",
            fontSize: "0.8rem", color: "var(--fg-muted)", display: "flex", alignItems: "center", gap: "4px",
          }}>
            <X size={12} /> Back
          </button>
        )}
        <button onClick={startCreate} style={{
          background: "var(--btn-bg)", color: "var(--btn-fg)",
          border: "none", borderRadius: "5px", padding: "5px 14px",
          cursor: "pointer", fontWeight: 600, fontSize: "0.82rem",
          display: "flex", alignItems: "center", gap: "4px",
        }}>
          <Plus size={13} /> New Agent
        </button>
      </div>

      {/* ── Agent cards or list ── */}
      {view === "list" && (
        <div style={{ flex: 1, overflow: "auto", padding: "1.25rem" }}>
          {loading && <div style={{ color: "var(--fg-muted)" }}>Loading…</div>}
          {!loading && agents.length === 0 && (
            <div style={{ textAlign: "center", color: "var(--fg-muted)", padding: "3rem 0" }}>
              <Users size={40} strokeWidth={1.5} style={{ opacity: 0.3, marginBottom: "0.5rem" }} />
              <div>No agents yet. Click <strong>New Agent</strong> to create one.</div>
            </div>
          )}
          <div style={agentLayout === "cards"
            ? { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))", gap: "8px", alignItems: "start" }
            : { display: "flex", flexDirection: "column", gap: "0.65rem" }}>
            {agents.map(a => (
              <AgentCard key={a.id} agent={a} layout={agentLayout}
                onInvoke={a => setInvokeTarget(a)}
                onEdit={a => selectAgent(a)}
                onDelete={async a => { await api.deleteAgent(a.id); await load(); }}
                onCopy={async a => {
                  const payload = {
                    name: `${a.name}-copy`, description: a.description,
                    instructions: a.instructions, enabled: a.enabled,
                    model: a.model, provider: a.provider,
                    max_iterations: a.max_iterations,
                    mcp_servers: a.mcp_servers, skills: a.skills,
                    enabled_toolsets: a.enabled_toolsets, plugins: a.plugins,
                    gateway_enabled: a.gateway?.enabled ?? false,
                    tags: a.tags,
                  };
                  const created = await api.createAgent(payload);
                  await load(created.id);
                }}
              />
            ))}
          </div>
        </div>
      )}

      {/* ── Edit form ── */}
      {view === "edit" && (
        <div style={{ flex: 1, overflow: "auto", padding: "1.25rem 1.5rem" }}>
          {/* Toolbar */}
          <div style={{
            display: "flex", alignItems: "center", gap: "8px",
            marginBottom: "1.25rem", flexWrap: "wrap",
          }}>
            <span style={{ fontWeight: 700, fontSize: "0.95rem", flex: 1 }}>
              {creating ? "New Agent" : selected?.name}
            </span>
            {!creating && selected && (
              deleteConfirm === selected.id ? (
                <>
                  <span style={{ fontSize: "0.8rem", color: "var(--fg-muted)" }}>Delete?</span>
                  <button onClick={() => deleteAgent(selected.id)} style={{ background: "#ef4444", color: "#fff", border: "none", borderRadius: "5px", padding: "4px 12px", cursor: "pointer", fontSize: "0.8rem" }}>Yes</button>
                  <button onClick={() => setDeleteConfirm(null)} style={{ background: "transparent", border: "1px solid var(--border)", borderRadius: "5px", padding: "4px 10px", cursor: "pointer", fontSize: "0.8rem", color: "var(--fg)" }}>No</button>
                </>
              ) : (
                <button onClick={() => setDeleteConfirm(selected.id)} style={{ display: "flex", alignItems: "center", background: "transparent", border: "1px solid var(--border)", borderRadius: "5px", padding: "4px 8px", cursor: "pointer", color: "#ef4444" }}>
                  <Trash2 size={13} />
                </button>
              )
            )}
            <button onClick={save} disabled={saving || !form.name.trim()} style={{
              background: form.name.trim() ? "var(--btn-bg)" : "var(--sidebar-active)",
              color: form.name.trim() ? "var(--btn-fg)" : "var(--fg-muted)",
              border: "none", borderRadius: "5px", padding: "5px 18px",
              cursor: saving || !form.name.trim() ? "default" : "pointer",
              fontWeight: 600, fontSize: "0.85rem",
            }}>
              {saving ? "Saving…" : "Save"}
            </button>
            {msg && <span style={{ fontSize: "0.78rem", color: msg.ok ? "#22c55e" : "#ef4444" }}>{msg.ok ? "✓" : "✗"} {msg.text}</span>}
          </div>

          <div style={{ maxWidth: "760px", display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0 1.5rem" }}>

            {/* Left column */}
            <div>
              <div style={{ gridColumn: "1/-1" }}>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0 1rem" }}>
                  <Field name="Name *">
                    <input value={form.name} onChange={e => patch("name", e.target.value)}
                      placeholder="my-agent" style={inputSt()} />
                  </Field>
                  <Field name="Enabled">
                    <label style={{ display: "flex", alignItems: "center", gap: "8px", cursor: "pointer", paddingTop: "4px" }}>
                      <input type="checkbox" checked={form.enabled} onChange={e => patch("enabled", e.target.checked)}
                        style={{ width: "15px", height: "15px", cursor: "pointer" }} />
                      <span style={{ fontSize: "0.85rem" }}>Active</span>
                    </label>
                  </Field>
                </div>
              </div>

              <Field name="Description">
                <input value={form.description} onChange={e => patch("description", e.target.value)}
                  placeholder="What this agent does…" style={inputSt()} />
              </Field>

              <Field name="Instructions (System Prompt)">
                <textarea value={form.instructions} onChange={e => patch("instructions", e.target.value)}
                  rows={7} placeholder={"You are a helpful agent that...\n\nFollow these rules:\n- ..."}
                  style={{ ...inputSt(), resize: "vertical", lineHeight: 1.55, fontFamily: "inherit" }} />
              </Field>

              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0 1rem" }}>
                <Field name="Model (optional)">
                  <input value={form.model} onChange={e => patch("model", e.target.value)}
                    placeholder="e.g. gpt-4o" style={inputSt()} />
                </Field>
                <Field name="Provider (optional)">
                  <input value={form.provider} onChange={e => patch("provider", e.target.value)}
                    placeholder="e.g. copilot" style={inputSt()} />
                </Field>
              </div>

              <Field name="Max Iterations">
                <input type="number" min={1} max={200} value={form.max_iterations}
                  onChange={e => patch("max_iterations", parseInt(e.target.value) || 30)}
                  style={inputSt({ width: "120px" })} />
              </Field>

              <Field name="Tags (comma-separated)">
                <input value={form.tags} onChange={e => patch("tags", e.target.value)}
                  placeholder="automation, reporting" style={inputSt()} />
              </Field>
            </div>

            {/* Right column */}
            <div>
              <Field name={`Skills (${form.skills.length} selected)`}>
                <MultiSelect items={skillList} selected={form.skills}
                  onChange={v => patch("skills", v)} label="skills" />
              </Field>

              <Field name={`MCP Servers (${form.mcp_servers.length} selected)`}>
                <MultiSelect items={mcpList} selected={form.mcp_servers}
                  onChange={v => patch("mcp_servers", v)} label="MCP servers" />
              </Field>

              <Field name={`Toolsets (${form.enabled_toolsets.length} selected)`}>
                <MultiSelect items={toolsets} selected={form.enabled_toolsets}
                  onChange={v => patch("enabled_toolsets", v)} label="toolsets" />
              </Field>

              {/* Gateway */}
              <div style={{
                border: "1px solid var(--border)", borderRadius: "8px",
                padding: "0.85rem 1rem", marginTop: "0.5rem",
                background: "var(--bg)",
              }}>
                <label style={{ display: "flex", alignItems: "center", gap: "8px", cursor: "pointer", marginBottom: form.gateway_enabled ? "0.75rem" : 0 }}>
                  <input type="checkbox" checked={form.gateway_enabled}
                    onChange={e => patch("gateway_enabled", e.target.checked)}
                    style={{ width: "14px", height: "14px", cursor: "pointer" }} />
                  <Globe size={13} />
                  <span style={{ fontWeight: 600, fontSize: "0.83rem" }}>Enable Gateway Endpoint</span>
                </label>

                {form.gateway_enabled && (
                  <div style={{ fontSize: "0.78rem", color: "var(--fg-muted)", lineHeight: 1.5 }}>
                    <p style={{ margin: "0 0 6px" }}>
                      After saving, the agent will be accessible at:
                    </p>
                    <code style={{ fontFamily: "monospace", fontSize: "0.72rem", wordBreak: "break-all", color: "var(--blue)" }}>
                      POST /api/gateway/agents/&#123;id&#125;
                    </code>
                    <p style={{ margin: "6px 0 0", fontSize: "0.72rem" }}>
                      Authenticate via <code style={{ fontFamily: "monospace" }}>X-Agent-Key</code> header.
                    </p>

                    {!creating && selected?.gateway?.api_key && (
                      <div style={{ marginTop: "8px" }}>
                        {regenConfirm ? (
                          <span style={{ display: "flex", gap: "6px", alignItems: "center" }}>
                            <span style={{ color: "#ef4444" }}>Regenerate key?</span>
                            <button onClick={regenKey} style={{ background: "#ef4444", color: "#fff", border: "none", borderRadius: "4px", padding: "2px 8px", cursor: "pointer", fontSize: "0.75rem" }}>Yes</button>
                            <button onClick={() => setRegenConfirm(false)} style={{ background: "transparent", border: "1px solid var(--border)", borderRadius: "4px", padding: "2px 6px", cursor: "pointer", fontSize: "0.75rem" }}>No</button>
                          </span>
                        ) : (
                          <button onClick={() => setRegenConfirm(true)} style={{ background: "transparent", border: "1px solid var(--border)", borderRadius: "4px", padding: "3px 10px", cursor: "pointer", fontSize: "0.75rem", color: "var(--fg-muted)", display: "flex", alignItems: "center", gap: "4px" }}>
                            <RefreshCw size={10} /> Regenerate API Key
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>

          </div>
        </div>
      )}

      {/* Invoke dialog */}
      {invokeTarget && (
        <InvokeDialog agent={invokeTarget} onClose={() => setInvokeTarget(null)} />
      )}
    </div>
  );
}
