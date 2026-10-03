import React, { useState, useEffect } from "react";
import { api } from "../lib/api";

export function AnalyticsPage() {
  const [data, setData] = useState<any[]>([]);
  useEffect(() => {
    api.getAnalytics(30).then((res) => {
      const sorted = (res.daily || []).slice().sort((a: any, b: any) =>
        new Date(b.day).getTime() - new Date(a.day).getTime()
      );
      setData(sorted);
    }).catch(() => {});
  }, []);

  return (
    <div style={{ color: "var(--fg)", width: "100%" }}>
      <h1 style={{ fontSize: 18, margin: "0 0 14px" }}>Analytics</h1>
      {data.length === 0 ? (
        <p style={{ color: "var(--color-muted)" }}>No usage data yet.</p>
      ) : (
        <div style={{ width: "100%", overflowX: "auto", border: "1px solid var(--border)", borderRadius: 8, background: "var(--card)" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ borderBottom: "1px solid var(--color-border)" }}>
              {["Day", "Sessions", "Input Tokens", "Output Tokens", "Cost"].map((heading, index) => (
                <th key={heading} style={{ textAlign: index ? "right" : "left", padding: "9px 12px", color: "var(--fg-muted)", fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.05em", whiteSpace: "nowrap" }}>{heading}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.map((row: any, i: number) => (
              <tr key={i} style={{ borderBottom: i === data.length - 1 ? 0 : "1px solid var(--border)" }}>
                <td style={{ padding: "10px 12px" }}>{row.day}</td>
                <td style={{ padding: "10px 12px", textAlign: "right" }}>{row.sessions}</td>
                <td style={{ padding: "10px 12px", textAlign: "right" }}>{row.input_tokens?.toLocaleString()}</td>
                <td style={{ padding: "10px 12px", textAlign: "right" }}>{row.output_tokens?.toLocaleString()}</td>
                <td style={{ padding: "10px 12px", textAlign: "right" }}>${(row.cost || 0).toFixed(4)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
    </div>
  );
}
