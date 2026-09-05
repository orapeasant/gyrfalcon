import React, { useState, useEffect } from "react";
import { api } from "../lib/api";

export function ConfigPage() {
  const [config, setConfig] = useState<string>("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.getConfigRaw().then((data) => {
      setConfig(data.content || "");
      setLoading(false);
    }).catch(() => setLoading(false));
  }, []);

  const handleSave = async () => {
    await api.putConfigRaw(config);
    alert("Config saved!");
  };

  if (loading) return <p>Loading...</p>;

  return (
    <div>
      <h2 style={{ color: "var(--fg)" }}>Configuration</h2>
      <textarea
        value={config}
        onChange={(e) => setConfig(e.target.value)}
        style={{
          width: "100%", height: "400px",
          background: "var(--color-midground)",
          color: "var(--color-foreground)",
          border: "1px solid var(--color-border)",
          borderRadius: "8px",
          padding: "1rem",
          fontFamily: "monospace",
          fontSize: "0.9rem",
          resize: "vertical",
        }}
      />
      <button onClick={handleSave} style={{
        marginTop: "1rem", padding: "0.5rem 1rem",
      background: "var(--btn-bg)", color: "#000",
        border: "none", borderRadius: "4px", cursor: "pointer"
      }}>
        Save
      </button>
    </div>
  );
}
