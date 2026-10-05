import React, { useEffect, useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { api } from "../lib/api";

type RoutingRule = {
  pattern: string;
  toolset: string;
  model: string;
  max_iterations: string;
};

const card: React.CSSProperties = {
  background: "var(--card)", border: "1px solid var(--color-border)",
  borderRadius: 8, padding: "1rem",
};

const input: React.CSSProperties = {
  width: "100%", boxSizing: "border-box", background: "var(--input-bg)",
  color: "var(--fg)", border: "1px solid var(--color-border)",
  borderRadius: 6, padding: "0.5rem 0.6rem", fontSize: "0.86rem",
};

const label: React.CSSProperties = {
  display: "block", color: "var(--fg-muted)", fontSize: "0.75rem",
  marginBottom: "0.3rem",
};

function newRule(): RoutingRule {
  return { pattern: "", toolset: "", model: "", max_iterations: "" };
}

export function RoutingPage() {
  const [rules, setRules] = useState<RoutingRule[]>([]);
  const [toolsets, setToolsets] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    Promise.all([api.getConfig(), api.getToolsets()])
      .then(([config, toolsetData]) => {
        setRules((config.gateway?.routing_rules || []).map((rule: any) => ({
          pattern: String(rule.pattern || ""),
          toolset: String(rule.toolset || ""),
          model: String(rule.model || ""),
          max_iterations: rule.max_iterations == null ? "" : String(rule.max_iterations),
        })));
        setToolsets(Object.keys(toolsetData.toolsets || {}));
      })
      .catch((err) => setError(String(err)))
      .finally(() => setLoading(false));
  }, []);

  function update(index: number, key: keyof RoutingRule, value: string) {
    setRules((current) => current.map((rule, i) => i === index ? { ...rule, [key]: value } : rule));
  }

  async function save() {
    if (rules.some((rule) => !rule.pattern.trim())) {
      setError("Every routing rule needs a pattern.");
      return;
    }
    setSaving(true);
    setError("");
    setMessage("");
    try {
      await api.updateConfig({
        gateway: {
          routing_rules: rules.map((rule) => ({
            pattern: rule.pattern.trim(),
            ...(rule.toolset ? { toolset: rule.toolset } : {}),
            ...(rule.model.trim() ? { model: rule.model.trim() } : {}),
            ...(rule.max_iterations ? { max_iterations: Number(rule.max_iterations) } : {}),
          })),
        },
      });
      setMessage("Routing rules saved. Restart the gateway to apply them.");
    } catch (err) {
      setError(String(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div style={{ color: "var(--fg)", maxWidth: 1100 }}>
      <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginBottom: "0.25rem" }}>
        <h2 style={{ margin: 0 }}>Routing</h2>
        <button type="button" onClick={() => setRules((current) => [...current, newRule()])} style={{ ...input, width: "auto", display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}><Plus size={14} /> Add rule</button>
      </div>
      <p style={{ color: "var(--fg-muted)", marginTop: 0, fontSize: "0.9rem" }}>
        Match a gateway conversation by channel name or ID. The first matching rule sets its toolset, model, or iteration limit.
      </p>

      {error && <div role="alert" style={{ ...card, borderColor: "var(--red)", color: "var(--red)", marginBottom: "0.75rem" }}>{error}</div>}
      {message && <div role="status" style={{ ...card, color: "var(--green)", marginBottom: "0.75rem" }}>{message}</div>}
      {loading ? <p style={{ color: "var(--fg-muted)" }}>Loading routing rules…</p> : rules.length === 0 ? (
        <div style={card}><p style={{ margin: 0, color: "var(--fg-muted)" }}>No rules configured. Gateway messages use the platform defaults.</p></div>
      ) : (
        <div style={{ display: "grid", gap: "0.75rem" }}>
          {rules.map((rule, index) => <section key={index} style={{ ...card, display: "grid", gridTemplateColumns: "minmax(180px, 1.4fr) repeat(3, minmax(135px, 1fr)) auto", gap: "0.65rem", alignItems: "end" }}>
            <div><label style={label}>Channel pattern (regex)</label><input value={rule.pattern} onChange={(e) => update(index, "pattern", e.target.value)} placeholder="^#support" style={input} /></div>
            <div><label style={label}>Toolset</label><select value={rule.toolset} onChange={(e) => update(index, "toolset", e.target.value)} style={input}><option value="">Platform default</option>{toolsets.map((name) => <option key={name} value={name}>{name}</option>)}</select></div>
            <div><label style={label}>Model override</label><input value={rule.model} onChange={(e) => update(index, "model", e.target.value)} placeholder="Default model" style={input} /></div>
            <div><label style={label}>Max iterations</label><input type="number" min={1} value={rule.max_iterations} onChange={(e) => update(index, "max_iterations", e.target.value)} placeholder="Default" style={input} /></div>
            <button type="button" aria-label={`Remove rule ${index + 1}`} title="Remove rule" onClick={() => setRules((current) => current.filter((_, i) => i !== index))} style={{ ...input, width: "auto", cursor: "pointer", color: "var(--red)" }}><Trash2 size={14} /></button>
          </section>)}
        </div>
      )}

      <button type="button" onClick={save} disabled={loading || saving} style={{ ...input, width: "auto", marginTop: "1rem", background: "var(--primary)", color: "var(--btn-fg)", borderColor: "var(--primary)", cursor: "pointer", fontWeight: 600 }}>
        {saving ? "Saving…" : "Save routing rules"}
      </button>
    </div>
  );
}
