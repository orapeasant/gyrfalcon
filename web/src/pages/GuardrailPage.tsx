import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";

const card: React.CSSProperties = {
  background: "var(--card)", border: "1px solid var(--color-border)",
  borderRadius: 8, padding: "1rem",
};

const selectStyle: React.CSSProperties = {
  background: "var(--input-bg)", color: "var(--fg)",
  border: "1px solid var(--color-border)", borderRadius: 6,
  padding: "0.55rem 0.65rem", fontSize: "0.9rem", minWidth: 260,
};

export function GuardrailPage() {
  const [mode, setMode] = useState("smart");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    api.getConfig()
      .then((config) => setMode(config.security?.approval_mode || "smart"))
      .catch((err) => setError(String(err)))
      .finally(() => setLoading(false));
  }, []);

  async function save() {
    setSaving(true);
    setError("");
    setMessage("");
    try {
      await api.updateConfig({ security: { approval_mode: mode } });
      setMessage("Guardrail setting saved.");
    } catch (err) {
      setError(String(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div style={{ color: "var(--fg)", maxWidth: 900 }}>
      <h2 style={{ margin: "0 0 0.25rem" }}>Guardrail</h2>
      <p style={{ color: "var(--fg-muted)", marginTop: 0, fontSize: "0.9rem" }}>
        Configure approval prompts for potentially dangerous tool actions.
      </p>

      <section style={{ ...card, display: "grid", gap: "0.75rem" }}>
        <label htmlFor="approval-mode" style={{ fontWeight: 600 }}>Dangerous command approval</label>
        <select id="approval-mode" value={mode} disabled={loading} onChange={(e) => setMode(e.target.value)} style={selectStyle}>
          <option value="smart">Smart — ask before dangerous commands</option>
          <option value="none">Disabled — do not prompt for dangerous commands</option>
        </select>
        <p style={{ color: "var(--fg-muted)", fontSize: "0.82rem", margin: 0, lineHeight: 1.5 }}>
          Commands classified as blocked remain blocked in either mode. Read-only tools do not prompt for approval. Gateway agents also remain limited to their configured toolsets.
        </p>
        {error && <div role="alert" style={{ color: "var(--red)" }}>{error}</div>}
        {message && <div role="status" style={{ color: "var(--green)" }}>{message}</div>}
        <div><button type="button" disabled={loading || saving} onClick={save} style={{ background: "var(--primary)", color: "var(--btn-fg)", border: 0, borderRadius: 6, padding: "0.55rem 0.9rem", fontWeight: 600, cursor: "pointer" }}>{saving ? "Saving…" : "Save guardrail"}</button></div>
      </section>
      <p style={{ color: "var(--fg-muted)", fontSize: "0.82rem", marginTop: "0.85rem" }}>
        Set an individual agent’s tool ceiling on the <Link to="/agents" style={{ color: "var(--blue)" }}>Agents</Link> page.
      </p>
    </div>
  );
}
